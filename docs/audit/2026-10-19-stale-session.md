# Incident: "still connected", then a PIN prompt (2026-10-19)

**Reported:** the client was closed the night before, but at 17:03 the
next day it asked for a PIN. The host window still showed a connected
client.

**Short version:** the host was not holding a stale session at 17:03, but it
was too starved to answer. An audio-encoder warning flood fed the host GUI's
log view, and the view's layout work blocked every other host thread. The
certificate challenge arrived after the client's 8 s timeout, and the
client fell back to a PIN. Overnight, the same starvation had made the
client give up on heartbeats, and the host answered `BUSY` to each of its
reconnects for five hours. At 17:43 the host aborted inside Qt's text
layout.

## Timeline (host doggoos, Mac client 100.104.182.44)

| time | evidence | what happened |
|------|----------|---------------|
| 10-18 ≤ 18:00 | macOS log: `Python[9369]` with a VideoToolbox decoder | Client started. It decoded video all night (decoder sessions renewed through 06:55). |
| 21:46 | `pmset -g log`: display off | Mac display asleep. The Mac stayed awake (audio assertions held it). |
| 00:06 → 01:05 | host journal | libopus `Queue input is backward in time` appears now and then. At 01:05 the VR sink disappeared, audio capture moved to the HDMI monitor, and from then on it ran at ~17 lines/s. |
| 01:15 → 02:08 | `host.log.1..3`: 17,820 lines each, ~18 min apart | The flood rotated everything before 01:15 out of host.log. The journal's own retention began at 23:57. |
| 02:17 → 07:18 | host log: 128 × `Rejected CERTFP client … active session already running` | The same client, still running, re-authenticated every few seconds to minutes, and the host answered `BUSY` each time because that client's own session was still open. *Inferred, since the client log is gone:* the starved host's PINGs came more than 6 s apart, which is what makes the client declare "Lost heartbeat" and re-authenticate, while its PONGs kept the host's side alive. |
| 07:08:11 | `pmset`: display on | |
| 07:08:37 | macOS log for pid 9369: `applicationShouldTerminate` → exit handler | **The client was quit here, not the night before.** No GOODBYE was processed. |
| 07:09:11 | host: `Heartbeat timeout … no PONG or GOODBYE` | Session ended. Teardown log lines then lagged by minutes (`Client disconnected` stamped 07:17:57). |
| 17:03:12 | client: `Unexpected response to certificate auth () — falling back to PIN` | The client waited 8 s for the CHALLENGE and the host answered ~30 s later (`Certificate proof … (0 fields)` at 17:03:43). The client then asked for a PIN. |
| 17:43:07 | systemd-coredump, SIGABRT | Main thread in `QTextEdit::moveCursor` → `QTextDocumentLayout` → `QUnicodeTools::initCharAttributes`. Over 66 h of uptime the process had used 42 h of CPU, peaked at 1.2 GB plus 868 MB of swap, and was down until restarted at 18:12. |

The journal shows the lag directly: a log record stamped 17:04:13 reached
stdout at 17:04:44.

## Root causes

1. **Unbounded GUI log view (host).** Every log record became a queued
   signal, and each signal did `QTextEdit.append()` plus `moveCursor(End)`
   on a document that only grew. PyQt5 keeps the GIL during those calls. With
   hundreds of thousands of lines, each append re-laid out the document, and
   the heartbeat, handshake and control threads waited behind it. This one
   cause explains the late PINGs, the late CHALLENGE, the minute-long log
   lag, the CPU time, the memory and the crash.
2. **Encoder stderr logged line by line (host).** Every non-progress
   ffmpeg line was a WARNING, so a warning that repeats per frame became
   ~1 M log lines in a night. It also pushed the evening's records out of
   host.log.
3. **No way back into one's own session (host).** A certificate client was
   refused while *any* session was live, including its own stale one, so
   recovery waited on the heartbeat timeout. With a starved host, that
   timeout never fired while the client kept answering.
4. **Misleading fallback (client).** Both a `BUSY` answer and silence
   ended in a PIN prompt, although a PIN can fix neither.
5. **Evidence lost on relaunch (client, launcher).** `linuxplay_client.log`
   and `client-launch.log` were opened with mode `"w"`, so the 17:03 launch
   erased the overnight run's log.

## Fixes (branch `stabilize`)

| commit | change |
|--------|--------|
| `c3a2300` | GUI log is a `QPlainTextEdit` capped at 2000 lines, fed from a bounded queue in 250 ms batches; an overflow says how many lines it skipped. Encoder stderr goes through a repeat gate: each distinct message (addresses and numbers ignored) is logged once, then at most once a minute with a count. |
| `0e9cc67` | A certificate proof from the device that owns the live session (same address, and the same certificate or a PIN session from that address) replaces that session. Other devices still get `BUSY`. The client no longer offers a PIN for `BUSY` or for silence: silence gets one 30 s retry, then "Host Not Responding". |
| `84f62ec` | Client and launch logs keep the previous three runs (`.1`–`.3`). |
| `76432b8` | Fixes the race in the upload test (`test_hardening`) that failed about 1 run in 12. |

**Verified on doggoos (18:12–18:15):** the auth/host/hardening/parsers
suites pass. The host was restarted through its autostart unit and the
portal restored with no dialog. A 1-minute probe session ran: certificate
auth, `GOODBYE`, clean end. A probe was then killed with `-9` mid-stream and
a new one started a second later. The host logged `proved its certificate
again — replacing its previous session` and streamed to the new client.

## Open

- **Why the audio timestamps go backwards.** `-use_wallclock_as_timestamps 1`
  on a PulseAudio monitor stamps each read with the wall clock, and read
  jitter makes consecutive frames overlap. Hence `Queue input is backward in
  time` (libopus) and `Non-monotonic DTS` (muxer, 1000 in the morning's
  log). The flood is now contained, but the timestamps are still wrong. It
  is a candidate for the earlier "audio silent while video continues"
  reports. A fix (`aresample=async=…` or sample-count timestamps) changes A/V
  timing, so it needs a listening test.
- **Nobody watching.** The client streamed for ~9 h with the Mac display
  asleep, and the host encoded the whole time. A client-side "pause when
  the display sleeps" would have made the night uneventful.
- A host that is too slow still delays its own heartbeats. Queued PONGs
  count as fresh, so a starved host never times out a client that is gone.
  With the flood bounded this should not recur, so nothing was changed.
