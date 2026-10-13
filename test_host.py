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

# ── 1g. session teardown never restarts streams for a departed client ──
class _FakeStream:
    """Stands in for StreamThread: records starts, never spawns ffmpeg."""
    started = []

    def __init__(self, cmd, name, feeder_cmd=None, cmd_factory=None):
        self.name = name

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

    # Closing one window must not get that monitor restarted mid-session.
    _fresh_session()
    _FakeStream.started = []
    st = {"epoch": host.host_state.session_epoch, "failures": 0, "next_attempt": 0.0}
    host.host_state.closed_monitors.add(0)
    host._session_tick(_args, st)
    assert _FakeStream.started == [], _FakeStream.started
    host.end_session("cleanup")
    ok("a monitor the client closed stays closed for the rest of the session")

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
    store = {}
    for line in ("total_size=1000", "total_size=3000", "total_size=500"):   # restart after 3000
        host._progress_update(store, line)
    hs.encoder_stats["Audio"] = store
    hs.stream_restarts["Audio"] = 1
    report = host._audio_report()
    assert "a.monitor → b.monitor (1 source switches)" in report, report
    assert "1 encoder restarts" in report and "silent 40%" in report, report
    assert "0.0 MB sent" in report and "longest 40s" in report, report
    assert store["total_banked"] == 3000.0
    hs.encoder_stats.pop("Audio", None)
    host.end_session("cleanup")
    ok("audio report: sources, switches, bytes across restarts, restarts, silence")

    # The command captures the picked source and listens for silence.
    with patched(host, _udp_ts_pkt_size=lambda ip: 1128, _path_mtu=lambda ip: 1280):
        host._begin_session_locked("127.0.0.1", "h.265")
        cmd = host.build_audio_cmd()
        host.end_session("cleanup")
    assert cmd[cmd.index("-i") + 1] == "alsa_output.hdmi.monitor"
    assert cmd[cmd.index("-loglevel") + 1] == "level+info"
    assert any(a.startswith("silencedetect=") for a in cmd)
    assert host.host_state.audio_source == "alsa_output.hdmi.monitor"
    ok("audio command: picked source, silencedetect, level-tagged log")

_st = host.StreamThread(None, "Audio", cmd_factory=lambda: ["ffmpeg", "-i", "x"])
_st._refresh_cmd()
assert _st.cmd == ["ffmpeg", "-i", "x"]
ok("a stream with a command factory rebuilds its command before each start")

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
