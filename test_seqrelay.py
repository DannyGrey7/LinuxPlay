#!/usr/bin/env python3
"""Numbered video datagrams and the client's reorder relay (fix-plan 27).

On 2026-10-20 the Mac's Wi-Fi delivered up to 13% of the host's video
datagrams out of order, retransmissions arriving up to 127 datagrams and
~17 ms late, and the client froze on every one. These tests pin the
reordering rules, then run the real host relay process into the client
relay over loopback with that kind of reordering.
"""
import os
import random
import socket
import struct
import subprocess
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import seqrelay       # noqa: E402

PASS = []


def ok(name):
    PASS.append(name)
    print(f"  PASS: {name}")


def ts_datagram(n):
    """Six TS packets whose payload says which datagram this is."""
    return b"".join(b"\x47\x01\x00" + bytes([0x10 | (6 * n + k) % 16]) + struct.pack("!I", n)
                    + bytes(180) for k in range(6))


def rtp(seq, payload, ssrc=0x1234):
    return seqrelay.rtp_packet(seq, 0, ssrc, payload)


# ── 1. header ────────────────────────────────────────────────────────
d = ts_datagram(7)
assert seqrelay.parse_rtp(rtp(65535, d)) == (65535, 0x1234, d)
assert seqrelay.parse_rtp(d) is None, "plain TS starts 0x47: not RTP"
assert seqrelay.parse_rtp(b"\x80\x21\x00") is None, "too short"
with_csrc = bytes([0x81]) + rtp(5, d)[1:12] + b"CSRC" + d
assert seqrelay.parse_rtp(with_csrc)[2] == d
ok("RTP/MP2T header round-trips; plain TS and runts are not RTP")


# ── 2. ordering rules ────────────────────────────────────────────────
def names(out):
    return [struct.unpack_from("!I", p, 4)[0] for p in out]


r = seqrelay.Reorderer(wait=0.05)
assert names(r.push(rtp(10, ts_datagram(0)), 0.0)) == [0]
assert names(r.push(rtp(11, ts_datagram(1)), 0.0)) == [1]
ok("in-order datagrams go straight through")

r = seqrelay.Reorderer(wait=0.05)
out = r.push(rtp(100, ts_datagram(0)), 0.0)
out += r.push(rtp(102, ts_datagram(2)), 0.001)      # 101 is late
out += r.push(rtp(103, ts_datagram(3)), 0.002)
assert names(out) == [0] and abs(r.deadline() - 0.051) < 1e-9
out += r.push(rtp(101, ts_datagram(1)), 0.010)
assert names(out) == [0, 1, 2, 3] and r.reordered == 1 and r.deadline() is None
ok("a late datagram is put back in its place, and what waited behind it follows")

r = seqrelay.Reorderer(wait=0.05)
out = r.push(rtp(1, ts_datagram(1)), 0.0) + r.push(rtp(3, ts_datagram(3)), 0.0)
assert r.due(0.049) == []
out += r.due(0.050)
assert names(out) == [1, 3] and r.skipped == 1
assert r.push(rtp(2, ts_datagram(2)), 0.060) == [] and r.late == 1
ok("a datagram that never comes costs at most `wait`; it is dropped if it turns up later")

r = seqrelay.Reorderer(wait=0.05)
out = r.push(rtp(65534, ts_datagram(0)), 0.0) + r.push(rtp(0, ts_datagram(2)), 0.0)
out += r.push(rtp(65535, ts_datagram(1)), 0.0) + r.push(rtp(1, ts_datagram(3)), 0.0)
assert names(out) == [0, 1, 2, 3]
ok("sequence numbers wrap at 65535")

r = seqrelay.Reorderer(wait=0.05)
out = r.push(rtp(500, ts_datagram(0)), 0.0) + r.push(rtp(502, ts_datagram(2)), 0.0)
out += r.push(rtp(9, ts_datagram(9), ssrc=0x9999), 0.001)  # host relay restarted
out += r.push(rtp(10, ts_datagram(10), ssrc=0x9999), 0.002)
assert names(out) == [0, 2, 9, 10] and r.deadline() is None
ok("a restarted sender (new SSRC) is followed at once, holding nothing back")

r = seqrelay.Reorderer(wait=10.0, depth=8)
out = r.push(rtp(0, ts_datagram(0)), 0.0)
for s in range(2, 12):                                # 1 missing, more than depth behind it
    out += r.push(rtp(s, ts_datagram(s)), 0.0)
assert names(out)[:2] == [0, 2] and r.skipped == 1 and len(r.held) <= 8
ok("a full queue gives up on the gap instead of growing")

