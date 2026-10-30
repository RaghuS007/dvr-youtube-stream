#!/usr/bin/env python3
"""
Relay a Hikvision DVR RTSP channel to YouTube Live using FFmpeg.

- Pulls the RTSP main stream (H.265) over TCP
- Transcodes to H.264 + AAC at a constant frame rate with a fixed GOP
- Pushes to YouTube over RTMP/RTMPS
- Supervises FFmpeg: restarts on exit, kills it on stalls, backs off exponentially

Usage:
    python dvr_to_youtube.py                 # uses config.ini next to this script
    python dvr_to_youtube.py --config my.ini
    python dvr_to_youtube.py --dry-run       # print the (redacted) FFmpeg command and exit
    python dvr_to_youtube.py --probe         # only test the DVR connection and exit
"""

import argparse
import configparser
import json
import logging
import logging.handlers
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote

SCRIPT_DIR = Path(__file__).resolve().parent
PLACEHOLDER_KEY = "PASTE-YOUR-YOUTUBE-STREAM-KEY-HERE"

log = logging.getLogger("dvr2yt")
stop_event = threading.Event()


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> configparser.ConfigParser:
    if not path.exists():
        sys.exit(f"Config file not found: {path}")
    cfg = configparser.ConfigParser(interpolation=None)  # passwords may contain '%'
    cfg.read(path, encoding="utf-8")

    # Environment variables override secrets in the file (handy for services/CI)
    if os.environ.get("YT_STREAM_KEY"):
        cfg["youtube"]["stream_key"] = os.environ["YT_STREAM_KEY"]
    if os.environ.get("DVR_PASSWORD"):
        cfg["dvr"]["password"] = os.environ["DVR_PASSWORD"]
    return cfg


def build_rtsp_url(dvr: configparser.SectionProxy) -> str:
    # Credentials MUST be percent-encoded: the '@' in "Windows@2025" would
    # otherwise be parsed as the user-info/host separator and break the URL.
    user = quote(dvr["username"], safe="")
    pwd = quote(dvr["password"], safe="")
    channel = dvr.getint("channel", 7)
    stream_type = 1 if dvr.get("stream", "main").lower() == "main" else 2
    # Hikvision ISAPI scheme: <channel><01=main|02=sub>  ->  ch1 main = 101
    path = f"/Streaming/Channels/{channel}{stream_type:02d}"
    return f"rtsp://{user}:{pwd}@{dvr['host']}:{dvr.getint('port', 554)}{path}"


def build_rtmp_url(yt: configparser.SectionProxy) -> str:
    return f"{yt['ingest_url'].rstrip('/')}/{yt['stream_key'].strip()}"


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "****")
    return text


# --------------------------------------------------------------------------- #
# DVR probing
# --------------------------------------------------------------------------- #
def probe_source(ffprobe: str, rtsp_url: str, timeout_s: int) -> dict | None:
    """Return {'video': codec, 'audio': codec|None, 'width', 'height'} or None if unreachable."""
    cmd = [
        ffprobe, "-v", "error",
        "-rtsp_transport", "tcp",
        "-timeout", str(timeout_s * 1_000_000),  # socket I/O timeout, microseconds
        "-analyzeduration", "5000000", "-probesize", "5000000",  # wait for SPS/PPS
        "-show_entries", "stream=codec_type,codec_name,width,height,avg_frame_rate",
        "-of", "json", rtsp_url,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 10)
    except subprocess.TimeoutExpired:
        log.warning("ffprobe timed out connecting to DVR")
        return None
    if res.returncode != 0:
        log.warning("ffprobe failed: %s", res.stderr.strip()[-500:] or f"exit {res.returncode}")
        return None

    info = {"video": None, "audio": None, "width": None, "height": None, "fps": None}
    for st in json.loads(res.stdout or "{}").get("streams", []):
        if st.get("codec_type") == "video" and info["video"] is None:
            info.update(video=st.get("codec_name"), width=st.get("width"),
                        height=st.get("height"), fps=st.get("avg_frame_rate"))
        elif st.get("codec_type") == "audio" and info["audio"] is None:
            info["audio"] = st.get("codec_name")
    if not info["video"]:
        return None
    if not info["width"]:
        # Connected, but the decoder never got valid parameter sets (typical of
        # Hikvision H.265+/Smart Codec streams). FFmpeg would hang with 0 frames.
        log.error("DVR sends %s video FFmpeg cannot decode (resolution unknown). "
                  "Set the channel's encoding to plain H.264 (H.264+/H.265+ off) "
                  "on the DVR, or use stream = sub in config.ini.", info["video"])
        return None
    return info


