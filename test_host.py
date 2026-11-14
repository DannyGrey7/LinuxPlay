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
# hevc_vaapi only takes VAAPI surfaces (uploaded from nv12): --pix_fmt yuv444p
# was silently ignored by ffmpeg. The command no longer pretends otherwise.
assert "-pix_fmt" not in native_cmd and "hevc_vaapi" in native_cmd, native_cmd
ok("portal + VAAPI: no -pix_fmt the encoder would ignore")

# A scaled output makes the two size sources disagree by a pixel: 3840x2160 at
# 175% is 2194.29x1234.29, which kscreen-doctor rounds up to 2195x1235 while
# the portal reports 2194x1234. The feeder writes the portal's size into the
# pipe, so -video_size must say the same — otherwise every frame starts
# mid-row and the picture shears and rolls.
_portal_frac = {"node": 68, "w": 2194, "h": 1234}
with patched(host, ffmpeg_has_encoder=lambda n: True, has_vaapi=lambda: True):
    old_ip = host.host_state.client_ip
    host.host_state.client_ip = "127.0.0.1"
    try:
        frac_cmd = host.build_video_cmd(_scaled, "30M", (2195, 1235, 0, 0), 5000,
                                        portal_stream=_portal_frac)
    finally:
        host.host_state.client_ip = old_ip
assert frac_cmd[frac_cmd.index("-video_size") + 1] == "2194x1234", frac_cmd
assert host._portal_stream_size(_portal_frac) == (2194, 1234)
assert host._portal_stream_size(_portal_frac, (1280, 720)) == (1280, 720)
assert "size=_portal_stream_size(ps, stream_size)" in \
    inspect.getsource(host.start_streams_for_current_client), \
    "the feeder must be told the same size the encoder reads"
ok("portal: encoder reads the size the feeder writes when the probe disagrees")

# GPU feeder (the default): GStreamer pads NV12 rows to 4 bytes, so
# 2194 wide arrives as 2196. The encoder reads the padded frame and crops it;
# read as 2194, every frame would start 3,702 bytes early and roll the picture.
with patched(host, ffmpeg_has_encoder=lambda n: True, has_vaapi=lambda: True):
    old_ip = host.host_state.client_ip
    host.host_state.client_ip = "127.0.0.1"
    try:
        gpu_cmd = host.build_video_cmd(_scaled, "30M", (2195, 1235, 0, 0), 5000,
                                       portal_stream=_portal_frac, gpu_feeder=True)
        gpu_720 = host.build_video_cmd(_scaled, "30M", (2195, 1235, 0, 0), 5000,
                                       portal_stream=_portal_frac, stream_size=(1280, 720),
                                       gpu_feeder=True)
    finally:
        host.host_state.client_ip = old_ip
assert gpu_cmd[gpu_cmd.index("-pixel_format") + 1] == "nv12", gpu_cmd
assert gpu_cmd[gpu_cmd.index("-video_size") + 1] == "2196x1234", gpu_cmd
assert gpu_cmd[gpu_cmd.index("-vf") + 1] == "crop=2194:1234:0:0,format=nv12,hwupload", gpu_cmd
assert gpu_720[gpu_720.index("-video_size") + 1] == "1280x720", gpu_720
assert gpu_720[gpu_720.index("-vf") + 1] == "format=nv12,hwupload", gpu_720
assert frac_cmd[frac_cmd.index("-pixel_format") + 1] == "bgr0", "CPU feeder unchanged"
ok("GPU feeder: the encoder reads GStreamer's padded NV12 frame and crops it back")

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

# ── 1d'. fix-plan 27: numbered (RTP) video goes out through seqrelay ──
# On 2026-10-20 the Mac's Wi-Fi delivered up to 13% of video datagrams out of
# order; numbered datagrams let the client put them back before the demuxer.
assert host._client_caps("HELLO 123456 RTP\nKEYREQ abc") == {"RTP"}
assert host._client_caps("HELLO CERTFP:AB12 rtp") == {"RTP"}
assert host._client_caps("HELLO CERTFP:AB12\n") == frozenset()
assert host._client_caps("HELLO 123456\nKEYREQ RTP") == frozenset(), "only the HELLO line"
assert host._video_transport_for({"RTP"}) == "rtp" and host._video_transport_for(set()) == "udp"
os.environ["LINUXPLAY_VIDEO_TRANSPORT"] = "udp"
try:
    assert host._video_transport_for({"RTP"}) == "udp"
