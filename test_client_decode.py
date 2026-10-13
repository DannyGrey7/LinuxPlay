#!/usr/bin/env python3
"""The client's decode path under loss: keyframe gate, counters, hardware resets.

What a lost datagram used to cost (seen in a 51.8 h session): the HEVC decoder
refused every frame until the next keyframe while logging ~9 lines for each,
the pacing report said "0 corrupt" throughout, and the first damaged frame
that reached VideoToolbox sent decoding to the CPU for good. These tests pin
the replacement behaviour:

  1. the keyframe gate holds damaged stretches back and counts them;
  2. the hardware-decode state machine resets, falls back, and retries;
  3. FFmpeg's expected chatter is tallied, the unexpected still printed;
  4. live: a real HEVC stream (4 slices, GOP 15, no B-frames, like the host)
     cut into 1128-byte datagrams with bursts dropped decodes without the
     decoder's avalanche — and, where VideoToolbox exists, without an error.

Section 4 needs an ffmpeg with an HEVC encoder; it is skipped (exit 77)
otherwise.
"""
import io
import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client  # noqa: E402

PASS, SKIPPED = [], []


def ok(name):
    PASS.append(name)
    print(f"  PASS: {name}")


def skip(name, why):
    SKIPPED.append(name)
    print(f"  SKIP: {name} — {why}")


# ── 1. keyframe gate on synthetic packets ─────────────────────────────
class _Pkt:
    def __init__(self, key=False, corrupt=False, frames=1, size=1000):
        self.is_keyframe, self.is_corrupt = key, corrupt
        self.size, self._frames = size, frames

    def decode(self):
        return [object()] * self._frames


class _Box:
    def __init__(self, packets):
        self.packets = packets

    def demux(self, **_):
        return iter(self.packets)


P, K, X = _Pkt, (lambda **k: _Pkt(key=True, **k)), (lambda **k: _Pkt(corrupt=True, **k))
stream = [P(), P(),            # joined mid-GOP: undecodable, skipped quietly
          K(), P(), P(),       # first keyframe: decode from here
          X(), P(), P(),       # a hole: hold back until the next keyframe
          K(), P(),
          P(frames=0)]         # the decoder swallowed one on its own
c = client.StreamCounters()
gate = client._KeyframeGate(c)
frames = list(client._counted_decode(_Box(stream), c, gate=gate))
assert len(frames) == 3 + 2, len(frames)
assert (c.damaged, c.skipped, c.refused) == (1, 3, 1), (c.damaged, c.skipped, c.refused)
assert c.freezes == 1 and c.freeze_ms >= 0.0
assert c.packets == len(stream) and c.keyframes == 2
ok("gate: joins at the first keyframe, holds a damaged stretch, counts each kind once")

c = client.StreamCounters()
frames = list(client._counted_decode(_Box(stream), c))
assert len(frames) == 10 and c.damaged == c.skipped == 0
ok("without a gate the loop still decodes everything (overlay/test callers)")

c = client.StreamCounters()
gate = client._KeyframeGate(c)
gate.since -= 11.0                       # as if 11 s passed with no keyframe flag
logging.disable(logging.WARNING)
frames = list(client._counted_decode(_Box([P(), P(), P()]), c, gate=gate))
logging.disable(logging.NOTSET)
assert len(frames) == 3 and gate.disabled
ok("a stream that never flags keyframes is not held back forever")

c.note_freeze(120.0)
c.note_freeze(40.0)
assert c.take_freeze_max() == 120.0 and c.take_freeze_max() == 0.0
ok("freeze bookkeeping: total, count, and a per-window longest")


# ── 2. hardware decode: reset, fall back, retry ────────────────────────
dt = client.DecoderThread("udp://@0.0.0.0:1", {"hwaccel": "videotoolbox"})
logging.disable(logging.WARNING)
try:
    assert dt._hw_failed("bad data") is False and dt._hw_retry_at == 0.0
    ok("hardware that never decoded a frame: CPU for the session, no retry flapping")

    dt = client.DecoderThread("udp://@0.0.0.0:1", {"hwaccel": "videotoolbox"})
    dt._hw_frames = 500
    assert dt._hw_failed("bad data") is True
    assert dt._hw_failed("bad data") is True
    assert dt.hw_resets == 2
    assert dt._hw_failed("bad data") is False, "a third failure inside the window falls back"
    assert dt._hw_retry_at > time.time()
    ok("working hardware: damaged input resets the decoder, repeated failures fall back")

    dt._sw_fallback_done = True
    dt.decoder_opts.pop("hwaccel", None)
    dt._maybe_restore_hw()
    assert "hwaccel" not in dt.decoder_opts, "must not retry before the retry time"
    dt._hw_retry_at = time.time() - 1
    dt._maybe_restore_hw()
    assert dt.decoder_opts.get("hwaccel") == "videotoolbox" and not dt._sw_fallback_done
    assert dt._hw_failures == []
    ok("the CPU fallback is temporary: hardware is retried after the retry time")