# --------------------------------------------------------------------------- #
# FFmpeg command
# --------------------------------------------------------------------------- #
def build_ffmpeg_cmd(cfg, ffmpeg: str, rtsp_url: str, rtmp_url: str, has_audio: bool) -> list[str]:
    enc = cfg["encoder"]
    fps = enc.getint("fps", 12)
    gop = fps * enc.getint("keyframe_seconds", 2)  # YouTube wants a keyframe every <=2 s
    timeout_us = cfg["resilience"].getint("rtsp_timeout", 10) * 1_000_000

    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats", "-progress", "pipe:1",
           "-max_error_rate", "1.0"]  # don't abort on a high decode-error rate

    # ---- Input 0: DVR over RTSP/TCP (UDP drops packets -> smeared frames) ----
    cmd += ["-rtsp_transport", "tcp", "-timeout", str(timeout_us),
            "-analyzeduration", "5000000", "-probesize", "5000000",
            # This DVR's H.264 is often partly corrupt. Keep damaged frames (smeared
            # picture) rather than dropping them: dropped frames starve YouTube and
            # it disconnects with "No data".
            "-fflags", "+genpts", "-flags2", "+showall", "-err_detect", "ignore_err"]
    if enc.getboolean("wallclock_timestamps", False):
        # DVRs often emit jittery/broken RTP timestamps; wallclock keeps output monotonic
        cmd += ["-use_wallclock_as_timestamps", "1"]
    cmd += ["-i", rtsp_url]

    # ---- Input 1: silent audio if the DVR channel has none (YouTube expects audio) ----
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

    cmd += ["-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0"]

    # ---- Video filters: constant 12 fps, optional letterbox to a fixed canvas ----
    vf = ["setpts=PTS-STARTPTS", f"fps={fps}"]
    size = enc.get("output_size", "").strip()
    if size:
        w, h = size.lower().split("x")
        vf += [f"scale={w}:{h}:force_original_aspect_ratio=decrease",
               f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"]
    vf.append("format=yuv420p")
    cmd += ["-vf", ",".join(vf)]

    # ---- H.264 encode (CBR-ish, fixed GOP, no scene-cut keyframes) ----
    bitrate = enc.get("video_bitrate", "3000k")
    bufsize = enc.get("buffer_size", "6000k")
    cmd += [
        "-c:v", enc.get("video_codec", "libx264"),
        "-preset", enc.get("preset", "veryfast"),
        "-profile:v", "high",
        "-b:v", bitrate, "-minrate", bitrate, "-maxrate", bitrate, "-bufsize", bufsize,
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-r", str(fps),
    ]
    if enc.get("video_codec", "libx264") == "libx264":
        # True CBR with filler data: a static CCTV scene otherwise drops to ~150 kbps
        # and YouTube reports "not receiving enough video".
        cmd += ["-x264-params", "nal-hrd=cbr:force-cfr=1"]

    # ---- AAC audio (Hikvision audio is usually G.711 8 kHz mono -> resample) ----
    # Bursty G.711 packets: stretch/pad audio to follow the video timeline instead of
    # emitting out-of-order DTS, which makes the FLV muxer abort.
    cmd += ["-af", "aresample=async=1:first_pts=0"]
    cmd += ["-c:a", "aac", "-b:a", enc.get("audio_bitrate", "128k"), "-ar", "44100", "-ac", "2"]
    if not has_audio:
        cmd += ["-shortest"]  # stop when the camera stops, not when anullsrc does (never)

    # ---- Output: FLV over RTMP(S) ----
    cmd += ["-max_interleave_delta", "0", "-f", "flv", "-flvflags", "no_duration_filesize", rtmp_url]
    return cmd


# --------------------------------------------------------------------------- #
# FFmpeg supervision
# --------------------------------------------------------------------------- #
def run_ffmpeg_once(cmd: list[str], secrets: list[str], stall_timeout: int, status_every: int) -> float:
    """Run FFmpeg until it exits, stalls, or we're asked to stop. Returns seconds it ran."""
    started = time.monotonic()
    state = {"last_frame": -1, "last_progress": started, "fields": {}}
    tail = deque(maxlen=20)

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=creationflags,
    )

    def read_progress():
        for line in proc.stdout:
            key, _, val = line.strip().partition("=")
            state["fields"][key] = val
            if key == "frame":
                try:
                    frame = int(val)
                except ValueError:
                    continue
                if frame > state["last_frame"]:
                    state["last_frame"] = frame
                    state["last_progress"] = time.monotonic()

    def read_stderr():
        for line in proc.stderr:
            line = redact(line.rstrip(), secrets)
            if line:
                tail.append(line)
                log.warning("ffmpeg: %s", line)

    threading.Thread(target=read_progress, daemon=True).start()
    threading.Thread(target=read_stderr, daemon=True).start()

    last_status = started
    reason = None
    while proc.poll() is None:
        if stop_event.wait(1):
            reason = "shutdown requested"
            break
        now = time.monotonic()
        if now - state["last_progress"] > stall_timeout:
            reason = f"no new frames for {stall_timeout}s (stalled)"
            break
        if now - last_status >= status_every and state["last_frame"] >= 0:
            f = state["fields"]
            log.info("LIVE  frames=%s fps=%s bitrate=%s speed=%s uptime=%ds",
                     f.get("frame"), f.get("fps"), f.get("bitrate"), f.get("speed"),
                     int(now - started))
            last_status = now

    if proc.poll() is None:
        log.info("Stopping FFmpeg: %s", reason)
        stop_process(proc)

    ran = time.monotonic() - started
    rc = proc.returncode
    if rc is not None and rc >= 2**31:
        rc -= 2**32  # Windows reports negative codes unsigned; -10053 = connection aborted
    if not stop_event.is_set():
        log.error("FFmpeg ended (exit=%s) after %.0fs. Last output:\n  %s",
                  rc, ran, "\n  ".join(tail) or "(none)")
    return ran


