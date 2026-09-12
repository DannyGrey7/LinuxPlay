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
assert '"-fflags", "nobuffer"' not in audio_src, "audio must not be starved of buffering"
assert "aresample=async=1:first_pts=0" in audio_src, "drift/gap recovery missing"
assert "bufsize=0" in audio_src and "universal_newlines" not in audio_src, \
    "the reader parses bytes; the pipe must not be in text mode"
assert "AUDIO_STALL_SECS" in audio_src and "_parse_ffplay_clock" in audio_src
# the listener must never stop the shared ffplay itself
assert "audio_stop.set()" not in audio_src, "closeEvent must own that, not the listener"
close_src = _func_source("closeEvent")
assert "if remaining == 0:" in close_src and "audio_stop.set()" in close_src
ok("audio buffering/async resampling on; only the last window stops the player")
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
