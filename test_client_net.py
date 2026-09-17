#!/usr/bin/env python3
"""Client-side network hardening tests.

Covers the trust boundary on the client's inbound UDP sockets (heartbeat/STATS),
the stats-overlay crash path, the CPU YUV fallback with odd frame dimensions
and the portal feeder's framerate handling.

Sections that need a socket this machine cannot own are skipped; the runner
reports exit 77 as SKIP.
"""
import os
import socket
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import client         # noqa: E402
import portal_capture as pc   # noqa: E402

PASS, SKIPPED = [], []


def ok(name):
    PASS.append(name)
    print(f"  PASS: {name}")


def skip(name, why):
    SKIPPED.append(name)
    print(f"  SKIP: {name} — {why}")


# ── 1. STATS parsing refuses values the graph maths cannot take ──────
stats = client._parse_host_stats("STATS inf nan 512.0 29.9 1e999 4.5 8000 3 900")
assert stats is not None
assert "cpu" not in stats and "gpu" not in stats and "rtt" not in stats, stats
assert stats["mem"] == 512.0 and stats["jitter"] == 4.5, stats
assert stats["enc_kbps"] == 8000 and stats["drops"] == 3.0, stats
assert client._parse_host_stats("STATS 12.0 3.0 900.0")["cpu"] == 12.0
assert client._parse_host_stats("STATS") is None
ok("non-finite STATS fields (inf/nan/1e999) are dropped, finite ones parsed")


# A single bad sample must not abort paintEvent (PyQt turns an exception there
# into qFatal, which killed the whole client).
from PyQt5.QtWidgets import QApplication                    # noqa: E402
from PyQt5.QtGui import QImage, QPainter                    # noqa: E402

_app = QApplication.instance() or QApplication([])
_overlay = client.StatsOverlay(lambda: {})
_series = _overlay.series["hcpu"]
_series.push(float("nan"))
_series.push(float("inf"))
_series.push(42.0)
_img = QImage(400, 400, QImage.Format_ARGB32)
_painter = QPainter(_img)
try:
    _overlay._draw_series(_painter, _series, 0, 0, 100, 50)   # must not raise
finally:
    _painter.end()
ok("a non-finite sample cannot crash the overlay's paint path")


# ── 2. the heartbeat responder only ever answers the negotiated host ──
assert client._host_source_ips("192.0.2.123") == {"192.0.2.123"}
assert "127.0.0.1" in client._host_source_ips("localhost")
ok("host source set comes from the address the user connected to")


def _heartbeat_probe(host_ip, port, token, source_host="127.0.0.1"):
    """Start a responder on `port` and send it one PING; return the reply."""
    with _patched_client_port(port):
        client.CLIENT_STATE["token"] = token
        client.heartbeat_responder(host_ip)
        time.sleep(0.3)                 # let the socket bind
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.5)
        try:
            s.sendto(b"PING 1234.5", (source_host, port))
            try:
                data, _ = s.recvfrom(256)
                return data.decode("utf-8", errors="replace")
            except socket.timeout:
                return None
        finally:
            s.close()


import contextlib                                           # noqa: E402


@contextlib.contextmanager
def _patched_client_port(port):
    old = client.UDP_HEARTBEAT_PORT
    client.UDP_HEARTBEAT_PORT = port
    try:
        yield
    finally:
        client.UDP_HEARTBEAT_PORT = old