finally:
    os.environ.pop("LINUXPLAY_VIDEO_TRANSPORT", None)
ok("RTP is offered on the HELLO line; LINUXPLAY_VIDEO_TRANSPORT=udp turns it off")

os.environ["LINUXPLAY_CAPTURE"] = "x11grab"
_hs = host.host_state
_saved = (_hs.client_ip, _hs.video_transport)
try:
    # A 1344-byte path: 7 TS packets fit plain, but not behind a 12-byte header.
    with patched(host, _session_type=lambda: "wayland", _path_mtu=lambda ip: 1344):
        _hs.client_ip = "100.104.182.44"
        _hs.video_transport = "udp"
        _plain = host.build_video_cmd(_mk_args(), "8M", (1920, 1080, 0, 0), 5001)
        _plain_relay = host._video_relay_cmd(5001)
        _hs.video_transport = "rtp"
        _numbered = host.build_video_cmd(_mk_args(), "8M", (1920, 1080, 0, 0), 5001)
        _relay = host._video_relay_cmd(5001)
finally:
    _hs.client_ip, _hs.video_transport = _saved
    os.environ.pop("LINUXPLAY_CAPTURE", None)
assert _plain[-1].startswith("udp://100.104.182.44:5001?pkt_size=1316&"), _plain[-1]
assert _plain_relay is None
assert _numbered[-1].startswith("udp://127.0.0.1:25001?pkt_size=1128&"), _numbered[-1]
_out_fmt = _numbered[len(_numbered) - 1 - _numbered[::-1].index("-f") + 1]
assert _out_fmt == "mpegts", "ffmpeg's own output stays plain TS"
assert _relay[1].endswith("seqrelay.py"), _relay
assert _relay[2:] == ["send", "25001", "100.104.182.44", "5001"], _relay
ok("RTP video: ffmpeg sends TS to the local relay, sized for the RTP header")

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

# ── 1g. session teardown never restarts streams for a departed client ──
class _FakeStream:
    """Stands in for StreamThread: records starts, never spawns ffmpeg."""
    started = []

    def __init__(self, cmd, name, feeder_cmd=None, cmd_factory=None, relay_cmd=None,
                 fallback=None):
        self.name = name
        self.relay_cmd = relay_cmd

    def start(self):
        _FakeStream.started.append(self.name)

    def stop(self):
        time.sleep(0.3)                     # a real encoder takes a while to die

    def join(self, timeout=None):
        pass


def _fresh_session(ip="127.0.0.1"):
    hs = host.host_state
    hs.video_threads, hs.audio_thread = {}, None
    hs.last_disconnect_ts = 0.0
    hs.capture_mode = "x11grab"
    hs.monitors = [(1920, 1080, 0, 0)]
    with hs.pin_lock:
        reply = host._begin_session_locked(ip, "h.265")
    return reply.split("TOKEN ", 1)[1].split()[0]


