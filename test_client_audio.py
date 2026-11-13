#!/usr/bin/env python3
"""Client audio survival: silence while the clock runs, mute, reporting, banner, stalls.

The audio "didn't survive" sessions looked healthy to every check: the
player's clock kept running on a stream of digital silence. These tests pin
what replaced that blind spot:

  1. silence bookkeeping and the end-of-session audio report;
  2. mute is a volume change, never counted as a glitch, and a player switch
     neither; 2b. the built-in player's buffer rules;
  3. the status banner says what the picture and sound cannot;
  4. live: a real Opus stream of silence, then tone, through ffplay — the
     silence is reported while the clock runs, and its end too. The player
     runs muted, so nothing is audible while the suite runs.
  5. live: tone, then datagrams that never decode — ffplay's clock keeps
     ticking on wall time, so only the decoded-frame heartbeat sees the
     stall, and the player is replaced.
  6./7. live: the same through the built-in player, plus a burst cut back to
     the buffer target and a switch to ffplay mid-stream (muted: it plays zeros).

Sections 4-5 need ffmpeg and ffplay with libopus, 6-7 ffmpeg and
PyQt5.QtMultimedia; they are skipped otherwise.
"""
import socket
import logging
import os
import shutil
import subprocess
import sys
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


records = []


class _Capture(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())


root = logging.getLogger()
root.addHandler(_Capture())
root.setLevel(logging.INFO)

# ── 1. silence bookkeeping and the report ─────────────────────────────
st = client.AUDIO_STATE
st.update(started=1000.0, silent_since=None, silence_total=0.0, silence_longest=0.0)
assert client._audio_silence_line("[silencedetect @ 0x1] silence_start: 3.2", now=1100.0)
assert st["silent_since"] == 1100.0 - client.AUDIO_SILENCE_SECS
assert any("stream silent for" in m for m in records)
# silence_duration is in stream time, which a host encoder restart sends back
# to 0: the wall clock (60 s here) is what counts.
assert client._audio_silence_line(
    "[silencedetect @ 0x1] silence_end: 9.0 | silence_duration: 17970.0", now=1150.0)
assert st["silence_longest"] == 60.0 and st["silent_since"] is None
assert any(m == "Audio: sound again after 1m00s of silence." for m in records)
assert not client._audio_silence_line("  12.34 M-A:  0.000 fd=   0 aq=   0KB")
client._audio_silence_line("[silencedetect @ 0x1] silence_start: 90", now=1200.0)
client._audio_end_silence(now=1300.0)                 # player died mid-silence
assert st["silent_since"] is None and st["silence_longest"] == 110.0
report = client.audio_report(now=1400.0)
assert report.startswith("Audio report: 6m40s,") and "silent 42% (longest 1m50s)" in report, report
ok("silence while the clock runs is logged, timed, and summarised in the report")

# ── 2. mute is a volume change, not a glitch ──────────────────────────
class _Proc:
    terminated = False

    def terminate(self):
        _Proc.terminated = True


client.audio_proc = _Proc()
client.set_audio_muted(True)
assert st["muted"] and _Proc.terminated and client._audio_planned_restart.is_set()
client._audio_planned_restart.clear()
_Proc.terminated = False
client.set_audio_muted(True)                          # no change: no restart
assert not _Proc.terminated
client.set_audio_muted(False)
assert not st["muted"] and st["muted_since"] is None and st["muted_total"] >= 0.0
client._audio_planned_restart.clear()


class _Builtin(client._BuiltinAudio):
    def __init__(self):                               # no socket, no threads
        self.stopped = False

    def terminate(self):
        self.stopped = True


client.audio_proc = _Builtin()
client.set_audio_muted(True)                          # the built-in player reads the flag
assert not client.audio_proc.stopped and not client._audio_planned_restart.is_set()
client.set_audio_muted(False)
client.set_audio_player("ffplay")                     # switching is a planned restart
assert client.audio_proc.stopped and client._audio_planned_restart.is_set()
assert st["player"] == "ffplay"
client._audio_planned_restart.clear()
client.audio_proc = None
st["player"] = "builtin"
ok("mute restarts ffplay at the new volume (not the built-in player); a player switch "
   "is a planned restart")

# ── 2b. the built-in player's buffer rules ────────────────────────────
q = client._PcmQueue(target=8, limit=20)
assert q.pull(4) == bytes(4) and not q.playing, "silence until the target is buffered"
q.push(b"\x01" * 8)
assert q.pull(4) == b"\x01" * 4 and q.playing
assert q.pull(4) == b"\x01" * 4
assert q.pull(4) == bytes(4) and q.underruns == 1 and not q.playing, "ran dry: refill"
q.push(b"\x02" * 4)
assert q.pull(4) == bytes(4), "still refilling: 4 of 8 bytes"
q.push(b"\x03" * 24)                                  # a burst past the limit …
assert q.depth() == 8 and q.dropped == 20, (q.depth(), q.dropped)
assert q.pull(8) == b"\x03" * 8, "… keeps the newest audio, at the target"
q = client._PcmQueue(target=8, limit=100)          # a delay under the limit …
q.push(b"\x04" * 40)
q.pull(4, now=0.0)
q.push(b"\x04" * 4)
q.pull(4, now=0.5)
assert q.depth() == 36 and q.dropped == 0
q.pull(4, now=1.0)                                    # … that stays above 2x target for 1 s
assert q.depth() == 8 and q.dropped == 24, (q.depth(), q.dropped)
ok("built-in buffer: silence while filling, refill after running dry, bursts and lasting "
   "excess cut to target")

