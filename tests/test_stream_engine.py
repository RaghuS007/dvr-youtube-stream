"""
Unit and integration tests for stream_engine module.
Validates URL formatting, credential encoding, channel parsing,
bitrate scaling, settings management, and secret redaction.
"""

from __future__ import annotations

import types
import configparser
from pathlib import Path
import pytest

import stream_engine as se


def test_parse_channels():
    assert se.parse_channels("1,2,3,4,5,6,7,8") == [1, 2, 3, 4, 5, 6, 7, 8]
    assert se.parse_channels(" 8, 3, 1, 3, 5 ") == [1, 3, 5, 8]
    assert se.parse_channels("") == []
    assert se.parse_channels("foo, -1, 0, 4, bar, 7") == [4, 7]


def test_canvas_size():
    cfg = configparser.ConfigParser()
    cfg.add_section("encoder")

    cfg["encoder"]["canvas"] = "1920x1080"
    assert se.canvas_size(cfg) == (1920, 1080)

    # Test odd dimensions are rounded down to even for YUV420p
    cfg["encoder"]["canvas"] = "1921x1081"
    assert se.canvas_size(cfg) == (1920, 1080)

    cfg["encoder"]["canvas"] = "1280x720"
    assert se.canvas_size(cfg) == (1280, 720)


def test_double_rate():
    assert se._double_rate("3000k") == "6000k"
    assert se._double_rate("2500k") == "5000k"
    assert se._double_rate("2.5m") == "5000k"
    assert se._double_rate("4000") == "8k"


def test_rtsp_url_encoding():
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("dvr")
    cfg["dvr"]["host"] = "192.168.1.100"
    cfg["dvr"]["port"] = "554"
    cfg["dvr"]["username"] = "admin"
    cfg["dvr"]["password"] = "Pass@123%#Safe"
    cfg["dvr"]["stream"] = "main"

    url = se.rtsp_url(cfg, channel=1)
    # The '@' in password MUST be encoded as '%40' so it does not corrupt the RTSP host delimiter
    assert "Pass%40123%25%23Safe@" in url
    assert "rtsp://admin:Pass%40123%25%23Safe@192.168.1.100:554/Streaming/Channels/101" == url

    # Test sub stream (channel 2 sub = 202)
    cfg["dvr"]["stream"] = "sub"
    url_sub = se.rtsp_url(cfg, channel=2)
    assert url_sub.endswith("/Streaming/Channels/202")


def test_rtmp_url():
    cfg = configparser.ConfigParser()
    cfg.add_section("youtube")
    cfg["youtube"]["ingest_url"] = "rtmps://a.rtmps.youtube.com:443/live2/"
    cfg["youtube"]["stream_key"] = "  abcd-1234-wxyz-7890  "

    url = se.rtmp_url(cfg)
    assert url == "rtmps://a.rtmps.youtube.com:443/live2/abcd-1234-wxyz-7890"


def test_redact():
    secrets = ["SuperSecretPass", "abcd-1234-wxyz"]
    text = "Connecting to rtsp://admin:SuperSecretPass@192.168.0.1 and key abcd-1234-wxyz"
    redacted = se.redact(text, secrets)
    assert "SuperSecretPass" not in redacted
    assert "abcd-1234-wxyz" not in redacted
    assert "****" in redacted


def test_channel_label():
    cfg = configparser.ConfigParser()
    cfg.read_dict(se.DEFAULTS)

    # When no [channel_names] section exists
    assert se.channel_label(cfg, 1) == "1"

    # When [channel_names] is defined
    cfg.add_section("channel_names")
    cfg["channel_names"]["1"] = "Front Gate"
    cfg["channel_names"]["7"] = "Backyard"

    assert se.channel_label(cfg, 1) == "1 (Front Gate)"
    assert se.channel_label(cfg, 7) == "7 (Backyard)"
    assert se.channel_label(cfg, 2) == "2"


def test_settings_roundtrip(tmp_path: Path):
    test_ini = tmp_path / "test_config.ini"
    cfg = se.load_settings(test_ini)

    cfg["dvr"]["host"] = "10.0.0.50"
    cfg["youtube"]["stream_key"] = "test-key-9999"
    cfg["switching"]["mode"] = "rotate"

    se.save_settings(cfg, test_ini)
    assert test_ini.exists()

    reloaded = se.load_settings(test_ini)
    assert reloaded["dvr"]["host"] == "10.0.0.50"
    assert reloaded["youtube"]["stream_key"] == "test-key-9999"
    assert reloaded["switching"]["mode"] == "rotate"
    assert "osd" in reloaded.sections()


def test_osd_defaults():
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict(se.DEFAULTS)
    assert cfg["osd"]["enabled"] == "false"
    assert cfg["osd"]["show_channel"] == "true"
    assert cfg["osd"]["show_time"] == "true"
    assert cfg["osd"]["show_temp"] == "true"
    assert cfg["osd"]["weather_location"] == "auto"
    assert cfg["encoder"]["video_codec"] == "h264_qsv"


def test_osd_temperature_settings_roundtrip(tmp_path: Path):
    test_ini = tmp_path / "test_config.ini"
    cfg = se.load_settings(test_ini)
    assert cfg["osd"]["show_temp"] == "true"
    assert cfg["osd"]["weather_location"] == "auto"

    cfg["osd"]["show_temp"] = "false"
    cfg["osd"]["weather_location"] = "London"
    se.save_settings(cfg, test_ini)

    reloaded = se.load_settings(test_ini)
    assert reloaded["osd"]["show_temp"] == "false"
    assert reloaded["osd"]["weather_location"] == "London"


