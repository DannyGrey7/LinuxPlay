#!/usr/bin/env python3
"""Sequence numbers on the video datagrams, so the client can undo reordering.

On 2026-10-20 the Mac's Wi-Fi delivered 2-13% of the host's video datagrams
out of order (retransmissions arriving up to ~17 ms and 127 datagrams late)
while losing almost none. FFmpeg's TS demuxer reports any out-of-order
datagram as a continuity error, so the client's keyframe gate froze the
picture until the next keyframe: 75% of the time frozen (fix-plan 27).

Host: ffmpeg sends its MPEG-TS to 127.0.0.1, and `seqrelay.py send` (its own
process, so the host's GUI can never hold up video) puts each datagram
behind a 12-byte RTP header (RFC 3550, payload type 33 = MP2T) and sends it
on. ffmpeg's rtp_mpegts muxer can't do this: it fills every RTP payload,
so it holds the tail of each frame until the next frame arrives.

Client: Reorderer puts the datagrams back in sequence order, waiting at most
`wait` seconds for a missing one, strips the header, and the unchanged UDP
mpegts decoder reads plain TS from loopback. Datagrams without an RTP
header (an older host) pass straight through.

    seqrelay.py send LISTEN_PORT DEST_HOST DEST_PORT
"""
import logging
import os
import random
import socket
import struct
import sys
import threading
import time

RTP_HEADER = 12
PT_MP2T = 33
_HDR = struct.Struct("!BBHII")          # V/P/X/CC, M/PT, sequence, timestamp, SSRC


def rtp_packet(seq, timestamp, ssrc, payload):
    return _HDR.pack(0x80, PT_MP2T, seq & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc) + payload


def parse_rtp(data):
    """(seq, ssrc, payload) for an RTP/MP2T datagram, else None (plain TS starts 0x47)."""
    if len(data) < RTP_HEADER or data[0] >> 6 != 2 or data[1] & 0x7F != PT_MP2T:
        return None
    flags, _pt, seq, _ts, ssrc = _HDR.unpack_from(data)
    off = RTP_HEADER + 4 * (flags & 0x0F)
    if flags & 0x10 and len(data) >= off + 4:                 # header extension
        off += 4 + 4 * struct.unpack_from("!H", data, off + 2)[0]
    end = len(data)
    if flags & 0x20 and end > off:                            # padding
        end -= data[-1]
    if end < off:
        return None
    return seq, ssrc, data[off:end]


