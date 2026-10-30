"""
DVR -> YouTube Live desktop app.

Double-click to open (runs without a console window), or:
    pythonw dvr_stream_app.pyw [--autostart] [--minimized]

--autostart starts streaming immediately (use it from Task Scheduler at logon).
"""

import argparse
import logging
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import stream_engine as se

log = se.log

# channel button colours: (background, foreground)
COLORS = {
    "live": ("#2e7d32", "white"),
    "switching": ("#f9a825", "black"),
    "ok": ("#c8e6c9", "black"),
    "fail": ("#ef9a9a", "black"),
    "nosignal": ("#bdbdbd", "#555555"),
    None: ("#f0f0f0", "black"),
}
INGESTS = ["rtmps://a.rtmps.youtube.com:443/live2", "rtmp://a.rtmp.youtube.com/live2"]
DEFAULT_PRESETS = {"h264_qsv": "veryfast", "libx264": "veryfast", "h264_nvenc": "p4", "h264_amf": ""}


class QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(self.format(record))


def fmt_uptime(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:d}:{sec // 60 % 60:02d}:{sec % 60:02d}"


class App(tk.Tk):
    def __init__(self, args):
        super().__init__()
        self.title("DVR → YouTube Live")
        self.minsize(620, 600)
        self.cfg = se.load_settings()
        self.engine: se.Engine | None = None
        self.scan_results: dict[int, str] = {}
        self.logq: queue.Queue = queue.Queue()
        se.setup_logging(QueueHandler(self.logq))
        se.set_secrets(self.cfg)

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(300, self._tick)
        if args.autostart:
            self.after(500, self.start)
        if args.minimized:
            self.iconify()

    # ------------------------------------------------------------------ UI --
    def _build(self):
        pad = {"padx": 10, "pady": 5}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        self.status_lbl = tk.Label(top, text="● STOPPED", font=("Segoe UI", 15, "bold"), fg="#757575")
        self.status_lbl.pack(side="left")
        self.detail_lbl = ttk.Label(top, text="", font=("Segoe UI", 10))
        self.detail_lbl.pack(side="left", padx=14)

        bar = ttk.Frame(self)
        bar.pack(fill="x", **pad)
        self.start_btn = ttk.Button(bar, text="▶  Start streaming", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="■  Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(bar, text="Settings…", command=self.open_settings).pack(side="right")
        self.scan_btn = ttk.Button(bar, text="Scan channels", command=self.scan)
        self.scan_btn.pack(side="right", padx=6)

        self.ch_frame = ttk.LabelFrame(self, text="Channel on air  (click to switch · ★ = main channel)")
        self.ch_frame.pack(fill="x", **pad)

        of = ttk.LabelFrame(self, text="Audio & On-Screen Overlay")
        of.pack(fill="x", **pad)
        row_ao = ttk.Frame(of)
        row_ao.pack(fill="x", padx=6, pady=4)
        self.audio_var = tk.BooleanVar(value=self.cfg["audio"].getboolean("enabled", True))
        ttk.Checkbutton(row_ao, text="Send camera audio to YouTube",
                        variable=self.audio_var, command=self.on_audio).pack(side="left")
        osd_sec = self.cfg["osd"] if self.cfg.has_section("osd") else {}
        self.osd_var = tk.BooleanVar(value=osd_sec.getboolean("enabled", False) if hasattr(osd_sec, "getboolean") else False)
        ttk.Checkbutton(row_ao, text="Burn camera name & live clock onto video (OSD)",
                        variable=self.osd_var, command=self.on_osd).pack(side="left", padx=20)

        sf = ttk.LabelFrame(self, text="Auto-switching")
        sf.pack(fill="x", **pad)
        sw = self.cfg["switching"]
        self.mode_var = tk.StringVar(value=sw.get("mode", "off"))
        ttk.Radiobutton(sf, text="Off – stay on the selected channel (reconnects it if it drops)",
                        value="off", variable=self.mode_var, command=self.on_switching
                        ).pack(anchor="w", padx=6, pady=(4, 0))
        ttk.Radiobutton(sf, text="Failover – if the channel fails, go to the next working one, "
                                 "return when it recovers",
                        value="failover", variable=self.mode_var, command=self.on_switching
                        ).pack(anchor="w", padx=6)
        row = ttk.Frame(sf)
        row.pack(anchor="w", padx=6)
        ttk.Radiobutton(row, text="Rotate – cycle through the channels every", value="rotate",
                        variable=self.mode_var, command=self.on_switching).pack(side="left")
        self.rot_var = tk.StringVar(value=sw.get("rotate_seconds", "30"))
        spin = ttk.Spinbox(row, from_=5, to=3600, increment=5, width=6, textvariable=self.rot_var,
                           command=self.on_switching)
        spin.pack(side="left", padx=4)
        spin.bind("<FocusOut>", self.on_switching)
        spin.bind("<Return>", self.on_switching)
        ttk.Label(row, text="seconds").pack(side="left")
        ttk.Label(sf, text="Channels used for failover / rotation:").pack(anchor="w", padx=6, pady=(6, 0))
        self.auto_frame = ttk.Frame(sf)
        self.auto_frame.pack(anchor="w", padx=6, pady=(0, 6))

        lf = ttk.LabelFrame(self, text="Activity")
        lf.pack(fill="both", expand=True, **pad)
        self.log_txt = ScrolledText(lf, height=10, state="disabled", font=("Consolas", 9), wrap="word")
        self.log_txt.pack(fill="both", expand=True, padx=4, pady=4)

        self._build_channels()

    def _build_channels(self):
        for frame in (self.ch_frame, self.auto_frame):
            for w in frame.winfo_children():
                w.destroy()
        n = max(1, self.cfg["dvr"].getint("channels", 8))
        self.ch_btns = {}
        for ch in range(1, n + 1):
            b = tk.Button(self.ch_frame, text=str(ch), width=5, font=("Segoe UI", 11, "bold"),
                          relief="raised", bd=2, command=lambda c=ch: self.select_channel(c))
            b.grid(row=(ch - 1) // 8, column=(ch - 1) % 8, padx=4, pady=5)
            self.ch_btns[ch] = b
        ttk.Label(self.ch_frame, text="green = on air   yellow = switching   red = failed   "
                                      "grey = no signal", foreground="#666666"
                  ).grid(row=(n - 1) // 8 + 1, column=0, columnspan=8, sticky="w", padx=4, pady=(0, 4))
        chosen = set(se.parse_channels(self.cfg["switching"].get("auto_channels", "")))
        self.auto_vars = {}
        for ch in range(1, n + 1):
            v = tk.BooleanVar(value=ch in chosen)
            ttk.Checkbutton(self.auto_frame, text=str(ch), variable=v, command=self.on_switching
                            ).grid(row=(ch - 1) // 8, column=(ch - 1) % 8, padx=4)
            self.auto_vars[ch] = v

    # ------------------------------------------------------------- actions --
    def _save(self):
        try:
            se.save_settings(self.cfg)
        except OSError as e:
            log.error("Could not save settings: %s", e)

    def start(self):
        if self.engine:
            return
        if not self.cfg["youtube"]["stream_key"].strip():
            messagebox.showwarning("Stream key needed", "Enter your YouTube stream key in Settings.")
            self.open_settings()
            return
        try:
            eng = se.Engine(self.cfg)
            eng.start()
        except Exception as e:
            messagebox.showerror("Cannot start", str(e))
            return
        self.engine = eng
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

    def stop(self):
        eng, self.engine = self.engine, None
        if eng:
            threading.Thread(target=eng.stop, daemon=True).start()
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

    def select_channel(self, ch: int):
        self.cfg["switching"]["channel"] = str(ch)
        self._save()
        if self.engine:
            self.engine.select_channel(ch)
        else:
            log.info("Main channel set to %d", ch)

    def on_audio(self):
        on = self.audio_var.get()
        self.cfg["audio"]["enabled"] = "true" if on else "false"
        self._save()
        if self.engine:
            self.engine.set_audio(on)
        else:
            log.info("Audio will be %s", "ON" if on else "OFF")

    def on_osd(self):
        on = self.osd_var.get()
        if not self.cfg.has_section("osd"):
            self.cfg.add_section("osd")
        self.cfg["osd"]["enabled"] = "true" if on else "false"
        self._save()
        log.info("On-Screen Display (OSD) set to %s (applies on next channel switch)", "ON" if on else "OFF")

    def on_switching(self, *_):
        mode = self.mode_var.get()
        try:
            secs = max(5, int(self.rot_var.get()))
        except ValueError:
            secs = 30
            self.rot_var.set("30")
        chans = [c for c, v in self.auto_vars.items() if v.get()]
        if mode != "off" and len(chans) < 2:
            log.warning("Tick at least two channels for failover/rotation to have somewhere to go.")
        sw = self.cfg["switching"]
        changed = (sw["mode"], sw["rotate_seconds"], sw["auto_channels"]) != \
                  (mode, str(secs), ",".join(map(str, chans)))
        sw["mode"], sw["rotate_seconds"], sw["auto_channels"] = mode, str(secs), ",".join(map(str, chans))
        if changed:
            self._save()
            if self.engine:
                self.engine.set_switching(mode, secs, chans)

    def scan(self):
        try:
            ffprobe = se.find_tool(self.cfg, "ffprobe")
        except RuntimeError as e:
            messagebox.showerror("FFmpeg missing", str(e))
            return
        self.scan_btn.config(state="disabled")
        n = self.cfg["dvr"].getint("channels", 8)
        log.info("Scanning %d channels (takes a few seconds each)...", n)

        def on_result(ch, info):
            if info["ok"]:
                self.scan_results[ch] = "ok"
                log.info("CH%d: %s %dx%d, %s", ch, info["video"].upper(), info["width"], info["height"],
                         "audio" if info["audio"] else "no audio")
            else:
                self.scan_results[ch] = "nosignal"
                log.info("CH%d: no signal (%s)", ch, info["reason"])

        def run():
            se.scan_channels(self.cfg, ffprobe, range(1, n + 1), on_result)
            log.info("Scan finished.")
            self.after(0, lambda: self.scan_btn.config(state="normal"))

        threading.Thread(target=run, daemon=True).start()

    def open_settings(self):
        SettingsDialog(self)

    def settings_saved(self, channels_changed: bool):
        se.set_secrets(self.cfg)
        if channels_changed:
            self._build_channels()
        if self.engine:
            messagebox.showinfo("Settings saved",
                                "DVR, YouTube and encoder changes apply the next time you start streaming.")

    # -------------------------------------------------------------- refresh --
    def _tick(self):
        lines = []
        while not self.logq.empty():
            lines.append(self.logq.get_nowait())
        if lines:
            t = self.log_txt
            t.config(state="normal")
            t.insert("end", "\n".join(lines) + "\n")
            excess = int(t.index("end-1c").split(".")[0]) - 1000
            if excess > 0:
                t.delete("1.0", f"{excess}.0")
            t.see("end")
            t.config(state="disabled")

        selected = self.cfg["switching"].getint("channel", 1)
        if self.engine:
            st = self.engine.status()
            out = st["output"]
            if out == "live" and st["active"]:
                self.status_lbl.config(text="● LIVE", fg="#c62828")
            elif out == "live":
                self.status_lbl.config(text="● LIVE (holding picture)", fg="#ef6c00")
            else:
                self.status_lbl.config(text=f"● {out.upper()}", fg="#ef6c00")
            ch_str = se.channel_label(self.cfg, st["active"]) if st["active"] else "–"
            parts = [f"Channel {ch_str}"]
            if st["pending"]:
                parts.append(f"switching to {se.channel_label(self.cfg, st['pending'])}…")
            if st["bitrate"]:
                parts.append(st["bitrate"].replace("kbits/s", " kbps"))
            if st["uptime"]:
                parts.append(f"up {fmt_uptime(st['uptime'])}")
            if not st["audio"]:
                parts.append("audio OFF")
            elif st["active"] and not st["has_audio"]:
                parts.append("camera has no audio")
            self.detail_lbl.config(text="  ·  ".join(parts))
            health, active, pending = st["health"], st["active"], st["pending"]
        else:
            self.status_lbl.config(text="● STOPPED", fg="#757575")
            self.detail_lbl.config(text="")
            health, active, pending = {}, None, None

        for ch, b in self.ch_btns.items():
            state = ("live" if ch == active else "switching" if ch == pending
                     else health.get(ch) or self.scan_results.get(ch))
            bg, fg = COLORS[state]
            b.config(bg=bg, fg=fg, activebackground=bg,
                     text=f"{ch} ★" if ch == selected else str(ch),
                     relief="sunken" if ch == active else "raised")
        self.after(500, self._tick)

    def _on_close(self):
        if self.engine:
            if not messagebox.askyesno("Exit", "Streaming is running. Stop it and exit?"):
                return
            self.engine.stop()
        self.destroy()

    def report_callback_exception(self, exc, val, tb):
        log.error("UI error", exc_info=(exc, val, tb))
        messagebox.showerror("Error", str(val))


class SettingsDialog(tk.Toplevel):
    FIELDS = [
        ("dvr", "host", "DVR IP address", "entry", None),
        ("dvr", "port", "RTSP port", "entry", None),
        ("dvr", "username", "DVR username", "entry", None),
        ("dvr", "password", "DVR password", "secret", None),
        ("dvr", "channels", "Number of channels", "combo", ["4", "8", "16", "32"]),
        ("dvr", "stream", "Camera stream", "combo", ["main", "sub"]),
        ("youtube", "stream_key", "YouTube stream key", "secret", None),
        ("youtube", "ingest_url", "YouTube server", "combo", INGESTS),
        ("encoder", "canvas", "Output size", "combo", ["1920x1080", "1280x720", "1080x1920", "1488x1664"]),
        ("encoder", "video_bitrate", "Video bitrate", "combo", ["1500k", "2500k", "3000k", "4500k", "6000k"]),
        ("encoder", "video_codec", "Encoder", "combo", list(DEFAULT_PRESETS)),
        ("osd", "enabled", "Burn camera name/clock on stream (OSD)", "combo", ["false", "true"]),
        ("switching", "failover_after", "Channel fails after (s without video)", "entry", None),
        ("switching", "failback_seconds", "Failover: retry main channel every (s)", "entry", None),
    ]

    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.title("Settings")
        self.resizable(False, False)
        self.transient(app)
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both")
        self.vars, self.secret_entries = {}, []
        for i, (sec, key, label, kind, opts) in enumerate(self.FIELDS):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", pady=3, padx=(0, 10))
            var = tk.StringVar(value=app.cfg[sec].get(key, ""))
            if kind == "combo":
                w = ttk.Combobox(frm, textvariable=var, values=opts, width=40)
            else:
                w = ttk.Entry(frm, textvariable=var, width=43, show="•" if kind == "secret" else "")
                if kind == "secret":
                    self.secret_entries.append(w)
            w.grid(row=i, column=1, sticky="w", pady=3)
            self.vars[(sec, key)] = var
        self.show_var = tk.BooleanVar()
        ttk.Checkbutton(frm, text="Show password and stream key", variable=self.show_var,
                        command=lambda: [e.config(show="" if self.show_var.get() else "•")
                                         for e in self.secret_entries]
                        ).grid(row=len(self.FIELDS), column=1, sticky="w", pady=(4, 8))
        btns = ttk.Frame(frm)
        btns.grid(row=len(self.FIELDS) + 1, column=0, columnspan=2, sticky="e")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save", command=self.save).pack(side="right", padx=6)
        self.grab_set()

    def save(self):
        vals = {k: v.get().strip() for k, v in self.vars.items()}
        try:
            for key in [("dvr", "port"), ("dvr", "channels"),
                        ("switching", "failover_after"), ("switching", "failback_seconds")]:
                if int(vals[key]) <= 0:
                    raise ValueError
            w, h = (int(x) for x in vals[("encoder", "canvas")].lower().split("x"))
            if w < 16 or h < 16:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid value",
                                 "Port, channel count and timings must be positive numbers; "
                                 "output size must look like 1920x1080.", parent=self)
            return
        cfg = self.app.cfg
        old_channels = cfg["dvr"].get("channels")
        old_codec = cfg["encoder"].get("video_codec")
        for (sec, key), val in vals.items():
            cfg[sec][key] = val
        if cfg["encoder"]["video_codec"] != old_codec:
            cfg["encoder"]["preset"] = DEFAULT_PRESETS.get(cfg["encoder"]["video_codec"], "")
        self.app._save()
        self.destroy()
        log.info("Settings saved.")
        self.app.settings_saved(cfg["dvr"]["channels"] != old_channels)


def main():
    ap = argparse.ArgumentParser(description="DVR -> YouTube Live")
    ap.add_argument("--autostart", action="store_true", help="start streaming on launch")
    ap.add_argument("--minimized", action="store_true", help="start minimized")
    args = ap.parse_args()
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp text on high-DPI screens
        except Exception:
            pass
    App(args).mainloop()


if __name__ == "__main__":
    main()
