#!/usr/bin/env python3
"""Client audio-path and hardware-decode tests.

The audio test pushes a real Opus/MPEG-TS stream at the client's UDP port and
checks that ffplay is driven through the same code path the client uses, that
our status-line parser sees the playout clock move, and that the stall watchdog
does not fire while audio is flowing. SDL's dummy driver keeps it silent.
"""
import logging
import os
import socket
import subprocess
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import client   # noqa: E402

PASS = []
def ok(name):
    PASS.append(name)
    print(f"  PASS: {name}")


# ── 1. ffplay status-line parser (real line shapes) ──
assert client._parse_ffplay_clock(
    "   12.34 M-A:  0.012 fd=   1 aq=   12KB vq=    0KB sq=    0B f=0/0") == 12.34
assert client._parse_ffplay_clock(
    " 1:02:03.45 M-A: -0.001 fd=   1 aq=    2KB") == 3723.45
assert client._parse_ffplay_clock("   0.00 A-V:  0.000") == 0.0
assert client._parse_ffplay_clock("Input #0, mpegts, from 'udp://0.0.0.0:6001'") is None
assert client._parse_ffplay_clock("   bad M-A: 1") is None
assert client._parse_ffplay_clock("") is None
ok("ffplay playout clock parsed from real status lines")


import ast
_HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(_HERE, "client.py"), encoding="utf-8").read()
_tree = ast.parse(src)

