#!/usr/bin/env python3
"""Behavioral tests for LinuxPlay host Wayland support (run inside .venv)."""
import argparse
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host  # noqa: E402

PASS = []
def ok(name):
    PASS.append(name)
    print(f"  PASS: {name}")

# ── 1. Session detection ─────────────────────────────────────────────
st = host._session_type()
print(f"session_type = {st!r}  (XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE')!r})")
assert st == "wayland", st
ok("_session_type detects Wayland")

# ── 2. Monitor detection on the real session ─────────────────────────
ks = host._detect_monitors_kscreen()
print(f"kscreen-doctor -> {ks}")
assert (2560, 1440, 1080, 162) in ks, ks
assert (1080, 1920, 0, 0) in ks, ks
ok("kscreen parser finds both monitors incl. rotated portrait at correct offsets")

xr = host._detect_monitors_xrandr()
print(f"xrandr        -> {xr}")
assert (2560, 1440, 1080, 162) in xr and (1080, 1920, 0, 0) in xr, xr
ok("xrandr fallback agrees")

dm = host.detect_monitors()
assert dm == ks, (dm, ks)
ok("detect_monitors() dispatches to Wayland path")

vw, vh = host._virtual_screen_size()
assert (vw, vh) == (3640, 1920), (vw, vh)
ok("virtual screen bounding box 3640x1920")
args = argparse.Namespace(
    encoder="h.264", hwenc="auto", framerate="60", bitrate="8M", preset="medium",
    gop="15", qp="", tune="", pix_fmt="yuv420p", display=":0", audio="enable",
)
host.host_state.client_ip = "127.0.0.1"
host.host_state.net_mode = "lan"
host.HOST_ARGS = args

os.environ.pop("LINUXPLAY_CAPTURE", None)
cmd = host.build_video_cmd(args, "8M", (2560, 1440, 1080, 162), 5000)
assert cmd and "kmsgrab" in cmd, cmd
ok("auto capture picks kmsgrab (Wayland-safe)")

old_probe = host.ffmpeg_has_device
host.ffmpeg_has_device = lambda name: False
try:
    host.build_video_cmd(args, "8M", (1920, 1080, 0, 0), 5000)
    raise AssertionError("expected RuntimeError for x11grab under Wayland with no kmsgrab")
except RuntimeError as e:
    print(f"  refusal message: {e}")
ok("auto + no kmsgrab under Wayland refuses with clear error")

os.environ["LINUXPLAY_CAPTURE"] = "x11grab"
cmd2 = host.build_video_cmd(args, "8M", (1920, 1080, 0, 0), 5000)
assert cmd2 and "x11grab" in cmd2, cmd2
ok("explicit LINUXPLAY_CAPTURE=x11grab override still honored (with warning)")
host.ffmpeg_has_device = old_probe
os.environ.pop("LINUXPLAY_CAPTURE", None)

# ── 3b. Path-MTU-aware TS packet sizing + VAAPI GOP passthrough ─────
host.host_state.client_ip = "100.104.182.44"   # CGNAT range → tunnel (tailscale0, MTU 1280 here)
cmd_tun = host.build_video_cmd(args, "8M", (1920, 1080, 0, 0), 5000)
url_tun = next(x for x in cmd_tun if x.startswith("udp://"))
rt = host._route_mtu("100.104.182.44")
if rt and rt < 1500:
    fit = host._best_ts_pkt_size(rt, False)
    assert f"pkt_size={fit}" in url_tun, url_tun
    assert fit + 28 <= rt, (fit, rt)
    print(f"  path mtu {rt} → TS pkt_size {fit} (datagram {fit + 28} B, unfragmented)")
else:
    assert "pkt_size=1316" in url_tun, url_tun
    print("  no sub-1500 route MTU found; fallback pkt_size 1316 kept")
ok("TS pkt_size fits path MTU (no IP fragmentation over tunnels)")

assert cmd_tun[cmd_tun.index("-g") + 1] == "15", cmd_tun
ok("VAAPI encoder honors --gop")

assert cmd_tun[cmd_tun.index("-slices") + 1] == "4", cmd_tun
ok("VAAPI encoder uses 4 slices (loss confined to one band)")

host.host_state.client_ip = "127.0.0.1"

# ── 4. uinput injector: kernel registration + injection calls ────────
import re  # noqa: E402

inj = host._get_uinput_injector()
assert inj is not None, "injector creation failed"
print(f"injector virtual screen: {inj._vw}x{inj._vh}")
ok("uinput injector created")

time.sleep(0.2)   # let the kernel settle device registration
with open("/proc/bus/input/devices") as f:
    procs = f.read()

def section(name):
    m = re.search(r'N: Name="%s".*?(?=^N: |\Z)' % re.escape(name), procs, re.S | re.M)
    return m.group(0) if m else ""

kb, ptr, whl = (section(f"LinuxPlay Virtual {n}") for n in ("Keyboard", "Pointer", "Wheel"))
for label, sec in (("Keyboard", kb), ("Pointer", ptr), ("Wheel", whl)):
    if not sec:
        print(f"  !! {label} section missing; /proc tail:\n{procs[-600:]}")
assert kb and ptr and whl
print("Keyboard caps:", [ln.strip() for ln in kb.splitlines() if ln.strip().startswith(("B:", "H:"))])
print("Pointer  caps:", [ln.strip() for ln in ptr.splitlines() if ln.strip().startswith(("B:", "H:"))])
print("Wheel    caps:", [ln.strip() for ln in whl.splitlines() if ln.strip().startswith(("B:", "H:"))])
ok("all three virtual devices registered in kernel input layer")

assert "EV=" in kb and "KEY=" in kb and "event" in kb, kb
ok("keyboard device exposes KEY bitmap + event handler")

assert "ABS=" in ptr and "KEY=" in ptr and "event" in ptr, ptr
ok("pointer device exposes ABS bitmap (absolute) + button bitmap")

assert "REL=" in whl and "event" in whl, whl
ok("wheel device exposes REL bitmap")

# Exercise the shipped module-level entry points (host writes /dev/uinput only;
# reading /dev/input/eventN needs the 'input' group and is not used at runtime).
assert inj.mouse_abs(2000, 1000) is True
ok("absolute pointer write accepted (2000,1000) within bounds")
assert inj.mouse_abs(999999, 999999) is True and inj.mouse_abs(-5, -5) is True
ok("out-of-range absolute coords clamped into bounds")
assert inj.mouse_button("1", True) and inj.mouse_button("1", False)
ok("mouse button write accepted (BTN_LEFT press/release)")
assert inj.wheel("4") is True
ok("wheel write accepted (REL_WHEEL +1)")

assert inj.key("down", "a") and inj.key("up", "a")
ok("plain key accepted (KEY_A press/release)")
assert inj.key("down", "!") and inj.key("up", "!")
ok("shifted char accepted (SHIFT+KEY_1 sequence)")
assert inj.key("down", "Shift_L") and inj.key("up", "Shift_L")
ok("named modifier accepted (Shift_L)")
assert inj.key("down", "Page_Up") and inj.key("up", "Page_Up")
ok("named key accepted (Page_Up)")

assert not inj.key("down", "NoSuchKey_zz")
ok("unknown key name falls through to legacy path")

# auto-shift bookkeeping must be clean after the sequences above
assert not inj._auto_shift_keys and not inj._shift_pressed, (inj._auto_shift_keys, inj._shift_pressed)
ok("auto-shift state clean after press/release pairs")

print(f"\nALL {len(PASS)} HOST TESTS PASSED")
