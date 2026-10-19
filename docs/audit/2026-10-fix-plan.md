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
| 24 | Monotonic audio timestamps: wall-clock stamping of the PulseAudio input makes frames overlap (`Queue input is backward in time`, `Non-monotonic DTS`) | Flooded the log on 2026-10-19 (now contained). Candidate cause of the earlier silent-audio reports. | S / medium (A/V timing, needs a listening test) |
| 25 | Pause the stream while the client's display sleeps, or after long inactivity | On 2026-10-19 the host encoded for ~9 h to a Mac with its display off | S / low |

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