finally:
    logging.disable(logging.NOTSET)


# ── 3. FFmpeg's log: expected chatter tallied, the rest passed on ──────
seen = []


class _Sink(logging.Handler):
    def emit(self, record):
        seen.append(record.getMessage())


root = logging.getLogger()
sink = _Sink()
root.addHandler(sink)
root.setLevel(logging.INFO)
try:
    f = client._LibavLogFilter()
    rec = lambda msg, lvl=logging.ERROR: logging.LogRecord("libav.hevc", lvl, "", 0, msg, None, None)
    for _ in range(5):
        f.emit(rec("First slice in a frame missing."))
        f.emit(rec("Could not find ref with POC 13"))
    f.emit(rec("Packet corrupt (stream = 0, dts = 9).", logging.WARNING))
    f.emit(rec("Stream #0: not enough frames to estimate rate; consider increasing probesize",
               logging.WARNING))
    f.emit(rec("Something nobody has seen before"))
    assert f.decoder_msgs == 10, f.decoder_msgs
    assert seen.count("First slice in a frame missing.") == 1
    assert seen.count("Could not find ref with POC 13") == 1
    assert not any("Packet corrupt" in m for m in seen)
    assert not any(m.startswith("Stream #0: not enough frames") for m in seen)
    assert "Something nobody has seen before" in seen
    f._last_summary = 0.0
    f.emit(rec("First slice in a frame missing."))
    assert any(m.startswith("Decoder: ") and "slice without frame" in m for m in seen), seen[-2:]
    ok("decoder chatter: first of each kind shown, then a per-minute summary")
finally:
    root.removeHandler(sink)


# ── 4. live: a lossy HEVC stream through the real decoder ──────────────
def _hevc_encoder():
    if not shutil.which("ffmpeg"):
        return None
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                         capture_output=True, text=True).stdout
    return "libx265" if " libx265 " in out else None


def _client_opts():
    # Exactly what the client opens the stream with (non-ultra path).
    return client.DecoderThread("udp://@0.0.0.0:1", {}).decoder_opts


enc = _hevc_encoder()
if not enc:
    skip("lossy HEVC stream through the decoder", "no ffmpeg with libx265")
    skip("a frame reaches the decoder without waiting for the next", "no ffmpeg with libx265")