# ── 3. the status banner ──────────────────────────────────────────────
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

app = QApplication.instance() or QApplication([])


class _Gate:
    waiting, reason, since = True, "loss", 0.0


class _Dec:
    _has_first_frame = True
    hw_fallback_at = 0.0
    gate = _Gate()


class _Win:
    _audio_enabled = True
    _session_start = 0.0
    decoder_thread = _Dec()
    _banner_notices = client.MainWindow._banner_notices


win = _Win()
client.CLIENT_STATE.update(connected=True, reconnecting=False)
now = 100.0
_Gate.since = now - 2.0
_Dec.hw_fallback_at = now - 5.0
st["last_glitch"] = now - 1.0
notes = win._banner_notices(now)
assert notes == ["Picture frozen — waiting for a keyframe",
                 "Video decoding fell back to the CPU",
                 "Audio dropped out — restarting"], notes
client.CLIENT_STATE.update(connected=False, reconnecting=True,
                           host_note=("portal-wait", now - 1.0))
st["muted"] = True
_Gate.waiting = False
_Dec.hw_fallback_at = now - 60.0
notes = win._banner_notices(now)
assert notes == ["Reconnecting to the host…",
                 "The host is waiting for screen-share approval", "Muted"], notes
st["muted"] = False
client.CLIENT_STATE.update(connected=True, reconnecting=False, host_note=None)
assert win._banner_notices(now + 10) == []

parent = QWidget()
parent.resize(800, 500)
banner = client.StatusBanner(parent)
banner.show_notices(["Muted"])
assert banner.isVisibleTo(parent) and banner.text() == "Muted"
assert banner.x() > 8, "centred over the video"
banner.show_notices([])
assert not banner.isVisibleTo(parent)
ok("status banner: reconnecting, host approval, frozen picture, CPU decode, audio")

# ── 4. live: silence through a real player ────────────────────────────
have = all(shutil.which(b) for b in ("ffmpeg", "ffplay"))
if not have:
    skip("live silence detection", "ffmpeg/ffplay not installed")
else:
    st["player"] = "ffplay"                           # sections 4-5 are ffplay's
    records.clear()
    st.update(restarts=0, stalls=0, silent_since=None, silence_total=0.0,
              silence_longest=0.0, muted=False, muted_since=None, muted_total=0.0)
    pump = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=14",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1",
         "-c:a", "libopus", "-ar", "48000", "-ac", "2",
         "-f", "mpegts", f"udp://127.0.0.1:{client.UDP_AUDIO_PORT}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    listener = None
    try:
        listener = client.audio_listener("127.0.0.1", enabled=True, muted=True)
        deadline = time.time() + 40           # ~14 s when healthy; slack for a loaded box
        toggled = False
        while time.time() < deadline:
            if not toggled and any("playout clock running" in m for m in records):
                time.sleep(1.0)
                client.set_audio_muted(False)            # inaudible: still digital silence
                time.sleep(0.5)
                client.set_audio_muted(True)
                toggled = True
            if any("sound again after" in m for m in records):
                break
            time.sleep(0.2)
        assert any("stream silent for" in m for m in records), records[-8:]
        assert any("sound again after" in m for m in records), records[-8:]
        assert st["restarts"] == 0, "mute toggles and silence must not count as glitches"
        assert "-volume" in client.audio_proc.args and \
            client.audio_proc.args[client.audio_proc.args.index("-volume") + 1] == "0"
    finally:
        pump.terminate()
        try:
            pump.wait(timeout=3)
        except Exception:
            pump.kill()
        client.audio_stop.set()
        if client.audio_proc:
            client.audio_proc.terminate()
        if listener:
            listener.join(timeout=5)
    ok("live: digital silence is reported while the clock runs, then its end")

# ── 5. live: data keeps coming, nothing decodes ───────────────────────
if not have:
    skip("live stall detection", "ffmpeg/ffplay not installed")
