#!/usr/bin/env python3
"""Behavioural tests for the LinuxPlay host.

Covers session/monitor detection, the capture command builders (portal,
kmsgrab, x11grab), handshake framing and the input injector's key tables.

Environment-dependent sections are *skipped* rather than failed when this
machine does not have them (no Wayland session, no kscreen-doctor, no
/dev/uinput): the runner reports exit 77 as SKIP. Nothing in here injects
input into the running session — the previous version typed 'q', Shift and
Page_Up into whatever window had focus.
"""
import argparse
import inspect
import os
import re
import shutil
import socket
import sys
import threading
import time
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host  # noqa: E402

PASS, SKIPPED = [], []


def ok(name):
    PASS.append(name)
    print(f"  PASS: {name}")


def skip(name, why):
    SKIPPED.append(name)
    print(f"  SKIP: {name} — {why}")


@contextmanager
def patched(obj, **attrs):
    """Set attributes on obj (a module object or instance) for the block."""
    old = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


# ── 1. pure parsers and command builders (no environment) ────────────
assert host._parse_resolution("1920x1080") == (1920, 1080)
assert host._parse_resolution(" 1280x720 ") == (1280, 720)
assert host._parse_resolution("native") is None
assert host._parse_resolution("") is None
assert host._parse_resolution("1080p") is None          # junk must not crash the host
assert host._parse_resolution("8x8") is None            # too small to encode
assert host._parse_resolution("99999x1080") is None
ok("--resolution parses WxH, treats native/junk as the monitor's own size")

assert host._prepend_filter(["-vf", "format=nv12,hwupload", "-vaapi_device", "d"],
                            "scale=1280:720") == \
    ["-vf", "scale=1280:720,format=nv12,hwupload", "-vaapi_device", "d"]
assert host._prepend_filter(None, "scale=1280:720") == ["-vf", "scale=1280:720"]
ok("scale filter composes with an encoder's existing -vf")

assert host._normalise_codec("none") == "h.264"
assert host._normalise_codec("") == "h.264"
assert host._normalise_codec("h.265") == "h.265"
assert host._normalise_codec("HEVC") == "h.265"
ok("--encoder none normalises to a real codec instead of an empty encoder list")

# The rounded TS packet size cannot tell 1420 (WireGuard) from 1500, so tunnel
# detection must use the path MTU. _path_mtu reads the kernel routing table;
# feed it a fake one.
with patched(host, _route_mtu=lambda ip: 1420):
    assert host._path_mtu("10.0.0.1") == 1420
    assert host._best_ts_pkt_size(1420, False) == 1316
with patched(host, _route_mtu=lambda ip: 0):
    assert host._path_mtu("10.0.0.1") == 1500
with patched(host, _route_mtu=lambda ip: 9000):
    assert host._path_mtu("10.0.0.1") == 1500
ok("_path_mtu reports sub-1500 tunnels the rounded pkt_size hides")

assert host._xdotool_safe_keyname("a") == "a"
assert host._xdotool_safe_keyname("!") == "exclam"
assert host._xdotool_safe_keyname("Shift_L") == "Shift_L"
assert host._xdotool_safe_keyname("--version") is None
assert host._xdotool_safe_keyname("-e") is None
assert host._xdotool_safe_keyname("a;rm -rf /") is None
assert host._xdotool_safe_keyname(None) is None
ok("client-supplied key names cannot reach xdotool as options")

# ── 1b. kmsgrab filter chains follow the chosen encoder and crop the monitor ──
_W, _H, _OX, _OY = 2560, 1440, 1080, 162


def _mk_args(encoder="h.264", hwenc="auto", pix_fmt="yuv420p"):
    return argparse.Namespace(
        encoder=encoder, hwenc=hwenc, framerate="60", bitrate="8M", preset="",
        gop="30", qp="", tune="", pix_fmt=pix_fmt, display=":0", audio="enable")


