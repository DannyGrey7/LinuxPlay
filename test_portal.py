#!/usr/bin/env python3
"""Live portal screencast test: negotiate, capture 1 frame per monitor, print stats.

The frame is never written to disk - only numeric statistics are reported.
"""
import subprocess
import sys

import numpy as np

sys.path.insert(0, ".")
import portal_capture as pc  # noqa: E402

# ── the feeder must rescale: the portal reports the logical size (e.g.
# 1707x1067 at 150% scaling) while the node hands out the panel's own buffers
# (2560x1600 BGRA). Without videoscale those caps never negotiate and the
# client gets a black screen, so guard the shape of the command here.
_stream = {"node": 7, "w": 1707, "h": 1067}
_feeder = pc.build_feeder_cmd(_stream, fps=60)
assert "videoscale" in _feeder, _feeder
assert "video/x-raw,format=BGRx,width=1707,height=1067,framerate=60/1" in _feeder, _feeder
assert "videorate" in _feeder and "drop-only=true" in _feeder, _feeder
assert "videorate" not in pc.build_feeder_cmd(_stream), "no fps asked for, no rate cap to add"

# --resolution: the same scaling stage produces the smaller stream.
_scaled_feeder = pc.build_feeder_cmd(_stream, fps=60, size=(1280, 720))
assert "video/x-raw,format=BGRx,width=1280,height=720,framerate=60/1" in _scaled_feeder, _scaled_feeder
print("  PASS: feeder command rescales the node's buffers to the logical size")

p = pc.PortalCapture()
try:
    p.ensure(multiple=True)
    print(f"negotiated {len(p.streams)} stream(s):")
    for s in p.streams:
        print(f"  node={s['node']} {s['w']}x{s['h']} at ({s['x']},{s['y']})")

    for i, s in enumerate(p.streams):
        # Native, then the size --resolution asks for: the feeder must produce
        # exactly those pixels either way (that is what the encoder expects).
        for size in (None, (1280, 720)):
            want = size or (s["w"], s["h"])
            feeder = subprocess.Popen(
                pc.build_feeder_cmd(s, size=size),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                cmd = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-fflags", "nobuffer",
                    "-f", "rawvideo", "-pixel_format", "bgr0",
                    "-video_size", f"{want[0]}x{want[1]}", "-framerate", "5",
                    "-i", "pipe:0",
                    "-frames:v", "1", "-f", "rawvideo", "pipe:1",
                ]
                raw = subprocess.check_output(cmd, stdin=feeder.stdout, timeout=60)
                expected = want[0] * want[1] * 4
                assert len(raw) >= expected, (
                    f"feeder delivered {len(raw)} bytes, expected {expected} — the gst "
                    f"pipeline did not negotiate (is videoscale still in the chain?)"
                )
                arr = np.frombuffer(raw[:expected], np.uint8)
                print(f"monitor {i} {want[0]}x{want[1]}: frame bytes={len(raw)} "
                      f"(expected {expected})")
                print(f"  min={arr.min()} max={arr.max()} mean={arr.mean():.1f} std={arr.std():.1f}")
                verdict = "CONTENT OK (non-blank)" if arr.std() > 5 else "SUSPICIOUS: blank/uniform frame"
                print(f"  -> {verdict}")
            finally:
                feeder.terminate()
                err = feeder.stderr.read().decode(errors="replace").strip()
                if err:
                    print(f"  feeder stderr: {err.splitlines()[0]}")
    print("PORTAL CAPTURE TEST PASSED")
finally:
    p.close()