r = seqrelay.Reorderer()
assert r.push(d, 0.0) == [d] and r.plain == 1
ok("plain MPEG-TS from an older host passes through untouched")

r = seqrelay.Reorderer()
out = r.push(rtp(4, ts_datagram(4)), 0.0) + r.push(rtp(4, ts_datagram(4)), 0.0)
assert names(out) == [4] and r.late == 1
ok("a duplicate is dropped")


# ── 3. the real relays, end to end over loopback ────────────────────
def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


host_in, net_port, decoder_port = free_udp_port(), free_udp_port(), free_udp_port()
decoder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
decoder.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
decoder.bind(("127.0.0.1", decoder_port))
decoder.settimeout(2.0)

# The "network": catches the host relay's output so it can be reordered.
wire = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
wire.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
wire.bind(("127.0.0.1", net_port))
wire.settimeout(2.0)

sender = subprocess.Popen([sys.executable, os.path.join(HERE, "seqrelay.py"), "send",
                           str(host_in), "127.0.0.1", str(net_port)])
try:
    ffmpeg = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    time.sleep(0.5)                                   # let the relay bind
    N = 1200                                          # three 400-datagram keyframes
    for n in range(N):
        ffmpeg.sendto(ts_datagram(n), ("127.0.0.1", host_in))
        if n % 100 == 99:
            time.sleep(0.005)                         # don't outrun loopback buffers
    on_wire = []
    while len(on_wire) < N:
        on_wire.append(wire.recv(65536))
finally:
    sender.terminate()
    sender.wait(timeout=5)
seqs = [seqrelay.parse_rtp(p)[0] for p in on_wire]
assert all((b - a) & 0xFFFF == 1 for a, b in zip(seqs, seqs[1:])), "consecutive sequence numbers"
assert [seqrelay.parse_rtp(p)[2] for p in on_wire] == [ts_datagram(n) for n in range(N)]
ok("the host relay numbers ffmpeg's datagrams in order and leaves the TS alone")

# Reorder like the 2026-10-20 Wi-Fi: inside bursts, single datagrams held
# back 1-127 places (~6% of them).
rng = random.Random(27)
order = list(range(N))
for i in sorted(rng.sample(range(1, N - 130), 70), reverse=True):   # the relay starts at the first
    late = order.pop(i)
    order.insert(i + rng.randint(1, 127), late)
relay_port = free_udp_port()
relay = seqrelay.ReorderRelay(relay_port, decoder_port, wait=0.25, name="Test")   # ordering, not timing
relay.start()
try:
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for k, i in enumerate(order):
        out.sendto(on_wire[i], ("127.0.0.1", relay_port))
        if k % 100 == 99:
            time.sleep(0.005)
    got = []
    try:
        while len(got) < N:
            got.append(decoder.recv(65536))
    except socket.timeout:
        pass
finally:
    relay.stop()
assert len(got) == N, (len(got), vars(relay.order) | {"held": len(relay.order.held)},
                       "first seq", seqs[0])
assert order != sorted(order)
assert got == [ts_datagram(n) for n in range(N)], "the decoder sees every datagram, in order"
assert relay.order.reordered > 0 and relay.order.skipped == 0, vars(relay.order)
ok(f"the client relay undoes Wi-Fi-style reordering: {relay.order.reordered} gaps filled, "
   f"nothing given up")


# ── 4. the client wires the decoder to the relay ─────────────────────
try:
    import client     # noqa: E402
except ImportError as e:          # a host without the client's GUI stack
    print(f"  SKIP: client wiring — {e}")
    print(f"\nALL {len(PASS)} SEQRELAY TESTS PASSED (1 skipped)")
    sys.exit(77)


class _Win:
    host_ip, udp_port, monitor_index = "127.0.0.1", 5000, 0


w = _Win()
w.video_relay = object()
assert client.MainWindow._video_url_for_path(w).startswith(
    f"udp://@127.0.0.1:{5000 + client.VIDEO_RELAY_OFFSET}?"), client.MainWindow._video_url_for_path(w)
w.video_relay = None
assert client.MainWindow._video_url_for_path(w).startswith("udp://@0.0.0.0:5000?")
os.environ["LINUXPLAY_VIDEO_RELAY"] = "0"
try:
    client.MainWindow._start_video_relay(w)
    assert w.video_relay is None
finally:
    os.environ.pop("LINUXPLAY_VIDEO_RELAY", None)
ok("the decoder reads the relay's loopback port; LINUXPLAY_VIDEO_RELAY=0 reads the port directly")

print(f"\nALL {len(PASS)} SEQRELAY TESTS PASSED")
