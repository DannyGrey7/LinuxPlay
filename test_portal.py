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
print("  PASS: feeder command rescales the node's buffers to the logical size")

p = pc.PortalCapture()
try:
    p.ensure(multiple=True)
    print(f"negotiated {len(p.streams)} stream(s):")
    for s in p.streams:
        print(f"  node={s['node']} {s['w']}x{s['h']} at ({s['x']},{s['y']})")

    for i, s in enumerate(p.streams):
        feeder = subprocess.Popen(
            pc.build_feeder_cmd(s),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-fflags", "nobuffer",
                "-f", "rawvideo", "-pixel_format", "bgr0",
                "-video_size", f"{s['w']}x{s['h']}", "-framerate", "5",
                "-i", "pipe:0",
                "-frames:v", "1", "-f", "rawvideo", "pipe:1",
            ]
            raw = subprocess.check_output(cmd, stdin=feeder.stdout, timeout=60)
            expected = s["w"] * s["h"] * 4
            assert len(raw) >= expected, (
                f"feeder delivered {len(raw)} bytes, expected {expected} — the gst "
                f"pipeline did not negotiate (is videoscale still in the chain?)"
            )
            arr = np.frombuffer(raw[:expected], np.uint8)
            print(f"monitor {i}: frame bytes={len(raw)} (expected {expected})")
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