_args = argparse.Namespace(audio="enable")
_quiet = dict(set_status=lambda *_: None, set_pin_display=lambda *_: None)
with patched(host, StreamThread=_FakeStream, build_audio_cmd=lambda: ["ffmpeg"],
             build_video_cmd=lambda *a, **k: ["ffmpeg"], HOST_ARGS=_args,
             RECONNECT_COOLDOWN=0.2, **_quiet):
    # end_session forgets the client before it stops anything, exactly once.
    _fresh_session()
    seen = {}
    with patched(host, stop_streams_only=lambda: seen.update(
            ip=host.host_state.client_ip, active=host.host_state.session_active)):
        epoch = host.host_state.session_epoch
        assert host.end_session("test") is True
        assert host.end_session("test again") is False, "teardown must be idempotent"
    assert seen == {"ip": None, "active": False}, seen
    assert host.host_state.session_epoch == epoch + 1
    ok("end_session clears the client before stopping streams, and only once")

    # A start that outlives its session (portal prompt answered after the
    # client left) must not spawn encoders, and is not a failed start.
    _fresh_session()
    _FakeStream.started = []

    class _SlowPortal:
        def ensure(self, multiple=True):
            host.end_session("left during the prompt")

        def match_stream(self, i, mon):
            return None

    host.host_state.capture_mode = "portal"
    host.host_state.portal = _SlowPortal()
    st = {"epoch": host.host_state.session_epoch, "failures": 0, "next_attempt": 0.0}
    host._session_tick(_args, st)
    assert _FakeStream.started == [], _FakeStream.started
    assert st["failures"] == 0, "a departed client is not a failed start"
    host.host_state.portal = None
    host.host_state.capture_mode = "x11grab"
    ok("a start interrupted by the session ending spawns nothing and arms no retry")

    # The real race: the client's last window sends WINDOW_CLOSE then GOODBYE,
    # while the session manager ticks. Nothing may start after the first start.
    token = _fresh_session()
    _FakeStream.started = []
    st = {"epoch": host.host_state.session_epoch, "failures": 0, "next_attempt": 0.0}
    host._session_tick(_args, st)
    assert _FakeStream.started == ["Video 0", "Audio"], _FakeStream.started

    ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ctl.bind(("127.0.0.1", 0))
    listener = threading.Thread(target=host.control_listener, args=(ctl,), daemon=True)
    listener.start()
    stop_ticks = threading.Event()

    def _ticker():
        while not stop_ticks.is_set():
            host._session_tick(_args, st)
            time.sleep(0.01)

    ticker = threading.Thread(target=_ticker, daemon=True)
    ticker.start()
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.sendto(f"AUTH {token} WINDOW_CLOSE 0".encode(), ctl.getsockname())
    tx.sendto(f"AUTH {token} GOODBYE".encode(), ctl.getsockname())
    deadline = time.time() + 5
    while host.host_state.session_active and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)                          # let the ticker run past the teardown
    stop_ticks.set()
    ticker.join(2)
    ctl.close()
    tx.close()
    assert not host.host_state.session_active and host.host_state.client_ip is None
    assert _FakeStream.started == ["Video 0", "Audio"], \
        f"streams started during teardown: {_FakeStream.started}"
    assert st["failures"] == 0, "teardown must not arm a 'did not start' retry"
    ok("WINDOW_CLOSE + GOODBYE: no encoder restart and no retry during teardown")

    # NET only records the mode: a start can hold the stream lock across a
    # portal prompt for two minutes, and the control thread used to wait it
    # out (no input, no GOODBYE). Streams built for the old mode restart on
    # the session manager's next tick.
    token = _fresh_session()
    hs = host.host_state
    hs.net_mode = "lan"
    release = threading.Event()

    class _BlockingPortal:
        def ensure(self, multiple=True):
            release.wait(5)

        def match_stream(self, i, mon):
            return None

    hs.capture_mode, hs.portal = "portal", _BlockingPortal()
    st = {"epoch": hs.session_epoch, "failures": 0, "next_attempt": 0.0}
    starter = threading.Thread(target=host._session_tick, args=(_args, st), daemon=True)
    starter.start()
    time.sleep(0.2)                          # now inside ensure(), holding the lock
    ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ctl.bind(("127.0.0.1", 0))
    listener = threading.Thread(target=host.control_listener, args=(ctl,), daemon=True)
    listener.start()
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.sendto(f"AUTH {token} NET vpn".encode(), ctl.getsockname())
    tx.sendto(f"AUTH {token} NET wifi".encode(), ctl.getsockname())
    deadline = time.time() + 1.0
    while hs.net_mode != "wifi" and time.time() < deadline:
        time.sleep(0.02)
    assert hs.net_mode == "wifi", "the control thread blocked behind a portal prompt"
    release.set()
    starter.join(3)
    assert hs.streams_net_mode == "wifi", "a start in progress builds for the new mode"
    hs.portal, hs.capture_mode = None, "x11grab"

    _FakeStream.started = []
    hs.net_mode = "lan"                      # e.g. the client moved off the VPN
    host._session_tick(_args, st)            # stops the wifi-mode streams …
    assert not hs.video_threads
    host._session_tick(_args, st)            # … and starts them for lan
    assert _FakeStream.started[:1] == ["Video 0"] and hs.streams_net_mode == "lan"
    ctl.close()
    tx.close()
    host.end_session("cleanup")
    ok("NET never blocks the control thread; streams are rebuilt for the new mode")

    # Closing one window must not get that monitor restarted mid-session.
    _fresh_session()
    _FakeStream.started = []
    st = {"epoch": host.host_state.session_epoch, "failures": 0, "next_attempt": 0.0}
    host.host_state.closed_monitors.add(0)
    host._session_tick(_args, st)
    assert _FakeStream.started == [], _FakeStream.started
    host.end_session("cleanup")
    ok("a monitor the client closed stays closed for the rest of the session")


