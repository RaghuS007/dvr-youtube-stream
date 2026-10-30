"""
Streaming engine: Hikvision DVR channels -> one continuous YouTube Live output.

    InputSource  (one per DVR channel being pulled)
        ffmpeg: RTSP -> raw yuv420p frames on stdout, scaled/letterboxed to the canvas
                     -> raw s16le 44.1 kHz stereo audio over a localhost TCP socket
    Engine
        Two real-time clocked writer threads feed ONE persistent output ffmpeg:
        each tick they send the newest frame / audio of the active source, or the
        last frame / silence when there is none.
    OutputSink
        ffmpeg: raw A/V -> H.264 + AAC -> FLV over RTMP(S) to YouTube

The output never restarts and its timestamps come from the writers' clock, so
switching channels, muting audio or losing a camera doesn't touch the YouTube
connection: viewers just see the picture change (or freeze on the last frame).
"""

from __future__ import annotations

import configparser
import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
import socket
import subprocess
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from urllib.parse import quote

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.ini"
LOG_DIR = APP_DIR / "logs"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console popups under pythonw

# Request 1ms timer precision on Windows to eliminate sleep quantization jitter
if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass

AUDIO_RATE, AUDIO_CHANNELS = 44100, 2
AUDIO_BPS = AUDIO_RATE * AUDIO_CHANNELS * 2   # bytes per second of s16le stereo
AUDIO_TICK = 0.02                             # audio writer period, seconds
AUDIO_CHUNK = int(AUDIO_BPS * AUDIO_TICK)     # 3528 bytes = 882 whole stereo samples
SILENCE = bytes(AUDIO_CHUNK)

SWITCH_TIMEOUT = 25   # seconds for a newly selected channel to deliver its first frame
OUTPUT_STALL = 30     # seconds without output progress before reconnecting to YouTube

log = logging.getLogger("dvrapp")


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
DEFAULTS = {
    "dvr": {"host": "192.168.0.17", "port": "554", "username": "admin", "password": "",
            "channels": "8", "stream": "main"},
    "youtube": {"stream_key": "", "ingest_url": "rtmps://a.rtmps.youtube.com:443/live2"},
    "encoder": {"video_codec": "h264_qsv", "preset": "veryfast", "fps": "24",
                "keyframe_seconds": "2", "video_bitrate": "2500k", "audio_bitrate": "128k",
                "canvas": "1920x1080"},
    "switching": {"channel": "7", "mode": "off", "rotate_seconds": "30",
                  "auto_channels": "1,2,3,4,5,6,7,8", "failover_after": "10",
                  "failback_seconds": "60"},
    "audio": {"enabled": "true", "muted_channels": ""},
    "osd": {"enabled": "false", "show_channel": "true", "show_time": "true",
            "show_temp": "true", "weather_location": "auto", "font_size": "22"},
    "tools": {"ffmpeg": "", "ffprobe": ""},
}

TEMPLATE = """\
# DVR -> YouTube Live settings. Written by the app; you can also edit it by hand
# while the app is closed. Keep it private: it holds your DVR password and stream key.

[dvr]
host     = {dvr[host]}
port     = {dvr[port]}
username = {dvr[username]}
# Plain text; the app URL-encodes it ('@' -> %40).
password = {dvr[password]}
# Number of camera channels on the DVR.
channels = {dvr[channels]}
# main = full resolution (x01) | sub = low resolution (x02)
stream   = {dvr[stream]}

[youtube]
stream_key = {youtube[stream_key]}
# rtmps://a.rtmps.youtube.com:443/live2 (encrypted) or rtmp://a.rtmp.youtube.com/live2
ingest_url = {youtube[ingest_url]}

[encoder]
# libx264 (CPU) | h264_nvenc (NVIDIA) | h264_qsv (Intel) | h264_amf (AMD)
video_codec      = {encoder[video_codec]}
preset           = {encoder[preset]}
fps              = {encoder[fps]}
keyframe_seconds = {encoder[keyframe_seconds]}
video_bitrate    = {encoder[video_bitrate]}
audio_bitrate    = {encoder[audio_bitrate]}
# Every channel is scaled/letterboxed onto this frame (WIDTHxHEIGHT).
canvas           = {encoder[canvas]}

[switching]
# Main channel: shown at start, and the one failover returns to.
channel          = {switching[channel]}
# off = stay on the channel | failover = next working channel on failure | rotate = cycle
mode             = {switching[mode]}
rotate_seconds   = {switching[rotate_seconds]}
# Channels used by failover / rotate, comma-separated.
auto_channels    = {switching[auto_channels]}
# Seconds without new frames before a channel counts as failed.
failover_after   = {switching[failover_after]}
# In failover mode, how often to try returning to the main channel.
failback_seconds = {switching[failback_seconds]}

[audio]
enabled = {audio[enabled]}
# Channels whose camera audio is replaced by silence, comma-separated (e.g. 3,5).
muted_channels = {audio[muted_channels]}

[osd]
# Burn camera label, live clock, and live weather temperature into the video stream (true | false)
enabled          = {osd[enabled]}
show_channel     = {osd[show_channel]}
show_time        = {osd[show_time]}
show_temp        = {osd[show_temp]}
weather_location = {osd[weather_location]}
font_size        = {osd[font_size]}

[tools]
# Leave empty to use ffmpeg/ffprobe from PATH.
ffmpeg  = {tools[ffmpeg]}
ffprobe = {tools[ffprobe]}
"""