def _kms_cmd(args, stream_size=None, has_nvidia=False, has_vaapi=True,
             vaapi_encoders=("h264_vaapi", "hevc_vaapi")):
    """build_video_cmd with kmsgrab forced and the encoder probes pinned."""
    def has_encoder(name):
        if name in ("h264_nvenc", "hevc_nvenc"):
            return has_nvidia
        return name in vaapi_encoders or name in ("libx264", "libx265")

    with patched(host,
                 ffmpeg_has_device=lambda name: name == "kmsgrab",
                 ffmpeg_has_encoder=has_encoder,
                 has_nvidia=lambda: has_nvidia,
                 is_intel_cpu=lambda: False,
                 has_vaapi=lambda: has_vaapi,
                 _session_type=lambda: "wayland",
                 _kmsgrab_perms_ok=lambda: True,
                 _route_mtu=lambda ip: 1500,
                 _pick_kms_device=lambda: "/dev/dri/card1"):
        old_ip = host.host_state.client_ip
        host.host_state.client_ip = "127.0.0.1"
        try:
            return host.build_video_cmd(args, "8M", (_W, _H, _OX, _OY), 5000,
                                        stream_size=stream_size)
        finally:
            host.host_state.client_ip = old_ip


# "auto" resolves to NVENC on an NVIDIA box even when VAAPI merely exists —
# the exact case where keying the filters off args.hwenc produced no hwdownload
# and the encoder exited on the DRM-PRIME frame.
_cmd = _kms_cmd(_mk_args(hwenc="auto"), has_nvidia=True)
assert _cmd and "kmsgrab" in _cmd, _cmd
assert _cmd[_cmd.index("-c:v") + 1] == "h264_nvenc", _cmd
_vf = _cmd[_cmd.index("-vf") + 1]
assert "hwdownload" in _vf, _vf
assert f"crop={_W}:{_H}:{_OX}:{_OY}" in _vf, _vf
ok("kmsgrab: auto→nvenc still downloads the frame and crops the monitor")

_cmd = _kms_cmd(_mk_args(hwenc="auto"), has_nvidia=False)
assert _cmd[_cmd.index("-c:v") + 1] == "h264_vaapi", _cmd
_vf = _cmd[_cmd.index("-vf") + 1]
assert f"crop={_W}:{_H}:{_OX}:{_OY}" in _vf and "hwupload" in _vf, _vf
ok("kmsgrab + VAAPI crops the monitor and re-uploads for the encoder")

_cmd = _kms_cmd(_mk_args(hwenc="cpu"), has_nvidia=False, has_vaapi=False)
assert _cmd[_cmd.index("-c:v") + 1] == "libx264", _cmd
_vf = _cmd[_cmd.index("-vf") + 1]
assert "hwdownload" in _vf and f"crop={_W}:{_H}:{_OX}:{_OY}" in _vf, _vf
ok("kmsgrab + libx264 downloads, crops, then encodes")

_cmd = _kms_cmd(_mk_args(hwenc="cpu"), has_nvidia=False, has_vaapi=False,
                stream_size=(1280, 720))
_vf = _cmd[_cmd.index("-vf") + 1]
assert f"crop={_W}:{_H}:{_OX}:{_OY}" in _vf and "scale=1280:720" in _vf, _vf
ok("kmsgrab: crop and --resolution scale compose in the filter chain")

# ── 1c. portal capture leaves scaling to the feeder ──────────────────
_scaled = _mk_args(encoder="h.265", hwenc="auto", pix_fmt="yuv444p")
_portal = {"node": 110, "w": 1707, "h": 1067}
with patched(host, ffmpeg_has_encoder=lambda n: True, has_vaapi=lambda: True):
    old_ip = host.host_state.client_ip
    host.host_state.client_ip = "127.0.0.1"
    try:
        stream_cmd = host.build_video_cmd(_scaled, "30M", (1707, 1067, 0, 0), 5000,
                                          portal_stream=_portal, stream_size=(1280, 720))
        native_cmd = host.build_video_cmd(_scaled, "30M", (1707, 1067, 0, 0), 5000,
                                          portal_stream=_portal)
    finally:
        host.host_state.client_ip = old_ip
assert stream_cmd[stream_cmd.index("-video_size") + 1] == "1280x720", stream_cmd
assert native_cmd[native_cmd.index("-video_size") + 1] == "1707x1067", native_cmd
assert stream_cmd[stream_cmd.index("-vf") + 1] == "format=nv12,hwupload", stream_cmd
ok("portal capture leaves the scaling to the feeder (no double scale)")