def _port_usable(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


_probe_port = 17004
if not (_port_usable(_probe_port) and _port_usable(_probe_port + 1)):
    skip("heartbeat source filtering", "test ports are busy")
else:
    # A PING from a peer that is not the host must not be answered: the PONG
    # carries the session token.
    _reply = _heartbeat_probe("192.0.2.123", _probe_port, "sekrit-token")
    assert _reply is None, f"replied to a non-host peer with {_reply!r}"
    ok("a PING from anyone but the host gets no PONG (no token disclosure)")

    # ... and the host itself still gets one, token included.
    _reply = _heartbeat_probe("localhost", _probe_port + 1, "sekrit-token")
    assert _reply is not None and _reply.startswith("PONG sekrit-token"), repr(_reply)
    assert "1234.5" in _reply, "the timestamp must be echoed for the RTT measurement"
    ok("the negotiated host still receives its PONG with the echoed timestamp")

# STATS from a non-host peer must not reach the overlay either.
_reply = None
_stats_seen = {}
if _port_usable(_probe_port + 2):
    with _patched_client_port(_probe_port + 2):
        client.CLIENT_STATE["host_stats"] = None
        client.heartbeat_responder("192.0.2.123")
        time.sleep(0.3)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(b"STATS 99.0 98.0 1.0 60.0 1.0 1.0 1000 0 0", ("127.0.0.1", _probe_port + 2))
            time.sleep(0.4)
        finally:
            s.close()
    assert client.CLIENT_STATE.get("host_stats") is None, \
        "telemetry from a non-host peer was accepted"
    ok("STATS from a non-host peer is ignored")
else:
    skip("heartbeat STATS filtering", "test port is busy")


# ── 3. the CPU YUV fallback handles odd frame dimensions ────────────
import numpy as np                                          # noqa: E402

# 1079 luma rows against 540 chroma rows: the old floor division computed a
# 1:1 ratio and left the chroma planes smaller than luma, so np.stack raised
# inside paintGL. The fallback is what runs when the shader cannot compile.
_w, _h = 96, 1079
_y = np.zeros((_h, _w), dtype=np.uint8)
_u = np.zeros(((_h + 1) // 2, _w // 2), dtype=np.uint8)
_v = np.zeros_like(_u)
_planes = (
    (_y.reshape(-1), _w, _h, _w),
    (_u.reshape(-1), _w // 2, _u.shape[0], _w // 2),
    (_v.reshape(-1), _w // 2, _v.shape[0], _w // 2),
)
_rgb = client._yuv_planes_to_rgb(("yuv", _planes, _w, _h, False, None))
assert _rgb.shape == (_h, _w, 3), _rgb.shape
ok("odd frame heights (1079) survive the CPU YUV fallback")

# nv12 with odd dimensions takes the interleaved-chroma branch.
_uv = np.zeros(((_h + 1) // 2, _w // 2, 2), dtype=np.uint8)
_cpu_planes = (
    (_y.reshape(-1), _w, _h, _w),
    (_uv.reshape(-1), _w // 2, _uv.shape[0], _w),
)
_rgb = client._yuv_planes_to_rgb(("yuv", _cpu_planes, _w, _h, False, None))
assert _rgb.shape == (_h, _w, 3), _rgb.shape
ok("odd frame heights survive the nv12 CPU fallback too")


# ── 4. portal feeder framerate handling ─────────────────────────────
assert pc.parse_framerate("30") == 30
assert pc.parse_framerate("29.97") == 29
assert pc.parse_framerate(" 60 ") == 60
assert pc.parse_framerate("junk") == 60
assert pc.parse_framerate("junk", 0) == 0
assert pc.parse_framerate("0") == 60
assert pc.parse_framerate(None) == 60
ok("fractional/junk framerates resolve instead of raising in the feeder")

_stream = {"node": 7, "w": 1707, "h": 1067}
_feeder = pc.build_feeder_cmd(_stream, fps="29.97")
assert any("framerate=29/1" in a for a in _feeder), _feeder
assert "videorate" in _feeder, _feeder
_feeder_junk = pc.build_feeder_cmd(_stream, fps="junk")     # must not raise
assert any("framerate=60/1" in a for a in _feeder_junk), _feeder_junk
assert "videorate" not in pc.build_feeder_cmd(_stream), "no fps asked for, no rate cap"
ok("feeder caps and caps-negotiation survive odd framerate values")

# A failed negotiation must not leave a half-built session behind: the next
# ensure() would return True immediately with empty streams and the portal
# would never prompt again.
_p = pc.PortalCapture()
_p.session = "/org/freedesktop/portal/desktop/session/1_2/lp"
_p.streams = [{"node": 1, "w": 10, "h": 10, "x": 0, "y": 0}]
_p.conn = None
_p._reset_session()
assert _p.session is None and _p.streams == [] and _p.conn is None
ok("a failed portal negotiation cannot poison the next attempt")


# ── 5. clipboard: listener → queue → GUI-thread apply ────────────────
# The listener runs in a plain thread, so it must not touch QClipboard: it
# queues the text and MainWindow's 10 ms timer applies it on the GUI thread.
# Both halves are exercised here without touching the real clipboard.
class _FakeClipboard:
    def __init__(self):
        self.value = ""

    def text(self):
        return self.value

    def setText(self, text):
        self.value = text


class _FakeQApp:
    _cb = _FakeClipboard()

    @classmethod
    def clipboard(cls):
        return cls._cb


class _FakeWidget:
    def __init__(self):
        self.ignore_clipboard = False
        self.last_clipboard = ""


class _FakeWindow:
    def __init__(self):
        self.video_widget = _FakeWidget()


_win = _FakeWindow()
while not client.CLIPBOARD_INBOX.empty():        # start clean
    client.CLIPBOARD_INBOX.get_nowait()
client.CLIPBOARD_INBOX.put("from the host")
_orig_qapp = client.QApplication
client.QApplication = _FakeQApp
try:
    client.MainWindow._drain_clipboard_inbox(_win)
finally:
    client.QApplication = _orig_qapp
assert _FakeQApp._cb.value == "from the host", _FakeQApp._cb.value
assert _win.video_widget.ignore_clipboard is False, "the echo guard must be reset"
assert _win.video_widget.last_clipboard == "from the host"
ok("queued clipboard text is applied on the GUI thread with the echo guard armed")

_clip_port_free = _port_usable(_probe_port + 3)
if not _clip_port_free:
    skip("clipboard listener", "test port is busy")
else:
    _orig_clip_port = client.UDP_CLIPBOARD_PORT
    client.UDP_CLIPBOARD_PORT = _probe_port + 3
    client.CLIENT_STATE["token"] = "tok-clip"
    while not client.CLIPBOARD_INBOX.empty():
        client.CLIPBOARD_INBOX.get_nowait()
    try:
        client.clipboard_listener()
        time.sleep(0.3)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(b"AUTH wrong-token CLIPBOARD_UPDATE HOST nope",
                     ("127.0.0.1", _probe_port + 3))
            s.sendto(b"AUTH tok-clip CLIPBOARD_UPDATE HOST hello-from-host",
                     ("127.0.0.1", _probe_port + 3))
            _deadline = time.time() + 3
            _got = None
            while time.time() < _deadline and _got is None:
                try:
                    _got = client.CLIPBOARD_INBOX.get_nowait()
                except Exception:
                    time.sleep(0.05)
        finally:
            s.close()
    finally:
        client.UDP_CLIPBOARD_PORT = _orig_clip_port
    assert _got == "hello-from-host", _got
    assert client.CLIPBOARD_INBOX.empty(), "the wrong-token update must be dropped"
    ok("clipboard listener queues token-authenticated updates and drops the rest")


print(f"\nALL {len(PASS)} CLIENT NET TESTS PASSED"
      + (f" ({len(SKIPPED)} skipped)" if SKIPPED else ""))
if SKIPPED:
    sys.exit(77)
