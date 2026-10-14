# Measurements (October 2026)

How each change was measured, before and after, on the real two machines.
Tools: `tools/probe_client.py` runs the real client code without a window
(handshake, decode, pacing reports, audio, the WINDOW_CLOSE/GOODBYE
teardown) and records the kernel's UDP drop counters. `tools/analyze_client_log.py`
summarises old- and new-format logs the same way. Lab scripts are in
`~/linuxplay-audit/lab/`.

## 1. Client decode path: A/B on the live host

Same host process (`9f40df7`), same desktop content, 30 minutes each, audio
off, MacBook Air on Wi-Fi via Tailscale. **A** = client `6cf606c` (baseline),
**B** = client `1a6394e` (keyframe gate, no parser, reassembly, hardware
resets, honest counters).

| | A: before | B: after |
|---|---|---|
| decoded fps p5 / p50 / p95 | 30.2 / 31.8 / 33.8 | 30.4 / 31.9 / 33.5 |
| receive rate | 4.77 Mb/s | 4.68 Mb/s |
| loss events (frames that arrived damaged) | 29 | 12 *(Wi-Fi varies run to run)* |
| what the pacing line said about them | "0 corrupt" throughout | 12 damaged, 132 held, 0 refused, 0 corrupt |
| frames lost per event | ~4.8 refused + an unknown number shown smeared | 11 held, none shown damaged |
| picture frozen per event | not measured (invisible) | **306 ms** avg, longest 549 ms |
| hardware decode | **fell back to CPU** after 20 min, for good | stayed on VideoToolbox |
| decoder log lines | 1,439 | **2** |
| WARNING + ERROR lines | 1,449 | 3 |
| client socket-buffer drops (kernel) | 0 | 0 |

For scale, the 51.8 h session the audit started from: 125,597 decoder lines
(2,423/h), 2,188 damaged frames (42/h), 13,853 refused frames, hardware decode
lost 22 minutes in.

## 2. Latency: when a frame reaches the decoder

Lab replay over localhost UDP of the production-encoder clip, one frame every
300 ms, each yielded packet matched to its frame by PTS (`latency_lab.py`):

| host muxer | client demux | send → decoder (p50) | frames held |
|---|---|---|---|
| default (`9f40df7`) | parser on (`6cf606c`) | **610 ms** (2 frame gaps) | all |
| default | parser off | 306 ms (1 gap) | all |
| PES length written (`c946510`) | parser on | 311 ms (1 gap) | all |
| **PES length written** | **parser off** (`3decef5`) | **0.7 ms** | only frames > 64 KB (6 of 599) |

At 30 fps that's 67 ms less latency on every frame. On a quiet desktop, where
frames come only when something changes, it's the difference between a
keystroke appearing at once and waiting for two more screen updates.
`test_client_decode.py` pins it (22/22 frames within 150 ms at a 300 ms gap).

## 3. Where the loss is

Synthetic doggoos → Mac datagrams over Tailscale (`burst_test.py`, 62,400
datagrams of 1,128 B in ~80 s):

| burst | paced | lost |
|---|---|---|
| 10 / 50 / 100 / 200 / 400 datagrams | no | 0 |
| 200 datagrams | 200 / 100 / 50 Mb/s | 0 |
| 400 datagrams | 100 Mb/s | 44, one run of 44 consecutive |

Host `tailscale0` TX drops 0, `SndbufErrors` 0. Mac `utun10` drops 0;
"full socket buffer" drops during the test 0, and 0 during every probe run.
Loss is rare runs of tens of datagrams (Wi-Fi), not burst overflow.

## 4. Audio

- Host source selection on doggoos: `pactl get-default-sink` works (pactl 17).
  The new command (`level+info`, `silencedetect`) was run against the idle
  HDMI monitor and logged `silence_start` after 10 s and `silence_end …
  silence_duration: 14.0` at exit. Silent Opus measured 33 kb/s on the wire
  vs ~150 kb/s with sound.
- Client player start: **4.8 s → 0.2 s** to first sound (ffplay's default
  stream analysis vs `-probesize 32 -analyzeduration 0`, same stream).
- Live client silence detection: `test_client_audio.py` drives 14 s of
  digital silence then tone through the real player. "stream silent for 10s+
  while the playout clock runs" and "sound again after …" are logged, and a
  mute toggle counts no glitch.

## 5. Portal prompt (first restart on the new host)