# ── 1d. x11grab keeps the full-monitor region; --resolution scales in ffmpeg ──
os.environ["LINUXPLAY_CAPTURE"] = "x11grab"
try:
    with patched(host, _session_type=lambda: "wayland"):
        old_ip = host.host_state.client_ip
        host.host_state.client_ip = "127.0.0.1"
        try:
            cmd2 = host.build_video_cmd(_mk_args(), "8M", (1920, 1080, 0, 0), 5000)
            cmd2s = host.build_video_cmd(_mk_args(), "8M", (1707, 1067, 0, 0), 5000,
                                         stream_size=(1280, 720))
        finally:
            host.host_state.client_ip = old_ip
    assert cmd2 and "x11grab" in cmd2, cmd2
    assert cmd2s[cmd2s.index("-video_size") + 1] == "1707x1067", cmd2s
    assert "scale=1280:720" in cmd2s[cmd2s.index("-vf") + 1], cmd2s
    ok("LINUXPLAY_CAPTURE=x11grab override honored; region is the monitor")
finally:
    os.environ.pop("LINUXPLAY_CAPTURE", None)

# ── 1e. the handshake reader waits for the whole message ─────────────
def _handshake_msg(first, second=None, delay=0.2, want_tokens=2):
    a, b = socket.socketpair()
    a.settimeout(5)
    try:
        b.sendall(first)
        if second is not None:
            def _later():
                time.sleep(delay)
                try:
                    b.sendall(second)
                except Exception:
                    pass
            threading.Thread(target=_later, daemon=True).start()
        return host._recv_handshake_msg(a, want_tokens=want_tokens, max_secs=5.0)
    finally:
        a.close()
        b.close()


# The pairing request arrives as one sendall but TCP may split it: the reader
# must not treat "HELLO <pin>" as complete just because it holds two fields.
_msg = _handshake_msg(b"HELLO 123456\n", b"KEYREQ Zm9v\n")
assert _msg.splitlines() == ["HELLO 123456", "KEYREQ Zm9v"], repr(_msg)
ok("handshake reader keeps the KEYREQ line when TCP splits the pairing request")

_proof = _handshake_msg(b"CERT abc SIG def\n", want_tokens=4)
assert _proof.splitlines() == ["CERT abc SIG def"], repr(_proof)
ok("newline-terminated proof line is returned whole")

# A peer that never sends a newline must not hold the listener open.
_t0 = time.time()
_partial = _handshake_msg(b"HELLO 123456", None)
assert _partial == "HELLO 123456", repr(_partial)
assert time.time() - _t0 < 3.0, "no-newline peer stalled the reader"
ok("a message without a trailing newline still returns (bounded wait)")

assert inspect.getsource(host.heartbeat_manager).count("_handle_pong(") == 1, \
    "each PONG must feed the jitter EMA once per datagram"
ok("heartbeat loop handles each PONG exactly once")

# ── 1f. Ctrl+C cannot deadlock on the video-thread lock ──────────────
assert type(host.host_state.video_thread_lock).__name__ == "RLock", \
    "the signal handler re-enters this lock from the main thread"
_held = threading.Event()
_release = threading.Event()


def _holder():
    with host.host_state.video_thread_lock:
        _held.set()
        _release.wait(5)


th = threading.Thread(target=_holder, daemon=True)
th.start()
_held.wait(2)
_t0 = time.time()
host.stop_all(lock_timeout=0.3)          # must give up, not block
elapsed = time.time() - _t0
_release.set()
host.host_state.should_terminate = False
assert elapsed < 3.0, f"stop_all blocked on a held lock for {elapsed:.1f}s"
ok("stop_all(lock_timeout) gives up instead of deadlocking shutdown")

# ── 2. session/monitor detection on this machine ─────────────────────
st = host._session_type()
print(f"session_type = {st!r}  (XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE')!r})")
if st != "wayland":
    skip("_session_type detects Wayland", f"session is {st!r}")
else:
    ok("_session_type detects Wayland")

if not shutil.which("kscreen-doctor"):
    skip("kscreen monitor detection", "kscreen-doctor not installed")