def _func_source(name):
    fn = next(n for n in ast.walk(_tree)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(src, fn)
audio_src = _func_source("audio_listener")
# comments legitimately mention the forbidden flags when explaining their absence
audio_code = "\n".join(l for l in audio_src.splitlines() if not l.lstrip().startswith("#"))
assert '"-fflags", "nobuffer+discardcorrupt"' in audio_code, "low-latency audio flags missing"
assert "aresample=async" not in audio_code and "first_pts" not in audio_code, \
    "async/first_pts add seconds of playout delay (measured ~6.3 s)"
assert "bufsize=0" in audio_src and "universal_newlines" not in audio_src, \
    "the reader parses bytes; the pipe must not be in text mode"
assert "AUDIO_STALL_SECS" in audio_src and "_parse_ffplay_clock" in audio_src
# the listener must never stop the shared ffplay itself
assert "audio_stop.set()" not in audio_src, "closeEvent must own that, not the listener"
close_src = _func_source("closeEvent")
assert "if remaining == 0:" in close_src and "audio_stop.set()" in close_src
ok("low-latency audio flags on, no async resampler; only the last window stops the player")
# ── 2. YUV plane payloads: zero-copy views, dims, formats, colour maths ──
import av                                              # noqa: E402
import numpy as np                                     # noqa: E402

def _gradient_rgb(w, h):
    """Smooth ramps: chroma changes slowly, so nearest-neighbour chroma
    upsampling (our fallback) and swscale's agree to within a few levels."""
    xs = np.linspace(20, 235, w, dtype=np.float32)
    ys = np.linspace(15, 240, h, dtype=np.float32)
    img = np.empty((h, w, 3), dtype=np.float32)
    img[..., 0] = ys[:, None] * 0.4 + xs[None, :] * 0.6
    img[..., 1] = 90 + 60 * np.sin(ys[:, None] / 9.0)
    img[..., 2] = xs[None, :] * 0.3 + ys[:, None] * 0.7
    return np.ascontiguousarray(img, dtype=np.uint8)

srcframe = av.VideoFrame.from_ndarray(_gradient_rgb(96, 64), format="rgb24")
for yuvfmt in ("yuv420p", "yuv444p", "nv12"):
    yframe = srcframe.reformat(format=yuvfmt)
    payload = client._frame_planes(yframe)
    assert payload is not None and payload[0] == "yuv", yuvfmt
    _, planes, w, h, _cs, frame_ref = payload
    assert (w, h) == (96, 64)
    expected_planes = 3 if yuvfmt != "nv12" else 2
    assert len(planes) == expected_planes, (yuvfmt, len(planes))
    cw, ch = 48, 32
    if yuvfmt == "yuv444p":
        assert all(pw == 96 and ph == 64 for _v, pw, ph, _s in planes)
    else:
        assert (planes[0][1], planes[0][2]) == (96, 64)
        assert all((pw, ph) == (cw, ch) for _v, pw, ph, _s in planes[1:])
    # zero-copy: the views alias the frame buffer, not a copy of it
    root = planes[0][0]
    while getattr(root, "base", None) is not None:
        root = root.base
    assert isinstance(root, memoryview), "plane views must be zero-copy"
    # CPU fallback converter must land within a few levels of swscale
    ours = client._yuv_planes_to_rgb(payload)
    sws = yframe.to_ndarray(format="rgb24")
    diff = np.abs(ours.astype(np.int16) - sws.astype(np.int16))
    # nearest-neighbour chroma upsampling vs swscale's bilinear costs a few
    # levels (nv12 takes a different swscale path than yuv420p, hence the
    # higher bound); a wrong matrix misses by an order of magnitude
    limit = (10, 5.0) if yuvfmt == "nv12" else (8, 2.0)
    assert diff.max() <= limit[0] and diff.mean() <= limit[1], \
        f"{yuvfmt}: colour conversion diverged (max {diff.max()}, mean {diff.mean():.2f})"
ok("plane payloads are zero-copy with correct dims; CPU fallback matches swscale colour")

# non-whitelisted formats go through the plane path check too
assert client._frame_planes(srcframe) is None, "rgb24 frames are not plane payloads"
f10 = av.VideoFrame(64, 48, "yuv420p10le")
assert client._frame_planes(f10) is None, "10-bit must fall back to the rgb24 path"
ok("non-whitelisted formats (rgb24, 10-bit) fall back to the rgb24 path")

# ── 2b. the decoder emits plane payloads when it can (source wiring) ──
assert "_frame_planes(frame)" in src and "_yuv_planes_to_rgb(payload)" in src
assert 'frame_ready.emit(payload)' in src
ok("decoder emits plane payloads and paintGL degrades via _yuv_planes_to_rgb")

# ── 2c. live GL: the shader builds and renders every plane format ─────
# Offscreen Mesa gives a real context on this box; a machine without GL
# skips this rather than failing (paintGL's CPU fallback covers it there).
from PyQt5.QtWidgets import QApplication                      # noqa: E402
_app = QApplication.instance() or QApplication([])
_glw = client.VideoWidgetGL(lambda m: None, 96, 64, 0, 0, "127.0.0.1")
_glw.resize(96, 64)
_glw.show()
_app.processEvents()
if _glw._yuv_prog is None:
    print("  SKIP: no OpenGL context offscreen — shader render test not run")
else:
    def _gl_readback():
        img = _glw.grabFramebuffer().scaled(96, 64)
        buf = img.constBits().asarray(img.byteCount())
        out = np.frombuffer(buf, dtype=np.uint8).reshape(img.height(), img.width(), 4)[:, :, :3]
        return out[:, :, ::-1]      # ARGB32 little-endian → B,G,R
    # alternate formats on purpose: 420p and 444p share (w, h, nplanes) and
    # must still reallocate their chroma textures
    for yuvfmt in ("yuv420p", "yuv444p", "nv12", "yuv420p"):
        yframe = srcframe.reformat(format=yuvfmt)
        _glw.updateFrame(client._frame_planes(yframe))
        _glw.update()
        _app.processEvents()
        # second identical paint: the very first swap can leave a few stale
        # pixels on the window's top scanline (offscreen rasterizer quirk)
        _glw.update()
        _app.processEvents()
        sws = yframe.to_ndarray(format="rgb24")
        got = _gl_readback()[2:-2, 2:-2]
        want = sws[2:-2, 2:-2]
        diff = np.abs(got.astype(np.int16) - want.astype(np.int16))
        assert diff.max() <= 10 and diff.mean() <= 5.0, \
            f"{yuvfmt}: GPU render diverged (max {diff.max()}, mean {diff.mean():.2f})"
    # the legacy rgb24 payload still paints through the fixed-function path
    legacy = srcframe.reformat(format="rgb24").to_ndarray(format="rgb24")
    _glw.updateFrame((legacy, 96, 64))
    _glw.update()
    _app.processEvents()
    _glw.update()
    _app.processEvents()
    got = _gl_readback()[2:-2, 2:-2]
    want = legacy[2:-2, 2:-2]
    diff = np.abs(got.astype(np.int16) - want.astype(np.int16))
    assert diff.max() <= 2, f"rgb24 payload render diverged (max {diff.max()})"
    _glw.hide()
    ok("GL shader renders 420p/444p/nv12 (incl. format switches) matching swscale")

# ── 3. hwaccel factory: only offers what this PyAV/FFmpeg can actually do ──
from av.codec.hwaccel import hwdevices_available          # noqa: E402
offered = list(hwdevices_available() or ())
assert client._make_hwaccel("definitely-not-a-device") is None
if offered:
    accel = client._make_hwaccel(offered[0])
    assert accel is not None, f"{offered[0]} is offered but the factory refused it"
    assert accel.is_hw_owned is False, "frames must be downloaded to system memory"
    assert accel.allow_software_fallback is True, "an unsupported stream must still play"
else:
    assert client._make_hwaccel("videotoolbox") is None
not_offered = next((d for d in ("vaapi", "videotoolbox", "cuda") if d not in offered), None)
if not_offered:
    assert client._make_hwaccel(not_offered) is None, \
        f"{not_offered} is not in this build's device list and must be refused"
ok(f"hwaccel factory validates against the PyAV build ({offered or 'none offered'})")


# ── 4. a failing hw decode must fall back to CPU exactly once ──
run_src = src.split("class DecoderThread")[1].split("class VideoWidgetGL")[0]
assert "_sw_fallback_done" in run_src and "self._hwaccel = None" in run_src
assert "self.decoder_opts.pop(\"hwaccel\", None)" in run_src
ok("a hardware-decode failure disables hwaccel and retries in software")


# ── 5. live: real Opus over UDP drives the listener and the watchdog stays quiet ──
records = []
class _Capture(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())
cap = _Capture()
logging.getLogger().addHandler(cap)
logging.getLogger().setLevel(logging.INFO)

pump = subprocess.Popen(
    ["ffmpeg", "-hide_banner", "-loglevel", "error",
     "-re", "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
     "-c:a", "libopus", "-ar", "48000", "-ac", "2",
     "-f", "mpegts", f"udp://127.0.0.1:{client.UDP_AUDIO_PORT}"],
    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
try:
    client.audio_listener("127.0.0.1", enabled=True)
    deadline = time.time() + 12
    while time.time() < deadline:
        if any("playout clock running" in m for m in records):
            break
        time.sleep(0.2)
    msgs = [m for m in records]
    assert any("playout clock running" in m for m in msgs), \
        f"listener never saw audio flow; log was: {msgs[-6:]}"
    assert not any("Audio stalled" in m for m in msgs), "watchdog fired while audio flowed"
    assert not any("Audio listener failed" in m for m in msgs), "listener crashed"
finally:
    pump.terminate()
    try:
        pump.wait(timeout=3)
    except Exception:
        pump.kill()
    client.audio_stop.set()
    logging.getLogger().removeHandler(cap)
ok("live Opus stream: clock advances, no false stall restart, no crash")


print(f"\nALL {len(PASS)} CLIENT A/V TESTS PASSED")