def run_sender(listen_port, dest_host, dest_port):
    """Host side: number every datagram ffmpeg sends to 127.0.0.1:listen_port."""
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    rx.bind(("127.0.0.1", listen_port))
    rx.settimeout(1.0)
    family = socket.AF_INET6 if ":" in dest_host else socket.AF_INET
    tx = socket.socket(family, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
    dest = (dest_host, dest_port)
    seq = random.getrandbits(16)
    ssrc = random.getrandbits(32)
    parent = os.getppid()
    while True:
        try:
            data = rx.recv(65536)
        except socket.timeout:
            if os.getppid() != parent:                  # the host is gone: so are we
                return
            continue
        ts = int(time.monotonic() * 90000)
        try:
            tx.sendto(rtp_packet(seq, ts, ssrc, data), dest)
        except OSError:
            pass                                        # like overrun_nonfatal: keep going
        seq = (seq + 1) & 0xFFFF


class Reorderer:
    """Client side: RTP datagrams back in sequence order, as plain TS.

    push() returns what can go to the decoder now; due() releases what has
    waited `wait` seconds for a datagram that never came. Counters:
      reordered  gaps filled by a datagram that arrived late
      skipped    datagrams given up on after `wait` (lost, or later still)
      late       datagrams that arrived after they had been given up on
      plain      datagrams without an RTP header, passed through
    """

    RESYNC_AFTER = 16           # this many "old" datagrams in a row = the sender restarted

    def __init__(self, wait=0.05, depth=1024):
        self.wait = wait
        self.depth = depth
        self.expected = None
        self.ssrc = None
        self.held = {}
        self.gap_since = None
        self._behind_run = 0
        self.received = self.reordered = self.skipped = self.late = self.plain = 0

    def deadline(self):
        return None if self.gap_since is None else self.gap_since + self.wait

    def _drain(self, out):
        while self.expected in self.held:
            out.append(self.held.pop(self.expected)[0])
            self.expected = (self.expected + 1) & 0xFFFF
        # The next missing datagram has been missing since the first datagram
        # after it arrived: each gap gets its own `wait`, however the late
        # ones overlap inside a burst.
        self.gap_since = min(t for _p, t in self.held.values()) if self.held else None

    def _flush_all(self, out):
        for seq in sorted(self.held, key=lambda s: (s - self.expected) & 0xFFFF):
            out.append(self.held.pop(seq)[0])
        self.gap_since = None

    def push(self, data, now):
        rtp = parse_rtp(data)
        if rtp is None:
            self.plain += 1
            return [data]
        seq, ssrc, payload = rtp
        self.received += 1
        out = []
        if ssrc != self.ssrc:                          # first datagram, or a new sender
            self._flush_all(out)
            self.ssrc, self.expected, self._behind_run = ssrc, seq, 0
        d = (seq - self.expected) & 0xFFFF
        if d == 0:
            self._behind_run = 0
            out.append(payload)
            self.expected = (seq + 1) & 0xFFFF
            if self.held:
                self.reordered += 1
                self._drain(out)
        elif d < 0x8000:                                # ahead: something is missing
            self._behind_run = 0
            if seq not in self.held:
                self.held[seq] = (payload, now)
            else:
                self.late += 1                          # a duplicate
            if self.gap_since is None:
                self.gap_since = now
            if len(self.held) > self.depth:
                out += self._give_up(now)
        else:                                           # behind: already given up on
            self.late += 1
            self._behind_run += 1
            if self._behind_run >= self.RESYNC_AFTER:
                self._flush_all(out)
                self.expected, self._behind_run = (seq + 1) & 0xFFFF, 0
                out.append(payload)
        return out

    def _give_up(self, now):
        """Stop waiting for the missing datagram(s): resume at the oldest one held."""
        out = []
        oldest = min(self.held, key=lambda s: (s - self.expected) & 0xFFFF)
        self.skipped += (oldest - self.expected) & 0xFFFF
        self.expected = oldest
        self._drain(out)
        return out

    def due(self, now):
        out = []
        while self.held and (now - self.gap_since >= self.wait or len(self.held) > self.depth):
            out += self._give_up(now)
        return out


class ReorderRelay(threading.Thread):
    """Receive the video port, restore order, hand plain TS to 127.0.0.1:forward_port."""

    REPORT_SECS = 60.0

    def __init__(self, listen_port, forward_port, wait=0.05, depth=1024, name="Video"):
        super().__init__(daemon=True, name=f"{name}-reorder")
        self.label = name
        self.forward = ("127.0.0.1", forward_port)
        self.order = Reorderer(wait=wait, depth=depth)
        # No SO_REUSEADDR/PORT: two sockets on one unicast port split the
        # datagrams between them, so a taken port must fail here, loudly.
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.rx.bind(("0.0.0.0", listen_port))
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._stop = threading.Event()
        self._said_mode = False

    def _send(self, datagrams):
        for d in datagrams:
            try:
                self.tx.sendto(d, self.forward)
            except OSError:
                pass                    # the decoder is reopening; it waits for a keyframe anyway

    def run(self):
        o = self.order
        last_report = time.monotonic()
        seen = (0, 0, 0)
        while not self._stop.is_set():
            dl = o.deadline()
            now = time.monotonic()
            self.rx.settimeout(0.5 if dl is None else max(0.0005, dl - now))
            try:
                batch = [self.rx.recv(65536)]
            except socket.timeout:
                batch = []
            except OSError:
                if self._stop.is_set():
                    break
                time.sleep(0.05)
                continue
            # Take everything already queued before giving up on a gap: after
            # a stall (this thread shares the GIL with the decoder and Qt) the
            # missing datagram may be sitting in the socket.
            if batch:
                self.rx.setblocking(False)
                try:
                    while len(batch) < 4096:
                        batch.append(self.rx.recv(65536))
                except (BlockingIOError, InterruptedError):
                    pass
                except OSError:
                    if self._stop.is_set():
                        break
            for data in batch:
                self._send(o.push(data, time.monotonic()))
            if batch and not self._said_mode:
                self._said_mode = True
                logging.info("%s: the host %s.", self.label,
                             "numbers its datagrams (RTP) — late ones are put back in order"
                             if o.received else "sends plain MPEG-TS (no reordering)")
            now = time.monotonic()
            self._send(o.due(now))
            if now - last_report >= self.REPORT_SECS:
                cur = (o.reordered, o.skipped, o.late)
                d = [a - b for a, b in zip(cur, seen)]
                if any(d):
                    logging.info("%s: %d gaps filled by late datagrams, %d datagrams given up, "
                                 "%d too late in the last %.0fs.", self.label, d[0], d[1], d[2],
                                 now - last_report)
                seen, last_report = cur, now

    def stop(self):
        self._stop.set()
        try:
            self.rx.close()
        except OSError:
            pass


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "send":
        try:
            run_sender(int(sys.argv[2]), sys.argv[3], int(sys.argv[4]))
        except KeyboardInterrupt:
            pass
    else:
        sys.exit(__doc__.rsplit("\n\n", 1)[-1])