# A capture stream that gave up (the share may be gone: a stop-sharing click,
# KWin or the portal restarting) makes the next start negotiate again, instead
# of handing every retry the dead session until the host restarts.
class _RecPortal:
    def __init__(self):
        self.calls = []

    def close(self):
        self.calls.append("close")

    def ensure(self, multiple=True):
        self.calls.append("ensure")

    def match_stream(self, i, mon):
        return None


host.host_state.portal_stale = False
host.StreamThread(["true"], "Audio")._retire()
assert not host.host_state.portal_stale, "only a capture stream marks the share stale"
host.StreamThread(["true"], "Video 0", feeder_cmd=["true"])._retire()
assert host.host_state.portal_stale
with patched(host, StreamThread=_FakeStream, build_audio_cmd=lambda: ["ffmpeg"],
             build_video_cmd=lambda *a, **k: ["ffmpeg"], HOST_ARGS=_args,
             RECONNECT_COOLDOWN=0.2, **_quiet):
    _fresh_session()
    host.host_state.capture_mode, host.host_state.portal = "portal", _RecPortal()
    st = {"epoch": host.host_state.session_epoch, "failures": 0, "next_attempt": 0.0}
    host._session_tick(_args, st)
    assert host.host_state.portal.calls == ["close", "ensure"], host.host_state.portal.calls
    assert not host.host_state.portal_stale
    host.end_session("cleanup")
    host.host_state.portal, host.host_state.capture_mode = None, "x11grab"
ok("a capture stream that gave up makes the next start negotiate the portal again")

# ── 1h. audio follows the default sink and reports what it did ─────────
_SOURCES = ("53\talsa_output.hdmi.monitor\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
            "98\twivrn.sink.monitor\tPipeWire\ts16le 2ch 48000Hz\tRUNNING\n"
            "52\talsa_input.usb.mono\tPipeWire\ts16le 1ch 8000Hz\tSUSPENDED\n")


def _fake_pactl(default_sink, sources=_SOURCES):
    def pactl(*args, timeout=2.0):
        if args[:3] == ("list", "short", "sources"):
            return sources
        if args[:1] == ("get-default-sink",):
            return default_sink
        return None
    return pactl


_env_monitor = os.environ.pop("PULSE_MONITOR", None)
with patched(host, _pactl=_fake_pactl("alsa_output.hdmi\n"),
             HOST_ARGS=argparse.Namespace(audio_source="auto")):
    # The old pick took the RUNNING monitor (here the headset's) regardless
    # of where the host's speakers point.
    assert host._pick_audio_source() == ("alsa_output.hdmi.monitor", "default sink")
with patched(host, _pactl=_fake_pactl(None), HOST_ARGS=argparse.Namespace(audio_source="auto")):
    assert host._pick_audio_source()[0] == "wivrn.sink.monitor", "fallback: the playing monitor"
with patched(host, _pactl=_fake_pactl("alsa_output.hdmi\n"),
             HOST_ARGS=argparse.Namespace(audio_source="wivrn.sink")):
    assert host._pick_audio_source() == ("wivrn.sink.monitor", "pinned")
with patched(host, _pactl=_fake_pactl("alsa_output.hdmi\n"),
             HOST_ARGS=argparse.Namespace(audio_source="alsa_input.usb.mono")):
    assert host._pick_audio_source() == ("alsa_input.usb.mono", "pinned")
os.environ["PULSE_MONITOR"] = "wivrn.sink.monitor"
with patched(host, _pactl=_fake_pactl("alsa_output.hdmi\n"),
             HOST_ARGS=argparse.Namespace(audio_source="auto")):
    assert host._pick_audio_source() == ("wivrn.sink.monitor", "pinned")