def test_fetch_temperature_open_meteo(monkeypatch):
    import io

    class MockResponse:
        def __init__(self, data: bytes):
            self.data = data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return self.data

    def mock_urlopen(req, timeout=5):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "open-meteo.com" in url:
            return MockResponse(b'{"current": {"temperature_2m": 24.3}}')
        raise RuntimeError("Unexpected URL")

    monkeypatch.setattr(se.urllib.request, "urlopen", mock_urlopen)
    temp = se.fetch_temperature("12.97,77.59")
    assert temp == "24\u00b0C"


def test_fetch_temperature_wttr_fallback(monkeypatch):
    class MockResponse:
        def __init__(self, data: bytes):
            self.data = data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return self.data

    def mock_urlopen(req, timeout=5):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "open-meteo" in url:
            raise se.urllib.error.URLError("Network unreachable")
        if "wttr.in" in url:
            return MockResponse(b"+27\xc2\xb0C\n")
        raise RuntimeError("Unexpected URL")

    monkeypatch.setattr(se.urllib.request, "urlopen", mock_urlopen)
    temp = se.fetch_temperature("London")
    assert temp == "27\u00b0C"


def test_fetch_temperature_error(monkeypatch):
    def mock_urlopen(req, timeout=5):
        raise se.urllib.error.URLError("Network unreachable")

    monkeypatch.setattr(se.urllib.request, "urlopen", mock_urlopen)
    assert se.fetch_temperature("auto") is None


def _osd_engine(monkeypatch):
    monkeypatch.setattr(se, "find_tool", lambda cfg, name: name)
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict(se.DEFAULTS)
    cfg["osd"]["enabled"] = "true"
    return se.Engine(cfg)


def test_temp_stale(monkeypatch):
    eng = _osd_engine(monkeypatch)
    src = types.SimpleNamespace(osd_temp=None)

    # Nothing fetched yet -> nothing to refresh
    assert not eng._temp_stale(src)

    eng.current_temp = "28°C"
    assert eng._temp_stale(src)

    src.osd_temp = "28°C"
    assert not eng._temp_stale(src)

    src.osd_temp = "27°C"
    assert eng._temp_stale(src)
    eng.cfg["osd"]["show_temp"] = "false"
    assert not eng._temp_stale(src)


def test_control_step_refreshes_temperature(monkeypatch):
    eng = _osd_engine(monkeypatch)
    begun = []
    monkeypatch.setattr(eng, "_begin", lambda ch, why: begun.append((ch, why)))
    eng.current_temp = "28°C"
    eng.active = types.SimpleNamespace(channel=7, osd_temp="27°C", dead=False,
                                       stalled_for=lambda now: 0)
    eng.active_since = 100.0
    eng.selected = 7

    # Rotation picks the temperature up on its next switch, no extra restart
    eng.mode = "rotate"
    eng._control_step(100.0)
    assert begun == []

    eng.mode = "off"
    eng._control_step(100.0)
    assert begun == [(7, "temperature update")]


def test_worker_osd_vf_filter(monkeypatch):
    monkeypatch.setattr(se, "find_tool", lambda cfg, name: name)
    monkeypatch.setattr(se, "probe", lambda cfg, ffprobe, ch, timeout=10: {
        "ok": True, "reason": "", "video": "h264", "audio": None, "width": 1920, "height": 1080
    })

    captured_cmds = []
    class DummyProc:
        def __init__(self, cmd, *args, **kwargs):
            captured_cmds.append(cmd)
            self.stdout = None
            self.stderr = []
            self.returncode = 0
        def poll(self):
            return 0

    monkeypatch.setattr(se.subprocess, "Popen", DummyProc)
    monkeypatch.setattr(se.InputSource, "_read_video", lambda self, size: None)

    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict(se.DEFAULTS)
    cfg["osd"]["enabled"] = "true"
    cfg["osd"]["show_channel"] = "true"
    cfg["osd"]["show_time"] = "true"
    cfg["osd"]["show_temp"] = "true"

    eng = se.Engine(cfg)
    eng.current_temp = "28°C"
    worker = se.InputSource(eng, 1)

    import time
    for _ in range(20):
        if captured_cmds:
            break
        time.sleep(0.05)

    assert len(captured_cmds) == 1
    vf_arg = captured_cmds[0][captured_cmds[0].index("-vf") + 1]
    assert "text='%{localtime}  ·  28°C'" in vf_arg
    # reload= would let a swapped file kill the camera feed
    assert "textfile=" not in vf_arg
    assert "reload=" not in vf_arg
    assert worker.osd_temp == "28°C"


def test_muted_channels_roundtrip(tmp_path: Path):
    test_ini = tmp_path / "test_config.ini"
    cfg = se.load_settings(test_ini)
    assert cfg["audio"]["muted_channels"] == ""

    cfg["audio"]["muted_channels"] = "3,5"
    se.save_settings(cfg, test_ini)
    reloaded = se.load_settings(test_ini)
    assert se.parse_channels(reloaded["audio"]["muted_channels"]) == [3, 5]


def test_engine_channel_mute(monkeypatch):
    monkeypatch.setattr(se, "find_tool", lambda cfg, name: name)
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict(se.DEFAULTS)
    cfg["audio"]["muted_channels"] = "2"
    eng = se.Engine(cfg)

    assert eng.audio_muted(2)
    assert not eng.audio_muted(1)

    eng.set_channel_muted(1, True)
    eng.set_channel_muted(2, False)
    assert eng.audio_muted(1)
    assert not eng.audio_muted(2)

    # The global switch overrides per-channel settings
    eng.set_audio(False)
    assert eng.audio_muted(2)
