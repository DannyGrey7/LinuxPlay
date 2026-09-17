#!/usr/bin/env python3
"""Turn the launcher's saved Host settings into a host.py command line.

One source of truth for two callers that must not drift:

  * start.py's "Start Host" button, which spawns the host as a child;
  * autostart_host.py, which re-creates the same command at login from
    ~/.linuxplay_start_cfg.json.

Everything here is pure: no Qt, no globals beyond the path constants, so the
mapping can be tested without a display.
"""
import json
import os
import re
import uuid

CFG_PATH = os.path.join(os.path.expanduser("~"), ".linuxplay_start_cfg.json")
MARKER = "LinuxPlayHost"

# The launcher stores the combo label, not the CLI alias ("Native (desktop)").
NATIVE_RESOLUTION_ALIASES = ("", "native", "native (desktop)", "auto", "desktop",
                            "off", "0", "0x0")
RESOLUTION_RE = re.compile(r"\d{2,5}x\d{2,5}")

# Presets that imply a 1-frame GOP when the GOP combo is left on "Auto".
ULL_PRESETS = ("llhp", "zerolatency", "ultra-low-latency", "ull")

ENCODERS = ("none", "h.264", "h.265")
# host.py's --hwenc choices: amf exists in the launcher's list on Windows only.
BACKENDS = ("auto", "cpu", "nvenc", "qsv", "vaapi")
CAPTURE_MODES = ("auto", "portal", "kmsgrab", "x11grab")

# What the launcher's "Default" profile selects (start.py profileChanged), used
# when the launcher has never saved a config on this machine.
HOST_DEFAULTS = {
    "encoder": "h.264",
    "hwenc": "auto",
    "framerate": "30",
    "resolution": "native",
    "bitrate": "8M",
    "audio": "enable",
    "adaptive": False,
    "display": ":0",
    "preset": "Default",
    "gop": "30",
    "qp": "None",
    "tune": "None",
    "pix_fmt": "yuv420p",
    "audio_mode": "Voice (low-latency)",
    "capture": "auto",
    "debug": False,
}


def load_launcher_cfg(path=None):
    """The whole launcher config file, or {} when it is missing/unreadable."""
    try:
        with open(path or CFG_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_host_cfg(path=None):
    """Just the saved Host tab settings (may be partial or empty)."""
    host = load_launcher_cfg(path).get("host")
    return host if isinstance(host, dict) else {}


def _text(value, fallback=""):
    """Config values are combo labels: strings, trimmed; anything else ignored."""
    if isinstance(value, bool) or value is None:
        return fallback
    s = str(value).strip()
    return s or fallback


def normalize_resolution(value):
    """"Native (desktop)" and friends -> "native"; a real WxH size passes through."""
    res = _text(value)
    if res.lower() in NATIVE_RESOLUTION_ALIASES:
        return "native"
    if RESOLUTION_RE.fullmatch(res.lower()):
        return res
    return ""


def effective_cfg(cfg):
    """Saved settings with defaults filled in and strings made safe for argv.

    A generated command line is not watched by a human, so a stale or hand-edited
    config must never reach argparse as an invalid choice.
    """
    cfg = cfg or {}
    out = dict(HOST_DEFAULTS)
    for key in HOST_DEFAULTS:
        if key in cfg and cfg[key] is not None and not isinstance(cfg[key], dict):
            out[key] = cfg[key]

    encoder = _text(out.get("encoder"), HOST_DEFAULTS["encoder"])
    out["encoder"] = encoder if encoder in ENCODERS else HOST_DEFAULTS["encoder"]
    if out["encoder"] == "none":       # a host without an encoder is not a host
        out["encoder"] = "h.264"

    hwenc = _text(out.get("hwenc"), "auto")
    out["hwenc"] = hwenc if hwenc in BACKENDS else "auto"

    out["audio"] = "disable" if _text(out.get("audio")) == "disable" else "enable"
    out["framerate"] = _text(out.get("framerate"), HOST_DEFAULTS["framerate"])
    out["bitrate"] = _text(out.get("bitrate"), HOST_DEFAULTS["bitrate"])
    out["display"] = _text(out.get("display"), HOST_DEFAULTS["display"])
    out["pix_fmt"] = _text(out.get("pix_fmt"), HOST_DEFAULTS["pix_fmt"])
    out["resolution"] = normalize_resolution(out.get("resolution")) or "native"

    capture = _text(out.get("capture"), "auto").lower()
    out["capture"] = capture if capture in CAPTURE_MODES else "auto"

    out["adaptive"] = bool(out.get("adaptive", False))
    out["debug"] = bool(out.get("debug", False))
    return out


def build_host_argv(python, host_py, cfg, gui=False):
    """Host command line for the given settings (mirrors start.py's Start Host)."""
    c = effective_cfg(cfg)

    # "Default"/"None" are combo placeholders meaning "let the host decide".
    preset = _text(c.get("preset"))
    preset = "" if preset in ("Default", "None") else preset
    qp = _text(c.get("qp"))
    qp = "" if qp in ("None",) else qp
    tune = _text(c.get("tune"))
    tune = "" if tune in ("None",) else tune

    argv = [python, host_py]
    if gui:
        argv.append("--gui")
    argv += [
        "--encoder", c["encoder"],
        "--framerate", c["framerate"],
        "--resolution", c["resolution"],
        "--bitrate", c["bitrate"],
        "--audio", c["audio"],
        "--pix_fmt", c["pix_fmt"],
        "--hwenc", c["hwenc"],
    ]
    if c["adaptive"]:
        argv.append("--adaptive")
    if preset:
        argv += ["--preset", preset]
    if qp:
        argv += ["--qp", qp]
    if tune:
        argv += ["--tune", tune]
    if c["debug"]:
        argv.append("--debug")
    argv += ["--display", c["display"]]

    try:
        gop = int(_text(c.get("gop"), "0") or 0)
    except Exception:
        gop = 0
    if gop > 0:
        argv += ["--gop", str(gop)]
    elif preset.lower() in ULL_PRESETS:
        argv += ["--gop", "1"]
    return argv


def build_host_env(cfg, base_env=None, sid=None):
    """Environment the host expects: the ffmpeg marker, a session id, audio mode.

    LINUXPLAY_SID tags this run's ffmpeg children so the launcher can tell its
    own leftovers from someone else's (see start.py _ffmpeg_running_for_us).
    """
    c = effective_cfg(cfg)
    env = dict(os.environ if base_env is None else base_env)
    env["LINUXPLAY_MARKER"] = MARKER
    env["LINUXPLAY_SID"] = sid or env.get("LINUXPLAY_SID") or str(uuid.uuid4())

    music = "music" in _text(c.get("audio_mode"), "").lower()
    env["LP_OPUS_APP"] = "audio" if music else "voip"
    env["LP_OPUS_FD"] = "20" if music else "10"
    env["LINUXPLAY_CAPTURE"] = c["capture"]

    kms_dev = _text((cfg or {}).get("kms_device"))
    if kms_dev:
        env["LINUXPLAY_KMS_DEVICE"] = kms_dev
    return env