def stop_process(proc: subprocess.Popen) -> None:
    try:
        proc.stdin.write("q")  # graceful: lets FFmpeg flush & close the RTMP session
        proc.stdin.flush()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.kill()
    proc.wait()


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    fileh = logging.handlers.RotatingFileHandler(
        log_dir / "stream.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fileh.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(console)
    log.addHandler(fileh)


def find_tool(name: str, configured: str) -> str:
    path = shutil.which(configured or name)
    if not path:
        sys.exit(f"'{configured or name}' not found. Install FFmpeg and add it to PATH "
                 f"(or set [tools] {name} = full\\path\\to\\{name}.exe in config.ini).")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="Hikvision DVR -> YouTube Live relay")
    ap.add_argument("--config", type=Path, default=SCRIPT_DIR / "config.ini")
    ap.add_argument("--dry-run", action="store_true", help="print the FFmpeg command and exit")
    ap.add_argument("--probe", action="store_true", help="only test the DVR stream and exit")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging(SCRIPT_DIR / "logs")

    tools = cfg["tools"] if cfg.has_section("tools") else {}
    ffmpeg = find_tool("ffmpeg", tools.get("ffmpeg", ""))
    ffprobe = find_tool("ffprobe", tools.get("ffprobe", ""))

    rtsp_url = build_rtsp_url(cfg["dvr"])
    rtmp_url = build_rtmp_url(cfg["youtube"])
    secrets = [cfg["youtube"]["stream_key"].strip(), quote(cfg["dvr"]["password"], safe=""),
               cfg["dvr"]["password"]]
    res = cfg["resilience"]

    if args.probe:
        info = probe_source(ffprobe, rtsp_url, res.getint("rtsp_timeout", 10))
        if not info:
            sys.exit("DVR stream NOT reachable. Check IP, credentials, channel and RTSP settings.")
        log.info("DVR OK: video=%s %sx%s @ %s fps, audio=%s",
                 info["video"], info["width"], info["height"], info["fps"], info["audio"] or "none")
        return

    if args.dry_run:
        cmd = build_ffmpeg_cmd(cfg, ffmpeg, rtsp_url, rtmp_url, has_audio=False)
        print(redact(subprocess.list2cmdline(cmd), secrets))
        return

    key = cfg["youtube"]["stream_key"].strip()
    if not key or key == PLACEHOLDER_KEY:
        sys.exit("Set your YouTube stream key in config.ini ([youtube] stream_key) "
                 "or the YT_STREAM_KEY environment variable.")

    def on_signal(signum, _frame):
        log.info("Signal %s received, shutting down...", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    if hasattr(signal, "SIGBREAK"):  # Ctrl+Break / console close on Windows
        signal.signal(signal.SIGBREAK, on_signal)

    min_backoff = res.getint("min_backoff", 2)
    max_backoff = res.getint("max_backoff", 60)
    stable_after = res.getint("stable_after", 60)
    stall_timeout = res.getint("stall_timeout", 30)
    status_every = res.getint("status_interval", 60)
    backoff = min_backoff
    attempt = 0

    log.info("Source : %s", redact(rtsp_url, secrets))
    log.info("Target : %s", redact(rtmp_url, secrets))

    while not stop_event.is_set():
        attempt += 1
        log.info("Attempt #%d: probing DVR...", attempt)
        info = probe_source(ffprobe, rtsp_url, res.getint("rtsp_timeout", 10))

        if info:
            log.info("DVR OK: video=%s %sx%s @ %s fps, audio=%s",
                     info["video"], info["width"], info["height"], info["fps"],
                     info["audio"] or "none (injecting silent track)")
            cmd = build_ffmpeg_cmd(cfg, ffmpeg, rtsp_url, rtmp_url, has_audio=bool(info["audio"]))
            log.info("Starting FFmpeg -> YouTube")
            ran = run_ffmpeg_once(cmd, secrets, stall_timeout, status_every)
            if ran >= stable_after:
                backoff = min_backoff  # it was healthy; reconnect quickly
        else:
            log.warning("DVR unreachable.")

        if stop_event.is_set():
            break
        log.info("Reconnecting in %ds...", backoff)
        if stop_event.wait(backoff):
            break
        backoff = min(backoff * 2, max_backoff)

    log.info("Stopped.")


if __name__ == "__main__":
    main()