del os.environ["PULSE_MONITOR"]
if _env_monitor is not None:
    os.environ["PULSE_MONITOR"] = _env_monitor
ok("audio source: default sink's monitor, not whichever was playing at connect")

with patched(host, StreamThread=_FakeStream, build_video_cmd=lambda *a, **k: ["ffmpeg"],
             HOST_ARGS=argparse.Namespace(audio_source="auto"),
             _pactl=_fake_pactl("alsa_output.hdmi\n"), **_quiet):
    _fresh_session()
    hs = host.host_state
    hs.audio_source = "alsa_output.hdmi.monitor"
    hs.audio_thread = _FakeStream(None, "Audio")
    hs.audio_watch_ts = 0.0
    host._audio_watch()
    assert hs.audio_stats["switches"] == 0, "no move while the default sink is unchanged"

    _FakeStream.started = []
    with patched(host, _pactl=_fake_pactl("wivrn.sink\n")):
        host._audio_watch()                  # rate-limited: too soon after the last look
        assert _FakeStream.started == []
        hs.audio_watch_ts = 0.0
        host._audio_watch()
    assert _FakeStream.started == ["Audio"] and hs.audio_stats["switches"] == 1
    assert hs.audio_thread.name == "Audio"
    ok("a default-sink change mid-session moves the audio capture")

    hs.audio_source = "alsa_output.hdmi.monitor"
    hs.audio_watch_ts = 0.0
    _FakeStream.started = []
    with patched(host, _pactl=_fake_pactl("wivrn.sink\n")):
        hs.session_active = False            # the session ended under the watchdog
        host._audio_watch()
    assert _FakeStream.started == [], "no audio for a session that has ended"
    hs.session_active = True
    host.end_session("cleanup")
    ok("the watchdog never starts audio for a session that already ended")

    # Silence bookkeeping and the end-of-session line.
    host._begin_session_locked("127.0.0.1", "h.265")
    hs.audio_stats["since"] = time.time() - 100
    hs.audio_stats["sources"] = [(0, "a.monitor"), (1, "b.monitor")]
    hs.audio_stats["switches"] = 1
    hs.audio_source = "b.monitor"
    host._audio_silence_event("[silencedetect @ 0x1] [info] silence_start: 5.0")
    assert hs.audio_stats["silent_since"] is not None
    host._audio_silence_event("[silencedetect @ 0x1] [info] silence_end: 45.0 | silence_duration: 40.0")
    assert hs.audio_stats["silence_longest"] == 40.0 and hs.audio_stats["silent_since"] is None
    # An encoder stopped mid-silence (capture moving) closes the silence at
    # end of stream: banked, but not reported as sound coming back.
    _seen = []
    with patched(host.logging, info=lambda *a, **k: _seen.append(a[0] % a[1:])):
        host._audio_silence_event("[silencedetect @ 0x1] [info] silence_start: 50.0")
        host._audio_silence_event("[silencedetect @ 0x1] [info] silence_end: 62.0 | "
                                  "silence_duration: 12.0", ending=True)
    assert not any("again after" in m for m in _seen), _seen
    assert hs.audio_stats["silence_total"] == 52.0
    store = {}
    for line in ("total_size=1000", "total_size=3000", "total_size=500"):   # restart after 3000
        host._progress_update(store, line)
    hs.encoder_stats["Audio"] = store
    hs.stream_restarts["Audio"] = 1
    report = host._audio_report()
    assert "a.monitor → b.monitor (1 source switches)" in report, report
    assert "1 encoder restarts" in report and "silent 52%" in report, report
    assert "0.0 MB sent" in report and "longest 40s" in report, report
    assert store["total_banked"] == 3000.0
    hs.encoder_stats.pop("Audio", None)
    host.end_session("cleanup")
    hs.encoder_stats["Audio"] = {"total_size": 9e9, "total_banked": 9e9}
    host._begin_session_locked("127.0.0.1", "h.265")
    assert "Audio" not in hs.encoder_stats, "a new session starts its totals from zero"
    host.end_session("cleanup")
    ok("audio report: sources, switches, bytes across restarts, restarts, silence")

    # The command captures the picked source and listens for silence.
    with patched(host, _udp_ts_pkt_size=lambda ip: 1128, _path_mtu=lambda ip: 1280):
        host._begin_session_locked("127.0.0.1", "h.265")
        cmd = host.build_audio_cmd()
        host.end_session("cleanup")
    assert cmd[cmd.index("-i") + 1] == "alsa_output.hdmi.monitor"
    assert cmd[cmd.index("-loglevel") + 1] == "level+info"
    assert host.host_state.audio_source == "alsa_output.hdmi.monitor"
    # Fix-plan 24: frames are numbered by sample count before anything else
    # sees them; wall-clock read stamps ran backwards whenever a capture read
    # fell behind. Nothing in the chain may buffer or pad the audio.
    chain = cmd[cmd.index("-af") + 1].split(",")
    assert chain == ["asetpts=N/SR/TB", f"silencedetect=noise={host.AUDIO_SILENCE_DB}dB:"
                                        f"duration={host.AUDIO_SILENCE_SECS}"], chain
    assert cmd.index("-af") < cmd.index("-c:a"), "the filters feed the encoder"
    # Fix-plan 26: the capture reads one Opus frame at a time (s16le stereo
    # 48 kHz = 192 bytes a millisecond), not ffmpeg's default 50 ms.
    _fd = os.environ.pop("LP_OPUS_FD", None)
    try:
        for fd, frag in ((None, "1920"), ("20", "3840")):
            if fd:
                os.environ["LP_OPUS_FD"] = fd
            with patched(host, _udp_ts_pkt_size=lambda ip: 1128, _path_mtu=lambda ip: 1280):
                host._begin_session_locked("127.0.0.1", "h.265")
                cmd = host.build_audio_cmd()
                host.end_session("cleanup")
            assert cmd[cmd.index("-fragment_size") + 1] == frag, (fd, cmd)
            assert cmd.index("-fragment_size") < cmd.index("-f") < cmd.index("-i"), "an input option"
            assert cmd[cmd.index("-frame_duration") + 1] == (fd or "10")
    finally:
        os.environ.pop("LP_OPUS_FD", None)
        if _fd is not None:
            os.environ["LP_OPUS_FD"] = _fd
    ok("audio command: picked source, sample-count timestamps, frame-sized capture reads, "
       "silencedetect, level-tagged log")

