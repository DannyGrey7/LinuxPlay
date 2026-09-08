#!/usr/bin/env python3
"""Fixture tests for monitor-detection parsers not natively runnable here."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host  # noqa: E402

PASS = []
def ok(name):
    PASS.append(name)
    print(f"  PASS: {name}")

real_co, real_which = host.subprocess.check_output, host.which

WLR = '''DP-1 "Dell U2515H (DP-1)"
  Make: Dell
  Model: U2515H
  Physical size: 550.000 mm x 310.000 mm
  Enabled: yes
  Modes:
    1920x1080 v60.000000 Hz (preferred)
    2560x1440 v59.951 Hz (current)
  Position: 1080,162
  Transform: normal
  Scale: 1.000000
HDMI-A-1 "TV (HDMI-A-1)"
  Enabled: no
  Modes:
    1920x1080 v60.000000 Hz (current)
  Position: 0,0
  Transform: normal
'''

HYP = '''[{"id":0,"name":"DP-1","width":2560,"height":1440,"x":1080,"y":162,"disabled":false,"focused":true},
{"id":1,"name":"HDMI-A-2","width":1920,"height":1080,"x":0,"y":0,"disabled":true}]'''

KSCREEN_ANSI = (
    "\x1b[01;32mOutput: \x1b[0;0m1 DP-1 79479e0f-uuid\n"
    "\t\x1b[01;32menabled\x1b[0;0m\n"
    "\t\x1b[01;32mconnected\x1b[0;0m\n"
    "\tGeometry: 1080,162 2560x1440\n"
    "\tScale: 1\n"
    "\x1b[01;32mOutput: \x1b[0;0m2 HDMI-A-2 7e56f91f-uuid\n"
    "\t\x1b[01;32mdisabled\x1b[0;0m\n"
    "\tGeometry: 0,0 1920x1080\n"
)

def run_probe(fn, fixture):
    host.which = lambda n: "/usr/bin/fake"
    host.subprocess.check_output = lambda cmd, **kw: fixture
    try:
        return fn()
    finally:
        host.subprocess.check_output, host.which = real_co, real_which

assert run_probe(host._detect_monitors_wlr_randr, WLR) == [(2560, 1440, 1080, 162)]
ok("wlr-randr parser: enabled+current mode+position; disabled output skipped")

assert run_probe(host._detect_monitors_hyprctl, HYP) == [(2560, 1440, 1080, 162)]
ok("hyprctl -j parser: disabled monitor skipped")

assert run_probe(host._detect_monitors_kscreen, KSCREEN_ANSI) == [(2560, 1440, 1080, 162)]
ok("kscreen parser: ANSI codes stripped, disabled output skipped, geometry exact")

# _session_type env-var precedence
saved = {k: os.environ.pop(k, None) for k in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY")}
try:
    os.environ["XDG_SESSION_TYPE"] = "x11";  assert host._session_type() == "x11"
    os.environ["XDG_SESSION_TYPE"] = "wayland";  assert host._session_type() == "wayland"
    del os.environ["XDG_SESSION_TYPE"]; os.environ["WAYLAND_DISPLAY"] = "wayland-0"
    assert host._session_type() == "wayland"
    del os.environ["WAYLAND_DISPLAY"]; os.environ["DISPLAY"] = ":0"
    assert host._session_type() == "x11"
    del os.environ["DISPLAY"]
    assert host._session_type() == "unknown"
    ok("_session_type precedence: XDG_SESSION_TYPE > WAYLAND_DISPLAY > DISPLAY > unknown")
finally:
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v

assert host._session_type() == "wayland"   # env restored

# CPU preset/tune sanitization (GUI offers mixed-backend names)
assert host._safe_x264_preset("llhp") == "ultrafast"
assert host._safe_x264_preset("p7") == "veryslow"
assert host._safe_x264_preset("balanced") == "medium"
assert host._safe_x264_preset("Default") == ""
assert host._safe_x264_preset("bogus") == "ultrafast"
assert host._safe_x264_preset("veryfast") == "veryfast"
ok("libx264 preset sanitizer: NVENC/QSP-style names mapped, junk -> ultrafast")

assert host._safe_cpu_tune("h.264", "") == ""
assert host._safe_cpu_tune("h.264", "high-quality") == ""
assert host._safe_cpu_tune("h.264", "low-latency") == "zerolatency"
assert host._safe_cpu_tune("h.264", "film") == "film"
assert host._safe_cpu_tune("h.265", "film") == "zerolatency"   # x265 has no film tune
assert host._safe_cpu_tune("h.265", "zerolatency") == "zerolatency"
ok("CPU tune sanitizer: aliases mapped, invalid per-codec -> zerolatency")

print(f"\nALL {len(PASS)} PARSER TESTS PASSED")