def load_settings(path: Path = CONFIG_PATH) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(interpolation=None)  # passwords may contain '%'
    cfg.read_dict(DEFAULTS)
    if path.exists():
        old = configparser.ConfigParser(interpolation=None)
        old.read(path, encoding="utf-8")
        for sec in old.sections():
            if not cfg.has_section(sec):
                cfg.add_section(sec)
            for k, v in old[sec].items():
                cfg[sec][k] = v
        # Migrate the single-channel script's layout
        if old.has_option("dvr", "channel") and not old.has_option("switching", "channel"):
            cfg["switching"]["channel"] = old["dvr"]["channel"]
        size = old.get("encoder", "output_size", fallback="").strip()
        if size and not old.has_option("encoder", "canvas"):
            cfg["encoder"]["canvas"] = size
    return cfg


def save_settings(cfg: configparser.ConfigParser, path: Path = CONFIG_PATH) -> None:
    text = TEMPLATE.format(**{s: dict(cfg[s]) for s in DEFAULTS})
    # Keep sections the app doesn't manage (e.g. [resilience] used by dvr_to_youtube.py)
    for sec in cfg.sections():
        if sec not in DEFAULTS:
            text += f"\n[{sec}]\n" + "".join(f"{k} = {v}\n" for k, v in cfg[sec].items())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def parse_channels(text: str) -> list[int]:
    out = set()
    for part in text.replace(" ", "").split(","):
        if part.isdigit() and int(part) > 0:
            out.add(int(part))
    return sorted(out)


def canvas_size(cfg) -> tuple[int, int]:
    w, h = (int(x) for x in cfg["encoder"]["canvas"].lower().split("x"))
    return w - w % 2, h - h % 2


def channel_label(cfg: configparser.ConfigParser, channel: int) -> str:
    """Return human-readable channel label, e.g. '7 (Backyard)' or '7'."""
    if cfg.has_section("channel_names"):
        name = cfg.get("channel_names", str(channel), fallback="").strip()
        if name:
            return f"{channel} ({name})"
    return str(channel)


def rtsp_url(cfg, channel: int) -> str:
    d = cfg["dvr"]
    stream = 1 if d.get("stream", "main").lower() == "main" else 2
    # Credentials must be percent-encoded: the '@' in a password would end the user-info part
    return (f"rtsp://{quote(d['username'], safe='')}:{quote(d['password'], safe='')}"
            f"@{d['host']}:{d.getint('port', 554)}/Streaming/Channels/{channel}{stream:02d}")


def rtmp_url(cfg) -> str:
    y = cfg["youtube"]
    return f"{y['ingest_url'].strip().rstrip('/')}/{y['stream_key'].strip()}"


def find_tool(cfg, name: str) -> str:
    configured = cfg["tools"].get(name, "").strip()
    path = shutil.which(configured or name)
    if path:
        return path
    # winget's link folder isn't on PATH for programs started before FFmpeg was installed
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / f"{name}.exe"
    if winget.exists():
        return str(winget)
    raise RuntimeError(f"{name} not found. Install FFmpeg (winget install Gyan.FFmpeg) "
                       f"or set its full path under [tools] in config.ini.")


def _double_rate(rate: str) -> str:
    rate = rate.strip().lower()
    mult = {"k": 1, "m": 1000}.get(rate[-1:], None)
    kbps = float(rate[:-1]) * mult if mult else float(rate) / 1000
    return f"{int(kbps * 2)}k"


# --------------------------------------------------------------------------- #
# Logging (secrets are masked before anything is written)
# --------------------------------------------------------------------------- #
def redact(text: str, secrets: list[str] | None = None) -> str:
    """Mask known sensitive strings (passwords, stream keys) from text."""
    target_secrets = secrets if secrets is not None else _redact.secrets
    for s in target_secrets:
        if s:
            text = text.replace(s, "****")
    return text


class _RedactFilter(logging.Filter):
    secrets: list[str] = []

    def filter(self, record):
        record.msg = redact(record.getMessage(), self.secrets)
        record.args = None
        return True


_redact = _RedactFilter()


def set_secrets(cfg) -> None:
    pwd = cfg["dvr"]["password"]
    vals = {cfg["youtube"]["stream_key"].strip(), pwd, quote(pwd, safe="")}
    _redact.secrets = sorted((v for v in vals if len(v) >= 4), key=len, reverse=True)


def setup_logging(*handlers: logging.Handler) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fileh = logging.handlers.RotatingFileHandler(
        LOG_DIR / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    for h in (fileh, *handlers):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)
    log.addFilter(_redact)


