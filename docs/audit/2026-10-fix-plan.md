# Fix plan: stability first (October 2026)

Companion to [the audit](2026-10-audit.md). Effort: XS < 1 h · S ≈ half a day ·
M ≈ a day · L = several days. Risk is the chance of making a working session
worse. Everything marked **done** is on branch `stabilize`, as one commit per
row, with tests. Measured effects are in [measurements](2026-10-measurements.md).

## Done: stability (P1)

| # | change | findings | effort / risk | commits |
|---|--------|----------|---------------|---------|
| 1 | One idempotent `end_session()` that forgets the client before stopping. Closed windows stay closed. A start that outlives its session spawns nothing and arms no retry. NET restarts go through the session manager. | F3 | S / low | `ebdf64f`, `2aebbed` |
| 2 | Decode path under loss: keyframe gate (damaged stretches never reach a decoder), no FFmpeg parser (−1 frame of latency, correct corrupt attribution), keyframes from NAL types, oversized keyframes reassembled, hardware decode reset instead of abandoned, honest pacing counters, FFmpeg chatter tallied | F2, F7, N1 | M / medium (mitigated by the lab clips, a live smoke test and a 30 min A/B) | `3decef5`, `1a6394e` |
| 3 | Host writes the PES length (frames < 64 KB reach the decoder on arrival) | new (latency) | XS / low | `c946510` |
| 4 | Host audio follows the default sink (`--audio_source auto`, or a name to pin). Re-checked every 3 s, restarts rebuild the command, `silencedetect`, end-of-session `Audio report:` | F1, N3 | M / low–medium | `1093cd0` |
| 5 | Client audio: silence while the clock runs is logged, player starts in 0.2 s (was 4.8 s), exit lines named, `Audio report:`, mute | F1 | S / low | `da06c26` |

## Done: observability and UX (P2)

| # | change | findings | effort / risk | commits |
|---|--------|----------|---------------|---------|
| 6 | Host STATS fps / kbit/s over the last 5 s, plus a window field; old hosts labelled "(session avg)" | F4 | S / low | `b6d1ec3` |
| 7 | Portal: token reuse at INFO, prompts recognised by their duration, honest "no token" line, `NOTE portal-wait` to the client; live portal test opt-in with a throwaway token | F6, N7 | S / low | `296ea84` |
| 8 | Status banner (reconnecting / waiting for video / frozen / CPU decode / audio dropped / muted / host awaiting approval); macOS Audio ▸ Mute Host Audio (⌘⇧M) | E | S / low | `2f6281b` |
| 9 | Overlay loss line and decode state; launcher "muted" choice and pixel-format hint | E, N2 | XS / low | `cd99280` |
| 10 | Portal VAAPI command drops the ignored `-pix_fmt`; says once that 4:4:4 does not apply | N2 | XS / low | `4d917ca` |
| 11 | Suite green on both machines (environment assumptions removed) | N7 | XS / none | `10c585e`, `308f56d` |
| 12 | Measurement tools: headless probe client, log analyzer | — | S / none | `84be001`, `3642b90` |
| 13a | NET (network mode) only records the mode; the session manager rebuilds streams made for another mode, so the control thread never waits out a portal prompt | F3 (NET path) | S / low | `6b1471c` |
| 13b | A stopped encoder's open silence isn't reported as sound returning; encoder numbers reset per session | live run C | XS / none | `e5f7f75`, `cb5cbde` |
| 13c | README describes the new audio, overlay and tests | — | XS / none | `f18e360` |

## Done: the 2026-10-19 stale-session incident

See [the incident report](2026-10-19-stale-session.md).

| # | change | cause | effort / risk | commits |
|---|--------|-------|---------------|---------|
| 20 | Bounded, batched GUI log view; encoder stderr repeat gate | log flood starved every host thread, then a Qt abort | S / low | `c3a2300` |
| 21 | The device that owns the live session can replace it; no PIN prompt for `BUSY` or a silent host | five hours of `BUSY` to its own reconnects; a PIN asked for that could not help | S / low | `0e9cc67` |
| 22 | Client and launch logs keep three previous runs | the relaunch erased the night's client log | XS / none | `84f62ec` |
| 23 | Upload test waits for its listener | 1-in-12 flaky `test_hardening` | XS / none | `76432b8` |

## Next (P3): recommended, not done