else:
    import socket
    records.clear()
    st.update(restarts=0, stalls=0)
    client.audio_stop.clear()
    client._audio_listener_started = False
    stall_secs, client.AUDIO_STALL_SECS = client.AUDIO_STALL_SECS, 3.0
    listener = None
    junk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        listener = client.audio_listener("127.0.0.1", enabled=True, muted=True)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             "-c:a", "libopus", "-ar", "48000", "-ac", "2",
             "-f", "mpegts", f"udp://127.0.0.1:{client.UDP_AUDIO_PORT}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        assert any("playout clock running" in m for m in records), records[-8:]
        deadline = time.time() + 15           # ~3 s when healthy
        while time.time() < deadline and not st["stalls"]:
            # Data keeps arriving, so only the decoded-frame heartbeat can notice.
            junk.sendto(os.urandom(1128), ("127.0.0.1", client.UDP_AUDIO_PORT))
            time.sleep(0.02)
        assert st["stalls"] >= 1, records[-8:]
        assert any("nothing decoded" in m for m in records), records[-8:]
    finally:
        junk.close()
        client.AUDIO_STALL_SECS = stall_secs
        client.audio_stop.set()
        if client.audio_proc:
            client.audio_proc.terminate()
        if listener:
            listener.join(timeout=5)
    ok("live: undecodable data while the clock ticks is caught and the player replaced")


# ── 6./7. live: the built-in player ───────────────────────────────────
def _opus_pump(lavfi, realtime=True):
    """ffmpeg sending lavfi audio the way the host does (Opus, 20 ms, MPEG-TS)."""
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", *(["-re"] if realtime else []),
         "-f", "lavfi", "-i", lavfi, "-c:a", "libopus", "-b:a", "128k",
         "-frame_duration", "20", "-ar", "48000", "-ac", "2",
         "-f", "mpegts", f"udp://127.0.0.1:{client.UDP_AUDIO_PORT}?pkt_size=1316"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_for(text, secs):
    deadline = time.time() + secs
    while time.time() < deadline:
        if any(text in m for m in records):
            return True
        time.sleep(0.1)
    return False


def _fresh_listener():
    records.clear()
    client.audio_stop.clear()
    client._audio_listener_started = False
    st.update(player="builtin", restarts=0, stalls=0, silent_since=None, silence_total=0.0,
              silence_longest=0.0, muted=False, muted_since=None, muted_total=0.0)
    return client.audio_listener("127.0.0.1", enabled=True, muted=True)


def _stop_listener(listener):
    client.audio_stop.set()
    if client.audio_proc:
        client.audio_proc.terminate()
    if listener:
        listener.join(timeout=5)


try:
    from PyQt5 import QtMultimedia  # noqa: F401
    have_builtin = shutil.which("ffmpeg") is not None
except ImportError:
    have_builtin = False

if not have_builtin:
    skip("live built-in player", "ffmpeg or PyQt5.QtMultimedia missing")
else:
    listener = None
    pump = _opus_pump("aevalsrc='if(lt(t,12),0,0.1*sin(2*PI*440*t))':s=48000:d=14")
    try:
        listener = _fresh_listener()                  # muted: plays zeros
        assert _wait_for("sound again after", 30), records[-8:]
        player = client.audio_proc
        assert isinstance(player, client._BuiltinAudio), player
        assert any("stream detected — built-in player" in m for m in records), records[-8:]
        assert any("stream silent for" in m for m in records), records[-8:]
        pump.wait(timeout=10)
        # A stall's backlog arriving at once (3 s of audio, unpaced) is cut to the target.
        _opus_pump("sine=frequency=440:duration=3", realtime=False).wait(timeout=10)
        time.sleep(0.5)
        assert player.queue.dropped >= 2 * 1000 * client._PCM_PER_MS, player.queue.dropped
        assert player.queue.depth() <= client.AUDIO_LIMIT_MS * client._PCM_PER_MS
        # Switching to ffplay while audio flows is a planned restart.
        pump = _opus_pump("sine=frequency=440:duration=6")
        time.sleep(0.5)
        client.set_audio_player("ffplay")
        assert _wait_for("playout clock running", 10), records[-8:]
        assert st["restarts"] == 0, "silence, a burst and a switch are not glitches"
    finally:
        pump.terminate()
        _stop_listener(listener)
    ok("live built-in player: silence then sound, a burst cut to the target, switch to ffplay")

    listener = None
    junk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stall_secs, client.AUDIO_STALL_SECS = client.AUDIO_STALL_SECS, 3.0
    try:
        listener = _fresh_listener()
        _opus_pump("sine=frequency=440:duration=2").wait(timeout=10)
        assert any("stream detected — built-in player" in m for m in records), records[-8:]
        deadline = time.time() + 15                   # ~3 s when healthy
        while time.time() < deadline and not st["stalls"]:
            junk.sendto(os.urandom(1128), ("127.0.0.1", client.UDP_AUDIO_PORT))
            time.sleep(0.02)
        assert st["stalls"] >= 1 and any("nothing decoded" in m for m in records), records[-8:]
    finally:
        junk.close()
        client.AUDIO_STALL_SECS = stall_secs
        _stop_listener(listener)
    ok("live built-in player: undecodable data is caught and the player replaced")


print(f"\nALL {len(PASS)} CLIENT AUDIO TESTS PASSED"
      + (f" ({len(SKIPPED)} skipped)" if SKIPPED else ""))
if SKIPPED:
    sys.exit(77)