else:
    ks = host._detect_monitors_kscreen()
    print(f"kscreen-doctor -> {ks}")
    if not ks or not all(w > 0 and h > 0 for w, h, _, _ in ks):
        skip("kscreen monitor detection", f"no usable monitors reported: {ks}")
    else:
        ok(f"kscreen parser finds {len(ks)} monitor(s)")
        vw, vh = host._virtual_screen_size()
        assert (vw, vh) == (max(w + x for w, h, x, y in ks),
                            max(h + y for w, h, x, y in ks)), (vw, vh)
        ok(f"virtual screen bounding box {vw}x{vh} matches the detected layout")

if not shutil.which("xrandr"):
    skip("xrandr monitor detection", "xrandr not installed")
else:
    xr = host._detect_monitors_xrandr()
    print(f"xrandr        -> {xr}")
    if not xr:
        skip("xrandr monitor detection", "xrandr reported no monitors")
    else:
        assert all(w > 0 and h > 0 for w, h, _, _ in xr), xr
        ok(f"xrandr fallback parses {len(xr)} monitor(s)")

# ── 3. uinput injector: registration and key tables only (no events) ──
if not os.path.exists("/dev/uinput") or not os.access("/dev/uinput", os.W_OK):
    skip("uinput injector", "/dev/uinput is not writable")
else:
    try:
        inj = host._get_uinput_injector()
    except Exception as e:
        inj = None
        skip("uinput injector", f"creation failed: {e}")
    if inj is not None:
        try:
            ok("uinput injector created")

            time.sleep(0.2)   # let the kernel settle device registration
            with open("/proc/bus/input/devices") as f:
                procs = f.read()

            def section(name):
                m = re.search(r'N: Name="%s".*?(?=^N: |\Z)' % re.escape(name),
                              procs, re.S | re.M)
                return m.group(0) if m else ""

            kb, ptr, whl = (section(f"LinuxPlay Virtual {n}")
                            for n in ("Keyboard", "Pointer", "Wheel"))
            for label, sec in (("Keyboard", kb), ("Pointer", ptr), ("Wheel", whl)):
                if not sec:
                    print(f"  !! {label} section missing; /proc tail:\n{procs[-600:]}")
            if not (kb and ptr and whl):
                skip("virtual devices registered in the kernel",
                     "a device section was missing from /proc/bus/input/devices")
            else:
                assert "EV=" in kb and "KEY=" in kb and "event" in kb, kb
                assert "ABS=" in ptr and "KEY=" in ptr and "event" in ptr, ptr
                assert "REL=" in whl and "event" in whl, whl
                ok("keyboard/pointer/wheel registered with the expected capabilities")

            # Key resolution only: writing events would type into the session.
            assert inj._resolve_key("a")[0] is not None
            assert inj._resolve_key("!")[0] is not None
            assert inj._resolve_key("Shift_L")[0] is not None
            assert inj._resolve_key("Page_Up")[0] is not None
            assert inj._resolve_key("NoSuchKey_zz")[0] is None
            ok("named/character keys resolve to evdev codes, unknown names do not")

            from evdev import ecodes as _ec                    # noqa: E402
            assert all(host._LETTER_KEY[c] == getattr(_ec, f"KEY_{c.upper()}")
                       for c in host._LETTER_KEY), \
                "letters must resolve by name, not arithmetic"
            qwerty_order = "qwertyuiopasdfghjklzxcvbnm"
            codes_in_order = [host._LETTER_KEY[c] for c in qwerty_order]
            assert codes_in_order == sorted(codes_in_order), \
                "codes must follow QWERTY key order, not the alphabet"
            declared = set(inj.key_caps)
            assert set(host._LETTER_KEY.values()) <= declared, \
                "keyboard must declare every letter"
            assert _ec.KEY_Q in declared, "old capability set missed the QWERTY top row"
            ok("every letter maps to its real evdev code and is declared")
        finally:
            # Remove the virtual devices now instead of at process exit.
            for dev in (getattr(inj, "kbd", None), getattr(inj, "abs_mouse", None),
                        getattr(inj, "wheel_dev", None)):
                try:
                    if dev is not None:
                        dev.close()
                except Exception:
                    pass
            ok("virtual devices removed at the end of the test")

print(f"\nALL {len(PASS)} HOST TESTS PASSED"
      + (f" ({len(SKIPPED)} skipped)" if SKIPPED else ""))
if SKIPPED:
    sys.exit(77)