_st = host.StreamThread(None, "Audio", cmd_factory=lambda: ["ffmpeg", "-i", "x"])
_st._refresh_cmd()
assert _st.cmd == ["ffmpeg", "-i", "x"]
ok("a stream with a command factory rebuilds its command before each start")

# A GPU feeder run that fails before it was healthy hands over to the CPU
# feeder, and the streams of later sessions start on the CPU one.
host.host_state.portal_gpu_failed = False
with patched(host, STREAM_MAX_RESTARTS=1, **_quiet):
    _gt = host.StreamThread(["false"], "Video 8", feeder_cmd=["true"],
                            fallback=(["true"], ["true"]))
    _gt.run()
assert _gt.cmd == ["true"] and _gt.fallback is None, (_gt.cmd, _gt.fallback)
assert host.host_state.portal_gpu_failed
host.host_state.portal_gpu_failed = host.host_state.portal_stale = False
ok("a GPU feeder that fails early hands over to the CPU feeder")

# The handshake reply names the transport only when the client asked for it.
with patched(host, StreamThread=_FakeStream, build_audio_cmd=lambda: ["ffmpeg"],
             build_video_cmd=lambda *a, **k: ["ffmpeg"], HOST_ARGS=_args, **_quiet):
    with host.host_state.pin_lock:
        _reply = host._begin_session_locked("127.0.0.1", "h.265", caps=frozenset({"RTP"}))
    assert _reply.endswith("\nTRANSPORT rtp") and host.host_state.video_transport == "rtp", _reply
    host.end_session("cleanup")
    with host.host_state.pin_lock:
        _reply = host._begin_session_locked("127.0.0.1", "h.265")
    assert "TRANSPORT" not in _reply and host.host_state.video_transport == "udp", _reply
    host.end_session("cleanup")
ok("the reply says TRANSPORT rtp only to a client that offered it")