| # | change | why | effort / risk |
|---|--------|-----|---------------|
| 13 | **Bounded recovery on quiet desktops:** `pipewiresrc keepalive-time=100..250` so frames keep flowing on a still screen, plus time-based keyframes (`-force_key_frames expr:gte(t,n_forced*1)`) with a longer `-g` | After a loss the picture stays frozen until 15 more *damage* frames. That's ~0.5 s on a busy screen, seconds on a quiet one (N5), and the frame you wait for often carries what you just typed. Also cuts IDRs on busy screens from ~2/s to 1/s. | S / medium: idle bitrate and GPU cost need an A/B with the probe |
| 14 | Keyframe on demand (client asks after a loss) | Turns a ~300 ms freeze into ~1 round trip. The ffmpeg CLI can't force an IDR at runtime: needs an in-process encoder (PyAV + `hevc_vaapi`) or a GStreamer `vah265enc` with force-key-unit events. | L / high |
| 15 | Rate control: VAAPI VBR is told 60 fps while capture averages ~24 fps, so the stream uses ~40% of `--bitrate`; `-bufsize` of 1 s doesn't bound IDR size | Quality on busy content; IDR burst size | S / medium (needs a quality comparison) |
| 16 | Try a session with AWDL off on the Mac (`sudo ifconfig awdl0 down`; AirDrop/Continuity come back on reboot) | Loss is Wi-Fi micro-outages (N4); AWDL off-channel scans are a common cause. It's an operational test, not code. | XS / none (Danny's call) |
| 18 | When VideoToolbox rejects a keyframe, decode that GOP in software and return to hardware at the next keyframe, instead of reopening and waiting | VideoToolbox intermittently rejects *valid* `hevc_vaapi` keyframes (4 in 40 min in run C). The CPU decoder takes them. Today each costs a freeze to the next keyframe. | M / medium |
| 19 | Flag whole-frame losses the demuxer can't see: compare the gap between consecutive frames' PTS with the host's frame cadence, or have the host number frames | With PES lengths, a lost small frame is only noticed when the next one is refused. Same freeze, but one frame of decoder chatter. | S / low |
| 17 | Remove the decoder settings PyAV 18 silently ignores (`low_delay`, `strict_std_compliance`, `flags2="+fast"`) | Code says something it doesn't do (N6) | XS / low |
| 24 | Monotonic audio timestamps: wall-clock stamping of the PulseAudio input makes frames overlap (`Queue input is backward in time`, `Non-monotonic DTS`). **Fixed in `ea7608b`, deployed 2026-10-19 18:43, awaiting the listening test:** `asetpts=N/SR/TB` ahead of `silencedetect` ([measurements below](#item-24-audio-timestamps)) | Flooded the log on 2026-10-19 (now contained). Candidate cause of the earlier silent-audio reports. | S / medium (A/V timing, needs a listening test) |
| 25 | Pause the stream while the client's display sleeps, or after long inactivity | On 2026-10-19 the host encoded for ~9 h to a Mac with its display off | S / low |

### Item 24: audio timestamps

Measured on doggoos on 2026-10-19 with no session running. These were
standalone runs of the host's audio command into files. Scripts and output
are in `~/linuxplay-audit/lab/item24/` on the box.

**Cause.** The PulseAudio capture hands ffmpeg audio in 50 ms reads, and
`-use_wallclock_as_timestamps 1` stamps each read when it returns. On a calm
machine the stamps jitter by about 2 ms: a quarter of the reads are stamped
up to 2.1 ms before the previous read's audio ends. libopus only complains
when a frame's timestamp fails to increase, so calm runs log nothing. The
current command was silent in three calm 60 s runs: the HDMI monitor with
both Opus settings, and the WiVRn monitor. When ffmpeg falls behind, though,
the reads that catch up get near-equal stamps and time goes backwards. In
these runs ffmpeg was paused with SIGSTOP for 60–250 ms every 0.4–1 s, as a
busy host would pause it:

| 120 s, 140 pauses, exact host command (`audio`, 20 ms frames) | `backward in time` | `Non-monotonic DTS` | packets out | timestamp steps |
|---|---|---|---|---|
| before (`b5af77c`) | 424 | 762 | 6046 | 803 bumped to +1 tick by the muxer, jumps up to 263 ms |
| after (`asetpts=N/SR/TB`) | **0** | **0** | 6046 | all exactly 20 ms |

The other candidates, all run together with the same pauses (90 s, 24
pauses of 0.3–1.5 s):

| variant | warnings | note |
|---|---|---|
| current | 414 + 868 | jumps up to 1.49 s |
| no `-use_wallclock_as_timestamps` (the pulse demuxer's own smoothed clock) | 0 | steps within 0.6 ms of 20 ms. It relies on PulseAudio's latency reports and ffmpeg's time filter, so it isn't monotonic by construction. |
| `aresample=async=1:first_pts=0` | 0 | Pads the audio to follow the stamps: +60 ms here, +100 ms in a 60 s run with short pauses. That is added latency and inserted sound. The client already dropped `async` because it added 6 s of playout delay. |
| `asetpts=N/SR/TB` | 0 | renumbers frames by sample count; no buffering, same samples |

**Choice: `asetpts=N/SR/TB`**, first in the encoder's filter chain.
Timestamps now follow the sound card's sample clock, not the wall clock.
Nothing lines them up with video: audio and video are separate ffmpeg
processes on separate ports, and the client's ffplay plays audio on its own
clock. The input flags shared with video are unchanged. `silencedetect`
still reports; its times now start at 0 and count samples. `test_host.py`
pins the chain.

**Why the 2026-10-19 flood held steady (inferred).** A slow stderr reader
alone causes no warnings, because ffmpeg 7+ writes progress from its main
thread while capture and encoding run on threads of their own. A 90 s run
with stderr drained at 300 B/s through a 4 KB pipe logged 0. A warning,
though, is written by the encoding thread itself. If the host is slow to
read, that thread waits on the pipe, the capture falls behind meanwhile, and
the catch-up reads produce more warnings. On 10-19 there were 4 warnings in
the hour before 01:05. The encoder's restart on the HDMI monitor at 01:05:39
gave 337 in its first 20 s and then a flat 988–995 a minute for 70 minutes
(01:06–02:15). A rate that flat is set by the reader, not by the capture.
It collapsed at 02:16, when the client's reconnects began.

**Link to the silent-audio reports (inferred).** That session's
`Audio report` says 238.1 MB in 8h13m, i.e. 8.0 KB/s. A silent stream with
the same settings sends 13.2 KB/s (measured here, and in today's 18:13
sessions), and sound only adds to that. So at most about 60% of the
session's audio left the encoder. Hours of it were held up, which the client
hears as gaps or silence while video plays on. The client log for that night
is gone, so this stays a candidate. With the fix, a late read logs nothing,
so there's nothing to start the loop, and the repeat gate and bounded log
view (`c3a2300`) keep the reader fast anyway.

**Seen, not changed.** The server picks the 50 ms read size, so audio leaves
the host in 50 ms lumps, the first of them up to 50 ms late. `-fragment_size`
on the pulse input could take back most of that, but it needs its own
latency A/B.

**Deployed** 2026-10-19 18:43 (autostart unit, host idle). The suites
pass on doggoos. A 2-minute muted probe session ran: certificate auth, portal
restored with no dialog, the encoder ran with the new chain, silencedetect
still reported, 1.6 MB sent (13.3 KB/s), no timestamp warnings, clean
`GOODBYE`.

**Still to do:** Danny's listening test. Check lip-sync on a talking-head video or a hand clap, play
a few minutes of a game, then run a long session. The host log should show
no `backward in time` or `Non-monotonic DTS` lines.

## Deliberately skipped, and why

- **`output_corrupt` (decode frames with missing references).** It makes the
  counter honest, but decodes against a grey placeholder: a static desktop
  flashes grey until the next keyframe. The keyframe gate gives the same
  honesty with a clean freeze.
- **Pacing or FEC on the UDP sender.** Bursts up to 400 datagrams were
  lossless; the loss that does happen is runs of ~44 datagrams during Wi-Fi
  stalls. Pacing doesn't touch that, and FEC able to cover it costs more than
  it saves at this loss rate (one event per ~85 s).
- **SRT/ARQ transport.** It would recover those runs, but adds a fixed
  2–4× RTT to every frame and replaces the transport on both ends. Worth it
  only if the freezes in item 13 stay annoying after the cheap fixes.
- **In-process audio (PyAV + QAudioOutput) instead of ffplay.** ffplay never
  exited in 51.8 h; the failures were silence the player couldn't know
  about. Muting, silence detection and fast restarts now work with ffplay.
  Revisit if switching the Mac's output device turns out to break playback.
- **F5 (2195 vs 2194).** Normalised mapping, < 1 logical px. Revisit only
  if a second monitor gets attached.
- **Following "whatever is playing" instead of the default sink.** The
  default sink is what the host's own speakers play, and it's predictable.
  `--audio_source <name>` pins a specific sink for the cases where it's not
  what Danny wants.
- **Upstream paths** (X11, kmsgrab, NVENC/QSV, Windows/Linux clients). Not
  touched beyond keeping them compiling and their tests passing. The macOS
  menu bar is macOS-only, so other clients keep upstream's window.
- **Upstreaming to Techlm77.** Not planned (the default assumption in the
  briefing).
