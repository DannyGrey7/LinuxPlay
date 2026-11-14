#!/usr/bin/env python3
"""Live portal screencast test: negotiate, capture 1 frame per monitor, print stats.

The frame is never written to disk - only numeric statistics are reported.
A blank frame means the capture could not be verified (the portal is
damage-driven, so an idle desktop can legitimately produce none): the test
reports SKIP (exit 77) instead of claiming success, which is how a black-screen
regression used to pass.

The live part is opt-in (LINUXPLAY_PORTAL_TEST=1) and uses a throwaway
restore-token file. It shows the share dialog on the host's screen, and
restore tokens are single-use: running it against the host's own token
file spent that token, and the next host start asked "which screen" again.
"""
import os
import subprocess
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portal_capture as pc  # noqa: E402

PASS, SKIPPED = [], []


def ok(name):
    PASS.append(name)
    print(f"  PASS: {name}")


def skip(name, why):
    SKIPPED.append(name)
    print(f"  SKIP: {name} — {why}")

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
ok("feeder command rescales the node's buffers to the logical size")

# GPU feeder: vapostproc scales and converts on the GPU (DMA-BUF in, NV12 out),
# keeps the rate cap, and writes BT.601 without a primaries conversion — what
# swscale made of the BGRx before (measured on SMPTE bars: identical values).
_gpu = pc.build_feeder_cmd(_stream, fps=60, size=(2194, 1234), gpu=True)
assert "vapostproc" in _gpu and "videoscale" not in _gpu and "videoconvert" not in _gpu, _gpu
assert ("video/x-raw,format=NV12,width=2194,height=1234,colorimetry=2:4:7:1,framerate=60/1"
        in _gpu), _gpu
assert _gpu.index("videorate") < _gpu.index("vapostproc"), _gpu
# GStreamer's NV12 rows are 4-byte aligned and its row count even; the encoder
# reads this padded size (test_host pins it), else every frame starts mid-row.
assert pc.nv12_frame_dims(2194, 1234) == (2196, 1234)
assert pc.nv12_frame_dims(1707, 1067) == (1708, 1068)
assert pc.nv12_frame_dims(1280, 720) == (1280, 720)
ok("GPU feeder: vapostproc to NV12 at the stream size, rate cap kept, padded frame known")

# ── negotiation logging, without a portal: what the log says about prompts ──
import logging  # noqa: E402

_records = []


class _Cap(logging.Handler):
    def emit(self, record):
        _records.append(record.getMessage())


logging.getLogger().addHandler(_Cap())
logging.getLogger().setLevel(logging.INFO)


class _FakeConn:
    unique_name = ":1.42"


class _FakePortal(pc.PortalCapture):
    """Answers like KDE: SelectSources at once, Start after `start_secs`."""

    def __init__(self, start_secs, start_token="tok-new"):
        super().__init__()
        self.conn = _FakeConn()
        self.start_secs, self.start_token = start_secs, start_token
        self.seen_waiting = None

    def _request(self, method, signature, body, timeout, path, dialog_timeout):
        self.last_request_secs = 0.05
        if method == "CreateSession":
            return 0, {"session_handle": "/org/freedesktop/portal/desktop/session/1_1/x"}
        if method == "SelectSources":
            return 0, {}
        self.seen_waiting = self.waiting_since
        self.last_request_secs = self.start_secs
        res = {"streams": [(100, {"size": (2194, 1234), "position": (0, 0)})]}
        if self.start_token:
            res["restore_token"] = self.start_token
        return 0, res


pc.RESTORE_PATH = os.path.join(tempfile.mkdtemp(prefix="lp_portal_"), "portal_restore.json")
fp = _FakePortal(start_secs=19.8)
assert fp._negotiate("tok-old", True, 120) is True
assert fp.seen_waiting is not None and fp.waiting_since is None, "waiting flag spans the prompt"
assert any("share dialog was shown and answered after 20s — the saved restore token was "
           "not honoured" in m for m in _records), _records[-4:]
assert not any("did not issue" in m for m in _records), "KDE issues the token with Start"
assert pc._load_restore_token() == "tok-new"
_records.clear()
assert _FakePortal(start_secs=0.1)._negotiate("tok-new", True, 120) is True
assert any("restore token accepted — no dialog" in m for m in _records), _records[-4:]
_records.clear()
assert _FakePortal(start_secs=8.0, start_token=None)._negotiate(None, True, 120) is True
assert any("did not issue a restore token" in m for m in _records), _records[-4:]
ok("portal log tells a restored session from a prompt, and the token from no token")

if os.environ.get("LINUXPLAY_PORTAL_TEST") != "1":
    skip("live portal negotiation", "shows the share dialog on this screen; "
         "set LINUXPLAY_PORTAL_TEST=1 to run it")
    print(f"\nPORTAL CAPTURE TEST PASSED ({len(PASS)} checks, {len(SKIPPED)} skipped)")
    sys.exit(77)

# Never the host's own token: a run that times out would spend it.
pc.RESTORE_PATH = os.path.join(tempfile.mkdtemp(prefix="lp_portal_"), "portal_restore.json")
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
                if arr.std() > 5:
                    verdict = "CONTENT OK (non-blank)"
                    ok(f"monitor {i} {want[0]}x{want[1]}: real content captured")
                else:
                    verdict = "blank/uniform"
                    # The portal only delivers frames when the compositor has
                    # damage to report, so an idle desktop looks like this.
                    skip(f"monitor {i} {want[0]}x{want[1]} frame content",
                         "blank frame — produce some screen damage (notify-send) and rerun")
                print(f"  -> {verdict}")
            finally:
                feeder.terminate()
                err = feeder.stderr.read().decode(errors="replace").strip()
                if err:
                    print(f"  feeder stderr: {err.splitlines()[0]}")
finally:
    p.close()

print(f"\nPORTAL CAPTURE TEST PASSED ({len(PASS)} checks"
      + (f", {len(SKIPPED)} skipped)" if SKIPPED else ")"))
if SKIPPED:
    sys.exit(77)
