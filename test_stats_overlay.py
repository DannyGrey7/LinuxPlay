#!/usr/bin/env python3
"""Stats overlay + telemetry tests (offscreen Qt; no display required)."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import client   # noqa: E402
import host     # noqa: E402

PASS = []
def ok(name):
    PASS.append(name)
    print(f"  PASS: {name}")


# ── 1. host STATS payload → client parser round trip ──
host.host_state.last_rtt_ms, host.host_state.last_jitter_ms = 4.25, 0.75
host.host_state.encoder_stats = {
    "Video 0": {"fps": 59.9, "bitrate_kbps": 6211.0, "drop_frames": 7.0, "frame": 9001.0},
    "Video 1": {"fps": 30.0, "bitrate_kbps": 111.0},
    "Audio": {"fps": 999.0},
}
payload = host._stats_payload(13.4, 88.0, 2048.5)
stats = client._parse_host_stats(payload)
assert len(payload.split()) == 10, payload
assert stats["cpu"] == 13.4 and stats["gpu"] == 88.0 and stats["mem"] == 2048.5
assert abs(stats["fps"] - 59.9) < 0.1, stats
assert abs(stats["rtt"] - 4.2) < 0.1 and abs(stats["jitter"] - 0.75) < 0.1, stats
assert stats["enc_kbps"] == 6211.0 and stats["drops"] == 7.0 and stats["frame"] == 9001.0
# the lowest-indexed video stream wins; audio never leaks in
assert host._video_encoder_stats()["stream"] == "Video 0"
ok("host STATS payload round-trips into the client's parser")


# ── 2. parser tolerance: old hosts, junk fields, short lines ──
assert client._parse_host_stats("STATS 12.5 88.0 2048.0 59.9")["cpu"] == 12.5
assert client._parse_host_stats("STATS 1 x 3") == {"cpu": 1.0, "mem": 3.0}
assert client._parse_host_stats("PONG abcd") is None
assert client._parse_host_stats("STATS") is None
ok("parser tolerates old layouts, junk fields and short lines")


# ── 3. decode loop counts bytes / packets / keyframes / frames ──
class _FakePacket:
    def __init__(self, size, keyframe, frames):
        self.size, self.is_keyframe, self._frames = size, keyframe, frames

    def decode(self):
        return [object()] * self._frames


class _FakeContainer:
    def __init__(self, packets):
        self._packets = packets
        self.kwargs = None

    def demux(self, **kwargs):
        self.kwargs = kwargs
        return iter(self._packets)


container = _FakeContainer([_FakePacket(1000, True, 1), _FakePacket(500, False, 2)])
counters = client.StreamCounters()
frames = list(client._counted_decode(container, counters, video=0))
assert len(frames) == 3, "every decoded frame must still be yielded"
assert counters.snapshot() == (1500, 2, 1, 3), counters.snapshot()
assert container.kwargs == {"video": 0}, container.kwargs
ok("decode loop counts bytes, packets, keyframes and frames")


# ── 4. sparkline series: bounded window, peak, junk tolerance ──
series = client._Series("x", "u", None)
for i in range(client.STATS_HISTORY + 10):
    series.push(i)
assert len(series.values) == client.STATS_HISTORY, "history must stay bounded"
assert series.latest == client.STATS_HISTORY + 9
assert series.peak == series.latest
before = series.values[:]
series.push(None)
series.push("nonsense")
assert series.values == before, "junk samples must be ignored"
ok("sparkline series keeps a rolling one-minute window")


# ── 5. the overlay renders a populated panel (offscreen) ──
import numpy as np                                    # noqa: E402
from PyQt5.QtWidgets import QApplication              # noqa: E402
from PyQt5.QtGui import QImage                        # noqa: E402

app = QApplication.instance() or QApplication([])

calls = []
def provider():
    n = len(calls)
    calls.append(n)
    return {
        "mbps": 38 + (n % 9), "enc": 42.0, "fps": 59.4,
        "rtt": 4.0 + (n % 4) * 0.5, "jitter": 0.4 + (n % 3) * 0.2, "dec": 3.1,
        "hcpu": 22.0, "hgpu": 61.0, "ccpu": 9.0, "cgpu": 14.0, "drop": 0.0,
        "headline": "LinuxPlay · 100.76.206.96:5000 · 2560x1440",
        "info": ["link vpn · connected · 00:42 up · 0 restarts",
                 "decode VAAPI · render OpenGL",
                 "host encode 42.0 Mb/s @ 60 fps · cpu 22% gpu 61%",
                 "received 210 MB · keyframes 1/s"],
    }

overlay = client.StatsOverlay(provider)
for _ in range(client.STATS_HISTORY):
    overlay._sample()
assert len(calls) == client.STATS_HISTORY
assert len(overlay.series["mbps"].values) == client.STATS_HISTORY
assert overlay.series["rtt"].values[-1] == 4.0 + ((client.STATS_HISTORY - 1) % 4) * 0.5
assert overlay.headline.startswith("LinuxPlay") and len(overlay.info) == 4

img = QImage(overlay.size(), QImage.Format_ARGB32)
img.fill(0)
overlay.render(img)
buf = img.constBits().asarray(img.byteCount())
pixels = np.frombuffer(buf, dtype=np.uint8).reshape(img.height(), img.width(), 4)
opaque = int((pixels[:, :, 3] > 200).sum())
colours = len(np.unique(pixels.reshape(-1, 4), axis=0))
assert opaque > 20000, f"panel looks blank ({opaque} opaque pixels)"
assert colours > 40, f"no graphs or text drawn ({colours} distinct colours)"
scratch = os.environ.get("PI_SCRATCH_DIR") or "/tmp"
png = os.path.join(scratch, "linuxplay_stats_overlay.png")
img.save(png)
ok(f"overlay renders a populated panel ({opaque} px, {colours} colours → {png})")


# ── 6. a hidden overlay draws nothing; a provider error is not fatal ──
hidden = client.StatsOverlay(provider)
before = list(hidden.series["mbps"].values)
hidden._sample()                       # not started: no timers, but sampling still works
assert hidden._sample_timer.isActive() is False
assert hidden._paint_timer.isActive() is False
broken = client.StatsOverlay(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
broken._sample()                       # must swallow the error
assert broken.series["mbps"].values == []
broken.start()
assert broken._sample_timer.isActive() and broken._paint_timer.isActive()
broken.stop()
assert not broken._sample_timer.isActive() and not broken._paint_timer.isActive()
assert hidden.series["mbps"].values != before
ok("overlay timers start/stop cleanly and a broken provider cannot crash it")


# ── 7. wiring: F1 toggles locally and never reaches the host ──
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "client.py"),
           encoding="utf-8").read()
assert "toggle_overlay=self.toggle_stats" in src, "F1 must be wired to the overlay"
assert "Qt.Key_F1" in src and "if callable(self.toggle_overlay)" in src
f1_body = src.split("Qt.Key_F1")[1].split("key_name = self._get_key_name")[0]
assert "return" in f1_body, "F1 must not fall through to input forwarding"
assert 'p.add_argument("--stats"' in src, "the overlay needs a startup flag"
ok("F1 is a local overlay toggle and never reaches the host")


# ── 8b. host side of the RTT measurement ──
host.host_state.client_ip = "192.0.2.50"
host.host_state.session_token = "tok-rtt"
host.host_state.last_pong_ts = 0.0
host.host_state.last_rtt_ms = 0.0
host.host_state.last_jitter_ms = 0.0
assert host._handle_pong("PONG tok-rtt 1000.0", "192.0.2.50", 1000.004) is True
assert abs(host.host_state.last_rtt_ms - 4.0) < 0.01, host.host_state.last_rtt_ms
assert host.host_state.last_pong_ts == 1000.004
# jitter is an EWMA over successive samples
host._handle_pong("PONG tok-rtt 1000.0", "192.0.2.50", 1000.014)      # rtt 14ms
assert host.host_state.last_jitter_ms > 0.0, "jitter must react to variation"
# rejections: wrong peer, wrong token, malformed
assert host._handle_pong("PONG tok-rtt 1000.0", "192.0.2.99", 1000.004) is False
assert host._handle_pong("PONG wrong-token 1000.0", "192.0.2.50", 1000.004) is False
assert host._handle_pong("PONG", "192.0.2.50", 1000.004) is False
assert host._handle_pong("PONG tok-rtt notanumber", "192.0.2.50", 1000.004) is True
# a 2-field PONG (older client) still counts as a heartbeat, without an RTT sample
rtt_seen = host.host_state.last_rtt_ms
assert host._handle_pong("PONG tok-rtt", "192.0.2.50", 1001.0) is True
assert host.host_state.last_rtt_ms == rtt_seen
# an absurd timestamp must not poison the reading
host._handle_pong("PONG tok-rtt 0.0", "192.0.2.50", 1002.0)
assert host.host_state.last_rtt_ms == rtt_seen, "implausible RTT must be ignored"
host.host_state.client_ip = None
host.host_state.session_token = None
ok("host measures RTT/jitter from the echoed PONG and rejects bad ones")


# ── 8. wire test: the heartbeat listener answers PING and ingests STATS ──
import socket as _socket     # noqa: E402
import time as _time         # noqa: E402

CLIENT_STATE = client.CLIENT_STATE
CLIENT_STATE["token"] = "tok-heartbeat"
CLIENT_STATE["host_stats"] = {}
client.heartbeat_responder("127.0.0.1")
_time.sleep(0.4)             # let the responder bind UDP 7004
probe = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
probe.settimeout(3)
try:
    probe.sendto(b"PING 1700000000.123456", ("127.0.0.1", client.UDP_HEARTBEAT_PORT))
    pong, _ = probe.recvfrom(512)
    parts = pong.decode().split()
    assert parts[0] == "PONG" and parts[1] == "tok-heartbeat", pong
    assert parts[2] == "1700000000.123456", f"timestamp must be echoed back: {pong}"

    probe.sendto(b"STATS 11.0 42.0 1024.0 59.5 3.5 0.4 5000 2 300",
                 ("127.0.0.1", client.UDP_HEARTBEAT_PORT))
    for _ in range(40):
        if CLIENT_STATE["host_stats"]:
            break
        _time.sleep(0.05)
    stats = CLIENT_STATE["host_stats"]
    assert stats.get("enc_kbps") == 5000.0 and stats.get("rtt") == 3.5, stats
    assert CLIENT_STATE["last_heartbeat"] > 0.0, "PING must refresh the heartbeat clock"
finally:
    probe.close()
ok("wire test: PING is answered with the timestamp echoed, STATS is ingested")


print(f"\nALL {len(PASS)} STATS OVERLAY TESTS PASSED")
