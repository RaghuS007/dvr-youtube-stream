#!/usr/bin/env python3
"""
Diagnostic utility: Read a DVR RTSP channel to null sink for N seconds
and report any frame delivery gaps, jitter, or stalls.

Usage:
    python dvr_gap_test.py                     # tests active channel from config.ini for 30s
    python dvr_gap_test.py --channel 2         # tests channel 2
    python dvr_gap_test.py --url rtsp://...    # tests an explicit RTSP URL
    python dvr_gap_test.py -t 60 --threshold 2.0
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

import stream_engine as se

APP_DIR = Path(__file__).resolve().parent


def parse_args():
    parser = argparse.ArgumentParser(description="Test DVR RTSP stream for packet loss and frame gaps")
    parser.add_argument("--config", type=Path, default=APP_DIR / "config.ini", help="Path to config.ini")
    parser.add_argument("-c", "--channel", type=int, help="Channel number to test")
    parser.add_argument("-u", "--url", type=str, help="Direct RTSP URL (overrides channel/config)")
    parser.add_argument("-t", "--duration", type=int, default=30, help="Test duration in seconds (default: 30)")
    parser.add_argument("--threshold", type=float, default=2.0, help="Gap warning threshold in seconds (default: 2.0)")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.url:
        rtsp_url = args.url
        channel_label = "Custom URL"
    else:
        cfg = se.load_settings(args.config)
        channel = args.channel or cfg["switching"].getint("channel", 1)
        rtsp_url = se.rtsp_url(cfg, channel)
        channel_label = se.channel_label(cfg, channel)

    print(f"Testing stream for channel {channel_label} ({args.duration}s)...")
    print(f"Target RTSP URL: {se.redact(rtsp_url)}")

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
        "-rtsp_transport", "tcp", "-timeout", "10000000",
        "-flags2", "+showall", "-err_detect", "ignore_err", "-i", rtsp_url,
        "-map", "0:v:0", "-t", str(args.duration), "-f", "null", "-"
    ]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        sys.exit("Error: FFmpeg is not installed or not found on PATH.")

    state = {
        "frame": 0,
        "t": time.monotonic(),
        "gaps": 0,
        "max_gap": 0.0,
    }
    start = time.monotonic()

    def reader():
        for line in proc.stdout:
            k, _, v = line.strip().partition("=")
            if k == "frame" and v.isdigit():
                frame_num = int(v)
                if frame_num > state["frame"]:
                    now = time.monotonic()
                    gap = now - state["t"]
                    if state["frame"] > 0 and gap > args.threshold:
                        state["gaps"] += 1
                        state["max_gap"] = max(state["max_gap"], gap)
                        print(f"[{time.strftime('%H:%M:%S')}] WARNING: Frame gap of {gap:5.2f}s before frame {frame_num}", flush=True)
                    state["frame"] = frame_num
                    state["t"] = now

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    while proc.poll() is None:
        time.sleep(1)
        if time.monotonic() - state["t"] > 45:
            print(f"[{time.strftime('%H:%M:%S')}] ERROR: Stream stalled >45s. Terminating FFmpeg.", flush=True)
            proc.kill()
            break

    el = time.monotonic() - start
    frames = state["frame"]
    avg_fps = frames / el if el > 0 else 0

    print("-" * 60)
    print(f"Result for {channel_label}:")
    print(f"  Frames decoded : {frames}")
    print(f"  Elapsed time   : {el:.1f}s")
    print(f"  Average FPS    : {avg_fps:.2f} fps")
    print(f"  Gaps detected  : {state['gaps']} (max gap: {state['max_gap']:.2f}s)")
    print(f"  Exit code      : {proc.returncode}")
    print("-" * 60)


if __name__ == "__main__":
    main()
