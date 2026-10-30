# DVR to YouTube Live Stream Relay 2.0

A resilient, multi-channel RTSP-to-YouTube streaming bridge engineered for continuous 24/7 CCTV broadcasting.

---

## 🏛 Architecture Overview

Traditional FFmpeg relays pipe a single RTSP stream directly into an RTMP upload. If the camera reboots, Wi-Fi drops, or the channel switches, FFmpeg exits and YouTube terminates the live broadcast.

This engine decouples camera acquisition from YouTube broadcast via a three-tier architecture:

```
[DVR / NVR RTSP]
       │
       ▼
 [InputSource]   ── (Decodes H.265/H.264 over TCP -> Raw YUV420p & 44.1kHz PCM)
       │
       ▼
  [Engine]       ── (Monotonic real-time clocked writer threads @ 24/30 fps;
       │             holds last good frame if camera drops, injects silence/resampled audio)
       ▼
 [OutputSink]    ── (One persistent FFmpeg encoder pushing H.264 + AAC -> YouTube RTMPS)
```

### Key Architectural Strengths
- **Seamless Channel Switching:** Switching cameras, rotating channels, or handling camera dropouts happens internally in memory—the YouTube uplink never disconnects.
- **Freeze-Frame Fault Recovery:** If a camera drops out, viewers see the last valid frame (or black fallback) while the engine auto-reconnects with exponential backoff.
- **Monotonic A/V Clock:** Real-time clock writer threads prevent timestamp jitter, FLV muxer crashes, or audio desynchronization.
- **Hardware Acceleration Ready:** Supports Intel QuickSync (`h264_qsv`), NVIDIA NVENC (`h264_nvenc`), and AMD AMF (`h264_amf`) to offload CPU encoding.

---

## 🚀 Quick Start

### 1. Requirements
- **OS:** Windows 10/11 (or Linux with minor launcher adaptation)
- **Python:** Python 3.10+
- **FFmpeg:** FFmpeg 6.0+ (Gyan.dev full build recommended) with `h264_qsv` / `libx264`

### 2. Configuration
Copy the template configuration and customize your camera credentials and YouTube stream key:
```powershell
copy config.example.ini config.ini
notepad config.ini
```

### 3. Launching
- **Desktop GUI:**
  ```cmd
  start_stream.bat --gui
  # or double-click "DVR Stream.lnk"
  ```
- **Headless / Console Mode (Task Scheduler / Windows Service):**
  ```cmd
  start_stream.bat
  # or directly:
  python stream_engine.py
  ```
- **Channel Probe / Network Audit:**
  ```cmd
  python stream_engine.py --probe
  ```
- **Packet Gap & Jitter Diagnostic:**
  ```cmd
  python dvr_gap_test.py --channel 7 --duration 30
  ```

---

## ⚡ Performance Optimization: Intel QuickSync (QSV)

If your system features an Intel processor with integrated graphics (e.g. **Intel Iris Xe Graphics**), CPU utilization can be reduced by up to **80%** by enabling Intel QSV:

In `config.ini`:
```ini
[encoder]
video_codec      = h264_qsv
preset           = veryfast
fps              = 24
keyframe_seconds = 2
video_bitrate    = 2500k
audio_bitrate    = 128k
canvas           = 1920x1080
```

---

## 🏷 Human-Readable Camera Names

You can label camera channels so they appear clearly in logs, status bars, and diagnostics:

In `config.ini`:
```ini
[channel_names]
1 = Front Gate
2 = Driveway
3 = Backyard
4 = Living Room
5 = Garage
6 = Front Porch
7 = Side Alley
8 = Terrace
```

---

## 📺 On-Screen Display (OSD) Overlay

The relay can automatically burn the camera name (top-left) and live timestamp clock (top-right) with clean semi-transparent badges directly onto the video:

In `config.ini`:
```ini
[osd]
enabled      = true
show_channel = true
show_time    = true
font_size    = 22
```

You can also toggle OSD dynamically with one click in the desktop GUI app (`start_stream.bat --gui`).

---

## 🛡 Security & Operational Safety

1. **Keep `config.ini` Private:** `config.ini` is excluded by `.gitignore` to prevent committing passwords and stream keys to Git.
2. **Log Redaction:** All passwords and stream keys are masked with `****` in console logs, file logs, and diagnostic output.
3. **URL Encoding:** Special characters in passwords (such as `@`, `%`, `#`) are automatically percent-encoded to prevent malformed RTSP URLs.

---

## 🧪 Automated Testing

Run the test suite using `pytest`:
```powershell
python -m pytest tests/test_stream_engine.py -v
```

---

## 🛠 Troubleshooting Matrix

| Issue | Root Cause | Solution |
| :--- | :--- | :--- |
| **"Resolution unknown / cannot decode"** | Hikvision Smart Codec / H.265+ enabled | Disable H.264+/H.265+ on DVR web UI (use standard H.264/H.265), or set `stream = sub` in `config.ini`. |
| **YouTube warns "Not receiving enough video"** | VBR bitrate dropping during static CCTV night scenes | The engine enforces true CBR with filler NAL units (`nal-hrd=cbr:force-cfr=1`). Ensure `video_bitrate >= 2500k`. |
| **Channel switches show transient HEVC warnings** | Delayed IDR/SPS packets from DVR mid-stream | Normal behavior for RTSP over TCP. Engine suppresses benign startup noise and recovers within 1 keyframe interval. |
| **High CPU usage** | Software encoder `libx264` active on 1080p | Change `video_codec = h264_qsv` in `config.ini` on Intel systems or `h264_nvenc` on NVIDIA. |