else:
    import av
    import socket
    import threading

    import host                                          # the muxer flags it streams with
    mux = host._mpegts_ll_mux_flags()
    assert "-omit_video_pes_length" in mux, "the host must write video PES lengths"

    tmp = tempfile.mkdtemp(prefix="lp_decode_")
    clip = os.path.join(tmp, "clip.ts")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-t", "8",
         "-c:v", "libx265", "-preset", "ultrafast",
         "-x265-params", "slices=4:keyint=15:min-keyint=15:bframes=0:scenecut=0:log-level=error",
         "-pix_fmt", "yuv420p", *mux, "-f", "mpegts", clip], check=True)
    data = open(clip, "rb").read()
    grams = [data[i:i + 1128] for i in range(0, len(data), 1128)]
    rnd = random.Random(7)
    drop = set()
    for start in rnd.sample(range(20, len(grams) - 20), 6):     # Wi-Fi-style bursts
        drop.update(range(start, start + 9))
    lossy = b"".join(g for i, g in enumerate(grams) if i not in drop)

    def run(gated, hw=None, opts=None):
        log = client._LibavLogFilter()
        lg = logging.getLogger("libav")
        old_handlers, old_prop = lg.handlers[:], lg.propagate
        lg.handlers, lg.propagate = [log], False
        try:
            import av.logging
            av.logging.set_level(av.logging.WARNING)
            kw = {}
            if hw:
                kw["hwaccel"] = client._make_hwaccel(hw)
            box = av.open(io.BytesIO(lossy), format="mpegts",
                          options=opts or _client_opts(), **kw)
            vs = next(st for st in box.streams if st.type == "video")
            c = client.StreamCounters()
            g = client._KeyframeGate(c, codec=vs.codec_context.name) if gated else None
            n, errors = 0, 0
            try:
                for _f in client._counted_decode(box, c, gate=g, video=0):
                    n += 1
            except Exception:
                errors += 1
            return n, c, log.decoder_msgs, errors
        finally:
            lg.handlers, lg.propagate = old_handlers, old_prop

    old_opts = dict(_client_opts(), fflags="nobuffer")      # before: parser on, no gate
    n0, c0, msgs0, _ = run(gated=False, opts=old_opts)
    n1, c1, msgs1, err1 = run(gated=True)
    assert msgs0 > 0, "the scenario must actually hurt the old decode path"
    assert c1.keyframes >= 15, "keyframes must be found without FFmpeg's parser"
    assert c1.damaged > 0 and c1.skipped >= c1.damaged and c1.freezes > 0
    assert msgs1 <= msgs0 // 20, (msgs0, msgs1)      # a merged keyframe PES can still slip through
    assert err1 == 0
    ok(f"lossy stream: decoder chatter {msgs0} → {msgs1}; {c1.damaged} damaged / "
       f"{c1.skipped} held / {c1.freezes} freezes counted, {c1.keyframes} keyframes found")

    from av.codec.hwaccel import hwdevices_available
    if "videotoolbox" in (hwdevices_available() or ()):
        n2, c2, msgs2, err2 = run(gated=True, hw="videotoolbox")
        assert err2 == 0, "VideoToolbox must never see a damaged frame"
        assert n2 >= n1 - 2, (n1, n2)
        ok(f"VideoToolbox decodes the same lossy stream with no error ({n2} frames)")
    else:
        skip("VideoToolbox under loss", "no videotoolbox in this PyAV build")

    # Keyframes bigger than the demuxer's 200 KB PES cap arrive in pieces once
    # FFmpeg's parser is off; the decode loop must put them back together.
    # (A live desktop at 2194x1234 has them; the first run of this change hit
    # "No start code is found" on every one and reconnected in a loop.)
    big = os.path.join(tmp, "big.ts")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=1920x1080:rate=30,noise=alls=40:allf=t", "-t", "0.5",
         "-c:v", "libx265", "-preset", "ultrafast",
         "-x265-params", "qp=8:keyint=15:bframes=0:slices=4:log-level=error",
         "-pix_fmt", "yuv420p", *mux, "-f", "mpegts", big], check=True)
    for hw in [None] + (["videotoolbox"] if "videotoolbox" in (hwdevices_available() or ())
                        else []):
        kw = {"hwaccel": client._make_hwaccel(hw)} if hw else {}
        box = av.open(io.BytesIO(open(big, "rb").read()), format="mpegts",
                      options=_client_opts(), **kw)
        vs = next(st for st in box.streams if st.type == "video")
        c = client.StreamCounters()
        g = client._KeyframeGate(c, codec=vs.codec_context.name)
        n = sum(1 for _ in client._counted_decode(box, c, gate=g, video=0))
        assert n >= 14 and c.damaged == 0, (hw, n, c.damaged)
    ok(f"keyframes over the 200 KB PES cap are reassembled and decode ({n} frames)")

    # Latency: one frame at a time over real UDP, 300 ms apart. Before, every
    # frame reached the decoder only when the frame after next arrived.
    def frames_of(ts):
        out, cur, vpid = [], [], None
        for off in range(0, len(ts) - 187, 188):
            pk = ts[off:off + 188]
            pid = ((pk[1] & 0x1F) << 8) | pk[2]
            pusi = bool(pk[1] & 0x40)
            afc = (pk[3] >> 4) & 3
            pay = pk[4 + (1 + pk[4] if afc in (2, 3) else 0):]
            if vpid is None and pusi and pay[:3] == b"\x00\x00\x01" and 0xE0 <= pay[3] <= 0xEF:
                vpid = pid
            if pid == vpid and pusi and cur:
                out.append(b"".join(cur))
                cur = []
            cur.append(pk)
        return out + ([b"".join(cur)] if cur else [])

    chunks = frames_of(data)[:24]
    port = 5000 + random.Random().randrange(2000, 9000)
    got = []

    def _recv():
        box = av.open(f"udp://@127.0.0.1:{port}?fifo_size=131072&overrun_nonfatal=1"
                      f"&rw_timeout=2000000", format="mpegts", options=_client_opts())
        vs = next(st for st in box.streams if st.type == "video")
        try:
            for pk in box.demux(vs):
                if pk.size:
                    got.append(time.time())
        except Exception:
            pass

    rx = threading.Thread(target=_recv, daemon=True)
    rx.start()
    time.sleep(0.4)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sent = []
    for ch in chunks:
        sent.append(time.time())
        for off in range(0, len(ch), 1128):
            tx.sendto(ch[off:off + 1128], ("127.0.0.1", port))
        time.sleep(0.3)
    rx.join(4)
    # Each yield should follow its own send by milliseconds, not by a gap.
    lag = [min(t - s for s in sent if s <= t) for t in got]
    prompt = sum(1 for d in lag if d < 0.15)
    assert len(got) >= 10 and prompt >= len(got) - 2, \
        f"{prompt}/{len(got)} frames reached the decoder before the next frame was sent"
    ok(f"a frame reaches the decoder without waiting for the next ({prompt}/{len(got)} "
       f"within 150 ms at a 300 ms frame gap)")
    shutil.rmtree(tmp, ignore_errors=True)


print(f"\nALL {len(PASS)} CLIENT DECODE TESTS PASSED"
      + (f" ({len(SKIPPED)} skipped)" if SKIPPED else ""))
if SKIPPED:
    sys.exit(77)