# --------------------------------------------------------------------------- #
# Weather / Temperature for OSD
# --------------------------------------------------------------------------- #
def fetch_temperature(location: str = "auto", timeout: int = 5) -> str | None:
    """Fetch current outdoor temperature in degrees Celsius (°C).

    Supports:
      - 'auto': Geolocation by public IP -> Open-Meteo current forecast, with wttr.in fallback.
      - City name (e.g. 'London', 'Bengaluru'): Open-Meteo geocoding -> forecast, with wttr.in fallback.
      - 'lat,lon' coordinates (e.g. '12.97,77.59'): Direct Open-Meteo forecast.
    """
    location = (location or "auto").strip()
    headers = {"User-Agent": "DVR-YouTube-Stream/2.0"}
    lat, lon = None, None

    if "," in location:
        parts = location.split(",")
        try:
            lat, lon = float(parts[0].strip()), float(parts[1].strip())
        except ValueError:
            pass
    elif location.lower() == "auto":
        try:
            req = urllib.request.Request("http://ip-api.com/json/", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                lat, lon = data.get("lat"), data.get("lon")
        except Exception:
            pass
    else:
        try:
            geo_url = (f"https://geocoding-api.open-meteo.com/v1/search?"
                       f"name={quote(location)}&count=1")
            req = urllib.request.Request(geo_url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                results = data.get("results") or []
                if results:
                    lat, lon = results[0].get("latitude"), results[0].get("longitude")
        except Exception:
            pass

    if lat is not None and lon is not None:
        try:
            url = (f"https://api.open-meteo.com/v1/forecast?"
                   f"latitude={lat}&longitude={lon}&current=temperature_2m")
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                temp = data["current"]["temperature_2m"]
                return f"{round(float(temp))}°C"
        except Exception:
            pass

    # Fallback to wttr.in
    try:
        wttr_loc = "" if location.lower() == "auto" else quote(location)
        url = f"https://wttr.in/{wttr_loc}?format=%t"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace").strip().lstrip("+")
            m = re.search(r"(-?\d+(?:\.\d+)?)\s*°?C?", text)
            if m:
                val = round(float(m.group(1)))
                return f"{val}°C"
    except Exception:
        pass

    return None


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #
def probe(cfg, ffprobe: str, channel: int, timeout: int = 10) -> dict:
    """Return {'ok', 'reason', 'video', 'audio', 'width', 'height'} for one channel."""
    info = {"ok": False, "reason": "", "video": None, "audio": None, "width": 0, "height": 0}
    cmd = [ffprobe, "-v", "error", "-rtsp_transport", "tcp",
           "-timeout", str(timeout * 1_000_000),
           "-analyzeduration", "5000000", "-probesize", "5000000",
           "-show_entries", "stream=codec_type,codec_name,width,height",
           "-of", "json", rtsp_url(cfg, channel)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15,
                             creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        info["reason"] = "timed out"
        return info
    if res.returncode != 0:
        err = res.stderr.strip().splitlines()
        info["reason"] = err[-1] if err else f"ffprobe exit {res.returncode}"
        return info
    for st in json.loads(res.stdout or "{}").get("streams", []):
        if st.get("codec_type") == "video" and not info["video"]:
            info.update(video=st.get("codec_name"), width=st.get("width") or 0,
                        height=st.get("height") or 0)
        elif st.get("codec_type") == "audio" and not info["audio"]:
            info["audio"] = st.get("codec_name")
    if not info["video"]:
        info["reason"] = "no video stream"
    elif not info["width"]:
        info["reason"] = ("video can't be decoded - turn off Hik-Connect "
                          "'Stream Encryption' on the DVR")
    else:
        info["ok"] = True
    return info


def scan_channels(cfg, ffprobe: str, channels, on_result) -> None:
    for ch in channels:
        on_result(ch, probe(cfg, ffprobe, ch, timeout=6))


# --------------------------------------------------------------------------- #
# Input: one DVR channel
# --------------------------------------------------------------------------- #
class InputSource:
    """Decodes one DVR channel to canvas-sized raw frames and 44.1 kHz PCM."""

    def __init__(self, engine: "Engine", channel: int):
        self.engine = engine
        self.channel = channel
        self.started = time.monotonic()
        self.latest: bytes | None = None
        self.frames = 0
        self.last_frame_t: float | None = None
        self.has_audio = False
        self.dead = False
        self.reason = ""
        self._stopped = False
        self._proc: subprocess.Popen | None = None
        self._srv: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._abuf = bytearray()
        self._alock = threading.Lock()
        self._primed = False
        self._tail = deque(maxlen=5)
        threading.Thread(target=self._run, daemon=True, name=f"src-{channel}").start()

    def stalled_for(self, now: float) -> float:
        return now - (self.last_frame_t or self.started)

    def pop_audio(self, n: int) -> bytes | None:
        """Up to n bytes of audio (zero-padded), or None while the jitter buffer fills."""
        with self._alock:
            if not self._primed:
                if len(self._abuf) < AUDIO_BPS // 5:   # wait for 200 ms of audio
                    return None
                self._primed = True
            avail = len(self._abuf) - len(self._abuf) % 4   # whole samples only
            take = min(n, avail)
            chunk = bytes(self._abuf[:take])
            del self._abuf[:take]
            if take < n:
                self._primed = False
                chunk += bytes(n - take)
            return chunk

    def stop(self) -> None:
        self._stopped = True
        threading.Thread(target=self._close, daemon=True).start()

    # ---- internals ----
    def _run(self) -> None:
        eng = self.engine
        has_audio = eng.audio_cache.get(self.channel)
        if has_audio is None:
            info = probe(eng.cfg, eng.ffprobe, self.channel)
            if self._stopped:
                return
            if not info["ok"]:
                return self._die(info["reason"])
            has_audio = bool(info["audio"])
            eng.audio_cache[self.channel] = has_audio
        self.has_audio = has_audio

        w, h = eng.canvas
        vf_filters = [
            f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black",
        ]
        osd = eng.cfg["osd"] if eng.cfg.has_section("osd") else {}
        if osd.get("enabled", "false").lower() in ("true", "1", "yes"):
            font_size = osd.get("font_size", "22")
            fontfile_param = "fontfile='C\\:/Windows/Fonts/arial.ttf':" if os.path.exists("C:/Windows/Fonts/arial.ttf") else ""
            if osd.get("show_channel", "true").lower() in ("true", "1", "yes"):
                ch_label = channel_label(eng.cfg, self.channel).replace(":", "\\:").replace("'", "\\'")
                vf_filters.append(
                    f"drawtext={fontfile_param}text='{ch_label}':fontcolor=white:fontsize={font_size}"
                    f":box=1:boxcolor=black@0.6:boxborderw=6:x=16:y=16"
                )
            show_time = osd.get("show_time", "true").lower() in ("true", "1", "yes")
            show_temp = osd.get("show_temp", "true").lower() in ("true", "1", "yes")
            if show_time or show_temp:
                if show_temp:
                    eng.update_osd_file()
                    escaped_osd = str(eng.osd_file).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
                    vf_filters.append(
                        f"drawtext={fontfile_param}textfile='{escaped_osd}':reload=1:fontcolor=white:fontsize={font_size}"
                        f":box=1:boxcolor=black@0.6:boxborderw=6:x=w-tw-16:y=16"
                    )
                else:
                    vf_filters.append(
                        f"drawtext={fontfile_param}text='%{{localtime}}':fontcolor=white:fontsize={font_size}"
                        f":box=1:boxcolor=black@0.6:boxborderw=6:x=w-tw-16:y=16"
                    )
        vf_filters.append("format=yuv420p")
        vf = ",".join(vf_filters)
        cmd = [eng.ffmpeg, "-hide_banner", "-loglevel", "error", "-max_error_rate", "1.0",
               "-rtsp_transport", "tcp", "-timeout", "10000000",
               "-analyzeduration", "5000000", "-probesize", "5000000",
               "-fflags", "+genpts+nobuffer", "-flags2", "+showall", "-err_detect", "ignore_err",
               "-i", rtsp_url(eng.cfg, self.channel),
               "-map", "0:v:0", "-vf", vf, "-fps_mode", "passthrough",
               "-f", "rawvideo", "pipe:1"]
        if has_audio:
            self._srv = socket.create_server(("127.0.0.1", 0))
            self._srv.settimeout(SWITCH_TIMEOUT)
            port = self._srv.getsockname()[1]
            cmd += ["-map", "0:a:0", "-af", "aresample=async=1:first_pts=0",
                    "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CHANNELS),
                    "-f", "s16le", f"tcp://127.0.0.1:{port}"]
        if self._stopped:
            return self._close()
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        except OSError as e:
            return self._die(str(e))
        if self._stopped:
            return self._close()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        if has_audio:
            threading.Thread(target=self._read_audio, daemon=True).start()
        self._read_video(w * h * 3 // 2)

    def _read_video(self, size: int) -> None:
        f = self._proc.stdout
        while not self._stopped:
            buf = f.read(size)
            if len(buf) < size:
                break
            self.latest = buf
            self.frames += 1
            self.last_frame_t = time.monotonic()
        if not self._stopped:
            self._proc.wait()
            detail = f": {self._tail[-1]}" if self._tail else ""
            self._die(f"stream ended (ffmpeg exit {self._proc.returncode}){detail}")

    def _read_audio(self) -> None:
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        self._conn = conn
        while True:
            try:
                data = conn.recv(65536)
            except OSError:
                break
            if not data:
                break
            with self._alock:
                self._abuf += data
                excess = len(self._abuf) - AUDIO_BPS      # cap at 1 s ...
                if excess > 0:
                    excess += AUDIO_BPS // 2              # ... by trimming to 0.5 s
                    del self._abuf[:excess - excess % 4]

    def _read_stderr(self) -> None:
        logged, window = 0, time.monotonic()
        for raw in self._proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            self._tail.append(line)
            now = time.monotonic()
            # Suppress benign transient decoder startup errors before initial IDR keyframe arrives
            if now - self.started < 4.0 and any(
                b in line for b in ("PPS id out of range", "Could not find ref", "Last message repeated")
            ):
                continue
            if now - window > 60:
                logged, window = 0, now
            if logged < 3:   # the DVR can be chatty; keep the log readable
                log.warning("CH%d: %s", self.channel, line)
                logged += 1

    def _die(self, reason: str) -> None:
        self.reason = reason
        self.dead = True
        self._close()

    def _close(self) -> None:
        p = self._proc
        if p and p.poll() is None:
            try:
                p.stdin.write(b"q")   # graceful quit so the DVR frees the RTSP session
                p.stdin.flush()
                p.wait(timeout=3)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                p.kill()
                try:
                    p.wait(timeout=2)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        for s in (self._conn, self._srv):
            if s:
                try:
                    s.close()
                except OSError:
                    pass


# --------------------------------------------------------------------------- #
# Output: one persistent connection to YouTube
# --------------------------------------------------------------------------- #
class OutputSink:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.t0: float | None = None
        self.last_progress: float | None = None
        self.failed = False
        self.handled = False
        self.reason = ""
        self.stats: dict[str, str] = {}
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._tail = deque(maxlen=10)
        self._last_out = None

    @property
    def alive(self) -> bool:
        return not self.failed and self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        eng, enc = self.engine, self.engine.cfg["encoder"]
        w, h = eng.canvas
        gop = str(eng.fps * enc.getint("keyframe_seconds", 2))
        rate = enc.get("video_bitrate", "3000k")
        codec = enc.get("video_codec", "libx264")
        preset = enc.get("preset", "").strip()

        with socket.socket() as s:   # pick a free port for ffmpeg's audio listener
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        cmd = [eng.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats",
               "-progress", "pipe:1",
               # input 0: PCM audio pushed by the engine
               "-f", "s16le", "-ar", str(AUDIO_RATE), "-ch_layout", "stereo",
               "-thread_queue_size", "1024", "-probesize", "32", "-analyzeduration", "0",
               "-i", f"tcp://127.0.0.1:{port}?listen=1",
               # input 1: raw frames pushed by the engine at a fixed rate
               "-f", "rawvideo", "-pix_fmt", "yuv420p", "-video_size", f"{w}x{h}",
               "-framerate", str(eng.fps), "-thread_queue_size", "64",
               "-probesize", "32", "-analyzeduration", "0", "-i", "pipe:0",
               "-map", "1:v", "-map", "0:a", "-c:v", codec]
        if codec == "libx264":
            cmd += ["-preset", preset or "veryfast",
                    # true CBR with filler so a static scene doesn't starve YouTube
                    "-x264-params", "nal-hrd=cbr:force-cfr=1"]
        elif codec == "h264_nvenc":
            cmd += ["-preset", preset if preset.startswith("p") else "p4", "-rc", "cbr"]
        elif codec == "h264_qsv":
            cmd += ["-preset", preset or "veryfast", "-look_ahead", "0"]
        elif codec == "h264_amf":
            cmd += ["-quality", "speed", "-rc", "cbr"]
        cmd += ["-profile:v", "high", "-pix_fmt", "yuv420p",
                "-b:v", rate, "-minrate", rate, "-maxrate", rate, "-bufsize", _double_rate(rate),
                "-g", gop, "-keyint_min", gop, "-sc_threshold", "0",
                "-c:a", "aac", "-b:a", enc.get("audio_bitrate", "128k"),
                "-f", "flv", "-flvflags", "no_duration_filesize", rtmp_url(eng.cfg)]

        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        threading.Thread(target=self._read_stderr, daemon=True).start()
        deadline = time.monotonic() + 15
        while True:
            if self._proc.poll() is not None:
                time.sleep(0.3)
                raise RuntimeError(self._tail[-1] if self._tail else "ffmpeg exited")
            try:
                self._sock = socket.create_connection(("127.0.0.1", port), timeout=1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    self.fail("ffmpeg audio input did not open")
                    raise RuntimeError(self.reason)
                time.sleep(0.2)
        self._sock.settimeout(10)
        self.t0 = time.monotonic()
        threading.Thread(target=self._read_progress, daemon=True).start()

    def write_video(self, frame: bytes) -> None:
        self._proc.stdin.write(frame)
        self._proc.stdin.flush()

    def write_audio(self, chunk: bytes) -> None:
        self._sock.sendall(chunk)

    def fail(self, reason: str) -> None:
        with self._lock:
            if self.failed:
                return
            self.failed = True
            self.reason = reason
        log.warning("YouTube output stopped: %s", reason)
        self._kill()

    def close(self) -> None:
        with self._lock:
            self.failed = True
        for f in (self._sock, self._proc.stdin if self._proc else None):
            try:
                f and f.close()   # EOF on both inputs lets ffmpeg finish the FLV cleanly
            except OSError:
                pass
        if self._proc:
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._kill()

    def _kill(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def _read_progress(self) -> None:
        for raw in self._proc.stdout:
            k, _, v = raw.decode("utf-8", "replace").strip().partition("=")
            self.stats[k] = v
            if k == "out_time_us" and v != self._last_out:
                self._last_out = v
                if self.last_progress is None:
                    log.info("LIVE on YouTube")
                self.last_progress = time.monotonic()
        self._proc.wait()
        if not self.failed:
            detail = f": {self._tail[-1]}" if self._tail else ""
            self.fail(f"ffmpeg exited (code {self._proc.returncode}){detail}")

    def _read_stderr(self) -> None:
        for raw in self._proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line and "deprecated pixel format" not in line:
                self._tail.append(line)
                log.warning("output: %s", line)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self, cfg: configparser.ConfigParser):
        self.cfg = cfg
        self.ffmpeg = find_tool(cfg, "ffmpeg")
        self.ffprobe = find_tool(cfg, "ffprobe")
        self.canvas = canvas_size(cfg)
        self.fps = cfg["encoder"].getint("fps", 24)
        sw = cfg["switching"]
        self.selected = sw.getint("channel", 1)
        self.mode = sw.get("mode", "off")
        self.rotate_seconds = max(5, sw.getint("rotate_seconds", 30))
        self.auto_channels = parse_channels(sw.get("auto_channels", ""))
        self.failover_after = max(3, sw.getint("failover_after", 10))
        self.failback_seconds = max(10, sw.getint("failback_seconds", 60))
        self.audio_enabled = cfg["audio"].getboolean("enabled", True)
        self.muted_channels = set(parse_channels(cfg["audio"].get("muted_channels", "")))

        self.audio_cache: dict[int, bool] = {}      # channel -> has an audio track
        self.channel_health: dict[int, str] = {}    # channel -> "ok" | "fail"
        self.active: InputSource | None = None
        self.pending: InputSource | None = None
        self.sink: OutputSink | None = None
        self.sink_status = "stopped"
        self.active_since = 0.0
        self.running = False
        self._request: int | None = None
        self._retry_at: dict[int, float] = {}
        self._fail_count: dict[int, int] = {}
        self._last_failback = 0.0
        self.osd_file = LOG_DIR / "osd_time_temp.txt"
        self.current_temp: str | None = None
        self._osd_lock = threading.Lock()
        self._weather_thread: threading.Thread | None = None
        set_secrets(cfg)
        self.update_osd_file()

    # ---- public controls (thread-safe enough: simple attribute swaps) ----
    def start(self) -> None:
        if not self.cfg["youtube"]["stream_key"].strip():
            raise ValueError("Set your YouTube stream key in Settings first.")
        self.running = True
        log.info("Starting: main channel %d, auto-switch %s, audio %s%s, output %dx%d@%d",
                 self.selected, self.mode, "on" if self.audio_enabled else "off",
                 f" (muted: {','.join(map(str, sorted(self.muted_channels)))})"
                 if self.muted_channels else "", *self.canvas, self.fps)
        for target in (self._control_loop, self._sink_loop, self._video_writer, self._audio_writer):
            threading.Thread(target=target, daemon=True, name=target.__name__).start()
        osd = self.cfg["osd"] if self.cfg.has_section("osd") else {}
        osd_on = osd.get("enabled", "false").lower() in ("true", "1", "yes")
        show_temp = osd.get("show_temp", "true").lower() in ("true", "1", "yes")
        if osd_on and show_temp and (not self._weather_thread or not self._weather_thread.is_alive()):
            self._weather_thread = threading.Thread(target=self._weather_loop, daemon=True, name="weather_loop")
            self._weather_thread.start()

    def stop(self) -> None:
        self.running = False
        for src in (self.pending, self.active):
            if src:
                src.stop()
        self.pending = self.active = None
        if self.sink:
            self.sink.close()
        self.sink_status = "stopped"
        log.info("Stopped.")

    def select_channel(self, ch: int) -> None:
        self.selected = ch
        self._request = ch

    def set_audio(self, on: bool) -> None:
        self.audio_enabled = on
        log.info("Audio %s", "ON" if on else "OFF (sending silence)")

    def set_channel_muted(self, ch: int, muted: bool) -> None:
        if muted:
            self.muted_channels = self.muted_channels | {ch}
        else:
            self.muted_channels = self.muted_channels - {ch}
        log.info("Channel %s audio %s", channel_label(self.cfg, ch), "MUTED" if muted else "unmuted")

    def audio_muted(self, ch: int | None) -> bool:
        """True when the given on-air channel must send silence instead of its audio."""
        return not self.audio_enabled or ch in self.muted_channels

    def set_switching(self, mode: str, rotate_seconds: int, channels: list[int]) -> None:
        changed = mode != self.mode
        self.mode = mode
        self.rotate_seconds = max(5, rotate_seconds)
        self.auto_channels = sorted(set(channels))
        if changed:
            self.active_since = time.monotonic()
            log.info("Auto-switching: %s", {"off": "off", "failover": "failover",
                                            "rotate": f"rotate every {self.rotate_seconds}s"}[mode])

    def update_osd_file(self) -> None:
        """Atomically update the badge text file read by FFmpeg drawtext."""
        osd = self.cfg["osd"] if self.cfg.has_section("osd") else {}
        show_time = osd.get("show_time", "true").lower() in ("true", "1", "yes")
        show_temp = osd.get("show_temp", "true").lower() in ("true", "1", "yes")

        parts = []
        if show_time:
            parts.append("%{localtime}")
        if show_temp and self.current_temp:
            parts.append(self.current_temp)

        content = "  ·  ".join(parts) if parts else " "

        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self.osd_file.with_suffix(".tmp")
            with self._osd_lock:
                tmp.write_text(content, encoding="utf-8")
                os.replace(tmp, self.osd_file)
        except OSError as e:
            log.warning("Failed to write OSD badge file: %s", e)

    def set_osd(self, on: bool) -> None:
        if not self.cfg.has_section("osd"):
            self.cfg.add_section("osd")
        self.cfg["osd"]["enabled"] = "true" if on else "false"
        self.update_osd_file()
        show_temp = self.cfg["osd"].get("show_temp", "true").lower() in ("true", "1", "yes")
        if on and show_temp and self.running and (not self._weather_thread or not self._weather_thread.is_alive()):
            self._weather_thread = threading.Thread(target=self._weather_loop, daemon=True, name="weather_loop")
            self._weather_thread.start()

    def _weather_loop(self) -> None:
        """Periodically fetch outdoor temperature and update the OSD badge."""
        while self.running:
            osd = self.cfg["osd"] if self.cfg.has_section("osd") else {}
            osd_on = osd.get("enabled", "false").lower() in ("true", "1", "yes")
            show_temp = osd.get("show_temp", "true").lower() in ("true", "1", "yes")
            if not (osd_on and show_temp):
                time.sleep(2)
                continue

            loc = osd.get("weather_location", "auto")
            temp = fetch_temperature(loc)
            if temp:
                self.current_temp = temp
                self.update_osd_file()
                log.info("OSD temperature updated: %s (location: %s)", temp, loc)
                sleep_s = 600
            else:
                sleep_s = 30  # retry sooner if fetch failed

            for _ in range(int(sleep_s)):
                if not self.running:
                    break
                time.sleep(1)

    def status(self) -> dict:
        now = time.monotonic()
        a, p, s = self.active, self.pending, self.sink
        st = {"running": self.running, "output": self.sink_status,
              "active": a.channel if a else None, "pending": p.channel if p else None,
              "has_audio": bool(a and a.has_audio), "audio": self.audio_enabled,
              "muted": bool(a and a.channel in self.muted_channels),
              "temp": self.current_temp,
              "selected": self.selected, "health": dict(self.channel_health),
              "uptime": 0, "bitrate": "", "speed": ""}
        if s and s.alive and s.t0:
            st.update(uptime=now - s.t0, bitrate=s.stats.get("bitrate", "").strip(),
                       speed=s.stats.get("speed", "").strip())
        return st

    # ---- channel control ----
    def _allowed(self, ch: int, now: float) -> bool:
        return now >= self._retry_at.get(ch, 0)

    def _mark_fail(self, ch: int, now: float, reason: str) -> None:
        n = self._fail_count.get(ch, 0) + 1
        self._fail_count[ch] = n
        delay = min(5 * 2 ** (n - 1), 60)
        self._retry_at[ch] = now + delay
        self.channel_health[ch] = "fail"
        log.warning("Channel %d failed: %s (retry in %ds)", ch, reason, delay)

    def _mark_ok(self, ch: int) -> None:
        self._fail_count[ch] = 0
        self._retry_at.pop(ch, None)
        self.channel_health[ch] = "ok"

    def _begin(self, ch: int, why: str) -> None:
        p = self.pending
        if p:
            if p.channel == ch:
                return
            p.stop()
        log.info("Switching to channel %s (%s)...", channel_label(self.cfg, ch), why)
        self.pending = InputSource(self, ch)

    def _next_channel(self, after: int, now: float) -> int | None:
        order = self.auto_channels or [self.selected]
        start = next((i for i, c in enumerate(order) if c > after), 0)
        for c in order[start:] + order[:start]:
            if c != after and self._allowed(c, now):
                return c
        return None

    def _control_loop(self) -> None:
        while self.running:
            try:
                self._control_step(time.monotonic())
            except Exception:
                log.exception("control loop error")
            time.sleep(0.25)

    def _control_step(self, now: float) -> None:
        # 1. Resolve a switch in progress: promote on first frame, give up on timeout
        p = self.pending
        if p:
            if p.frames > 0:
                old, self.active, self.pending = self.active, p, None
                self.active_since = now
                if p.channel != self.selected:
                    self._last_failback = now   # give the main channel time before retrying
                self._mark_ok(p.channel)
                log.info("ON AIR: channel %s%s", channel_label(self.cfg, p.channel),
                         "" if p.has_audio else " (camera has no audio)")
                if old and old is not p:
                    old.stop()
            elif p.dead or now - p.started > SWITCH_TIMEOUT:
                p.stop()
                self.pending = None
                self._mark_fail(p.channel, now, p.reason or f"no video within {SWITCH_TIMEOUT}s")

        # 2. User picked a channel
        req, self._request = self._request, None
        if req is not None:
            if not (self.active and self.active.channel == req):
                self._retry_at.pop(req, None)
                self._begin(req, "selected")
            elif self.pending:   # already on it: cancel any other switch in flight
                self.pending.stop()
                self.pending = None
            return

        # 3. Active channel lost: drop it (writers keep showing the last frame)
        a = self.active
        if a and (a.dead or a.stalled_for(now) > self.failover_after):
            self.active = None
            a.stop()
            self._mark_fail(a.channel, now,
                            a.reason or f"no video for {int(a.stalled_for(now))}s")
            log.info("Holding the last picture while recovering...")
            a = None

        if self.pending:
            return

        # 4. Nothing on air: start the main channel, or the next working one
        if a is None:
            if self._allowed(self.selected, now):
                self._begin(self.selected, "main channel")
            elif self.mode != "off":
                nxt = self._next_channel(self.selected, now)
                if nxt is not None:
                    self._begin(nxt, "failover")
            return

        # 5. Rotation / failback
        if self.mode == "rotate" and now - self.active_since >= self.rotate_seconds:
            nxt = self._next_channel(a.channel, now)
            if nxt is not None:
                self._begin(nxt, "rotation")
            else:
                self.active_since = now   # nothing else available right now
        elif (self.mode == "failover" and a.channel != self.selected
              and now - self._last_failback >= self.failback_seconds
              and self._allowed(self.selected, now)):
            self._last_failback = now
            self._begin(self.selected, "trying main channel again")

    # ---- output management ----
    def _sink_loop(self) -> None:
        backoff = 2
        retry_at = 0.0
        while self.running:
            now = time.monotonic()
            s = self.sink
            if s and s.alive:
                since = s.last_progress or s.t0
                if now - since > OUTPUT_STALL:
                    s.fail(f"no progress for {OUTPUT_STALL}s")
                else:
                    if s.last_progress and now - s.t0 > 60:
                        backoff = 2
                    self.sink_status = "live" if s.last_progress and now - s.last_progress < 5 else "connecting"
                time.sleep(0.5)
                continue
            if s and not s.handled:
                s.handled = True
                retry_at = now + backoff
                backoff = min(backoff * 2, 60)
            if now < retry_at:
                self.sink_status = f"reconnecting in {int(retry_at - now) + 1}s"
                time.sleep(0.5)
                continue
            self.sink_status = "connecting"
            log.info("Connecting to YouTube...")
            new = OutputSink(self)
            try:
                new.start()
                self.sink = new
            except Exception as e:
                log.error("Could not start YouTube output: %s", e)
                new.handled = True
                retry_at = time.monotonic() + backoff
                backoff = min(backoff * 2, 60)

    def _wait_sink(self) -> OutputSink | None:
        s = self.sink
        return s if s and s.alive and s.t0 else None

    def _video_writer(self) -> None:
        w, h = self.canvas
        last = bytes([16]) * (w * h) + bytes([128]) * (w * h // 2)   # black until a camera is up
        cur, n = None, 0
        while self.running:
            s = self._wait_sink()
            if not s:
                time.sleep(0.05)
                continue
            if s is not cur:
                cur, n = s, 0
            delay = s.t0 + n / self.fps - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif -delay > 3 and time.monotonic() - s.t0 > 15:
                s.fail("video fell more than 3 s behind real time (upload too slow?)")
                continue
            a = self.active
            if a and a.latest is not None:
                last = a.latest
            try:
                s.write_video(last)
            except (OSError, ValueError) as e:
                s.fail(f"video pipe closed ({e})")
            n += 1

    def _audio_writer(self) -> None:
        cur, k = None, 0
        while self.running:
            s = self._wait_sink()
            if not s:
                time.sleep(0.05)
                continue
            if s is not cur:
                cur, k = s, 0
            delay = s.t0 + k * AUDIO_TICK - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            a = self.active
            data = a.pop_audio(AUDIO_CHUNK) if a and a.has_audio else None
            if data is None or self.audio_muted(a.channel):
                data = SILENCE   # popping anyway keeps the camera's buffer fresh
            try:
                s.write_audio(data)
            except OSError as e:
                s.fail(f"audio socket closed ({e})")
            k += 1


# --------------------------------------------------------------------------- #
# CLI entry point (headless runner / background service)
# --------------------------------------------------------------------------- #
def main() -> None:
    import argparse
    import signal

    parser = argparse.ArgumentParser(description="DVR -> YouTube Live streaming engine")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="Path to config.ini")
    parser.add_argument("--channel", type=int, help="Override active starting channel")
    parser.add_argument("--mode", choices=["off", "failover", "rotate"], help="Override switching mode")
    parser.add_argument("--rotate-seconds", type=int, help="Rotation period in seconds")
    parser.add_argument("--no-audio", action="store_true", help="Force mute / stream silence")
    parser.add_argument("--mute-channels", help="Comma-separated channels to stream without audio")
    parser.add_argument("--no-temp", action="store_true", help="Disable temperature overlay in OSD")
    parser.add_argument("--weather-location", help="Weather location (city name, lat,lon, or 'auto')")
    parser.add_argument("--probe", action="store_true", help="Probe all channels and exit")
    args = parser.parse_args()

    cfg = load_settings(args.config)
    setup_logging(logging.StreamHandler(sys.stdout))
    set_secrets(cfg)

    if args.channel:
        cfg["switching"]["channel"] = str(args.channel)
    if args.mode:
        cfg["switching"]["mode"] = args.mode
    if args.rotate_seconds:
        cfg["switching"]["rotate_seconds"] = str(args.rotate_seconds)
    if args.no_audio:
        cfg["audio"]["enabled"] = "false"
    if args.mute_channels is not None:
        cfg["audio"]["muted_channels"] = ",".join(map(str, parse_channels(args.mute_channels)))
    if args.no_temp:
        if not cfg.has_section("osd"):
            cfg.add_section("osd")
        cfg["osd"]["show_temp"] = "false"
    if args.weather_location:
        if not cfg.has_section("osd"):
            cfg.add_section("osd")
        cfg["osd"]["weather_location"] = args.weather_location

    if args.probe:
        try:
            ffprobe = find_tool(cfg, "ffprobe")
        except RuntimeError as e:
            sys.exit(str(e))
        n = cfg["dvr"].getint("channels", 8)
        log.info("Probing %d channels...", n)
        for ch in range(1, n + 1):
            lbl = channel_label(cfg, ch)
            res = probe(cfg, ffprobe, ch, timeout=8)
            if res["ok"]:
                log.info("CH %-18s: OK - %s %dx%d, audio=%s", lbl,
                         res["video"].upper() if res["video"] else "UNKNOWN",
                         res["width"], res["height"], res["audio"] or "none")
            else:
                log.warning("CH %-18s: NO SIGNAL (%s)", lbl, res["reason"])
        return

    engine = Engine(cfg)
    stop_event = threading.Event()

    def on_signal(signum, _frame):
        log.info("Signal %s received, stopping engine...", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, on_signal)

    try:
        engine.start()
        while not stop_event.is_set():
            stop_event.wait(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