# A relay that dies takes its encoder down with it: the stream restarts as a
# pair instead of encoding into a closed loopback port.
_rs = host.StreamThread(["sleep", "30"], "Video 9", relay_cmd=[
    sys.executable, "-c", "import sys; sys.stderr.write('bind failed'); sys.exit(3)"])
assert _rs._start_children()
_ret, _err = _rs._wait_for_exit()
_rs._running = False
_rs._kill_children()
assert _ret == 3 and _err == "relay: bind failed", (_ret, _err)
assert _rs.process.poll() is not None, "the encoder is stopped with its relay"
ok("a stream's relay dying ends the encoder too, so both restart")

# A warning ffmpeg repeats on every frame is logged once, then once a window
# with a count (2026-10-19: ~17 a second for sixteen hours).
_flood = "[libopus @ 0x555e3d2e09c0] [warning] Queue input is backward in time"
_g = host._RepeatGate(window=60.0)
assert _g.admit(_flood, now=0.0) == [_flood]
for _i in range(1, 1000):
    assert _g.admit(_flood.replace("09c0", f"{_i:04x}"), now=_i * 0.05) == []
_other = "[aost#0:0/libopus @ 0x1] Non-monotonic DTS; previous: 10, current: 9; changing to 11."
assert _g.admit(_other, now=50.0) == [_other], "another message is not held back"
_out = _g.admit(_flood, now=61.0)
assert len(_out) == 1 and "repeated 999 more times in the last 61s" in _out[0], _out
assert "Queue input is backward in time" in _out[0], _out
assert _g.admit(_flood, now=62.0) == []
assert any("repeated 2 more times" in s for s in _g.flush(final=True))
assert _g.flush(final=True) == []
assert _g.admit(_other, now=200.0) == [_other], "a quiet message logs again after its window"
_g2 = host._RepeatGate(max_shapes=4)
for _c in "abcdefghij":
    _g2.admit(f"unique message {_c}", now=0.0)
assert len(_g2._shapes) == 4
ok("repeated encoder warnings: once, then a count per window; shapes bounded")


class _FloodProc:
    def __init__(self, lines):
        import io
        self.stderr = io.StringIO("".join(line + "\n" for line in lines))


_warned = []
with patched(host.logging, warning=lambda *a, **k: _warned.append(a[0] % a[1:])):
    host.StreamThread(["ffmpeg"], "Audio")._drain_stderr(_FloodProc([_flood] * 5000))
host.host_state.encoder_stats.pop("Audio", None)
assert len(_warned) == 2, _warned[:5]
assert _warned[0] == f"Audio encoder: {_flood}"
assert "repeated 4999 more times" in _warned[1], _warned[1]
ok("a 5000-line encoder flood reaches the log as two lines")

# The GUI log view takes queued lines in bounded batches.
import logging as _logging          # noqa: E402
import types as _types              # noqa: E402
_h = host.QtLogHandler(maxlen=100)
_h.setFormatter(_logging.Formatter("%(message)s"))
for _i in range(250):
    _h.emit(_logging.LogRecord("t", _logging.WARNING, __file__, 1, f"line {_i}", None, None))
_lines, _skipped = _h.drain()
assert _skipped == 150 and len(_lines) == 100, (_skipped, len(_lines))
assert _lines[0] == "line 150" and _lines[-1] == "line 249"
assert _h.drain() == ([], 0)
for _i in range(120):
    _h.emit(_logging.LogRecord("t", _logging.INFO, __file__, 1, f"next {_i}", None, None))
_shown = []
host.HostWindow._flush_log(_types.SimpleNamespace(_log_handler=_h, append_log=_shown.append))
assert len(_shown) == 1, "one append per flush, not one per record"
_batch = _shown[0].split("\n")
assert _batch[0].startswith("… 20 log lines skipped here") and len(_batch) == 101, _batch[:2]
_shown.clear()
host.HostWindow._flush_log(_types.SimpleNamespace(_log_handler=_h, append_log=_shown.append))
assert _shown == [], "nothing queued, nothing appended"
assert host.GUI_LOG_MAX_LINES > 0
ok("GUI log handler: bounded queue, batched appends, skipped lines counted")

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
        host.host_state.monitors = []     # the session tests above left a fake 1920x1080
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