19:42 restart of the host on `stabilize`. The client connected and the host logged
`Portal: reusing the saved restore token — no share dialog expected.`; KDE
ignored the token (spent by the audit's earlier test run, see F6) and showed
the dialog. Within a second the client logged `Host says: waiting for someone
to approve screen sharing on the host` (and the banner shows it). After the
120 s dialog timeout the host logged `Portal: nobody answered the
screen-share dialog on the host in time — it will be shown again on the next
try.` Before this change, all of that was one debug line and a black screen.

## 6. New host + new client (runs C and D)

**Run C**: host `308f56d`, client `308f56d`, 40 min, audio on (muted), busier
desktop than A/B (7.2 Mb/s, 33 fps p50).

| | |
|---|---|
| loss events | 60 (90/h; more traffic, and Wi-Fi varies) |
| per event | 19.6 frames held, **580 ms frozen** (longest 935 ms) |
| refused (losses the transport could not flag) | 52, each followed by a hold to the next keyframe |
| hardware decode | **4 VideoToolbox rejections, all reset on hardware**; no CPU fallback |
| decoder log lines | 4 (plus 9 per-minute summaries) |
| host vs client rates (live) | `host 30 fps / 7.3 Mb/s, recv 7.2 Mb/s` beside the client's 29.9 fps |
| teardown | `Stopped video stream … → Session … ended (goodbye) → Audio report → All streams stopped`. No encoder spawned, no ERROR |

**Forced audio interruptions during run C** (the acceptance sketch):

1. `kill -9` of the host's audio encoder at 19:49:26. Host: `Audio exited (-9)`
   → `restarting in 1s (attempt 1/5)` → command rebuilt (`Audio source:
   wivrn.sink.monitor (default sink)`). Client: the player rode through
   without a stall. Both ends logged the silence and its end in the same
   second.
2. Default sink switched `wivrn.sink` → HDMI at ~19:50:37 and back at
   ~19:51:07 (with Danny's OK). Host: `Audio: default sink changed — moving
   capture from wivrn.sink.monitor to alsa_output…hdmi-stereo-extra3.monitor`
   within 2 s, and back again 30 s later. This is the "silent, video fine"
   failure, now caught and corrected automatically. Nothing was playing on
   either sink, so the client correctly heard continuous silence. The run
   exposed one misleading line ("sound … again" when a capture was merely
   stopped), fixed in `e5f7f75`.
3. Session reports: host `40m00s from wivrn.sink.monitor → alsa_output…
   (2 source switches), 28.9 MB sent, 1 encoder restarts, silent 90%`; client
   `40m00s, 0 player restarts (0 stalls), silent 90% (longest 35m27s), muted 40m00s`.

**Recording analysis.** Ten minutes of the live stream recorded datagram
by datagram (`record_stream.py`, 503,171 datagrams, 556 MB) and replayed
offline through the client's decode path (`replay_analyze.py`):

- 8 continuity gaps in 10 min, each 2–12 TS packets. On a quiet desktop a
  whole P-frame is one short datagram, so these losses remove whole frames.
  With PES lengths written, the demuxer can't flag those (the frame before
  completed by its length; the one after starts clean). The decoder refuses
  the next frame and the gate holds until the keyframe: same freeze, one
  frame later. That's where run C's refusals came from.
- The VideoToolbox rejection in the recording hit a **clean** 321 KB
  keyframe: no gap within 4,600 datagrams. The keyframe reassembled from
  the demuxer's two pieces is **byte-identical** (MD5) to what FFmpeg's own
  parser produces, VideoToolbox fails on it either way, and FFmpeg's CPU
  decoder decodes the whole stretch without a message. VPS/SPS/PPS are
  identical across keyframes. So VideoToolbox intermittently rejects valid
  `hevc_vaapi` keyframes. That's the likelier cause of the original 14:13
  fallback too (no loss in that window). Resetting on hardware is the
  mitigation; plan item 18 removes the freeze.

**Run D**: final build `f18e360`, 5 min, after a host restart.

- `Portal: restore token accepted — no dialog (answered in 0.0s)`: the token
  approved at 19:48:50 restores unattended.
- `Network mode switch requested: lan → vpn` logged without blocking; the
  portal answered 350 ms later and the streams were built for vpn directly.
- 33.5 fps p50, 2 loss events (440 ms frozen each), 1 VideoToolbox reset on
  hardware, clean teardown, `Audio report … 3.9 MB sent` (per-session totals).

## 7. Not measured yet

- The ≥ 2 h acceptance session: covered here by 40 + 5 min with three forced
  interruptions on both ends. A long real session of Danny's on the new build
  is the better test, and `tools/analyze_client_log.py` reads its log as is.
- Plan item 13's cost (idle bitrate with a frame-rate floor and time-based
  keyframes), because it isn't implemented.
