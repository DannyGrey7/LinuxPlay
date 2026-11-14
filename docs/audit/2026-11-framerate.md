# LinuxPlay fork — host capture frame rate (2026-11-13)

Self-contained. Follows the [November audit](2026-11-audit.md) (§3 machines,
§10 fixes). Verified unless marked *inferred*.

## 1. Gist

1. **The 12–30 fps was a cap, not the screen's change rate.** A 60 fps test
   window (GStreamer `fpsdisplaysink`: 60.00 fps rendered, 0 dropped) gave
   30.0 captured frames every second for 20 s on the old feeder.
2. **The cap is KWin's SHM screencast path.** For each frame of an SHM
   (MemFd) stream, KWin 6.8.1 allocates a new 3840x2160 texture, renders
   the whole output into it, and reads 33 MB back with `glReadnPixels` on
   its main thread (`outputscreencastsource.cpp` `render(QImage*)`,
   `screencastutils.h` `grabTexture`). A DMA-BUF stream (WayVR's) gets the
   frame rendered straight into its own buffer, with buffer-age partial
   repaints and no readback.
3. **Fix: a GPU feeder, now the default** (`0b22859`, default since
   `be5ce8b`). `pipewiresrc` negotiates DMA-BUF (modifier `0x0200000020801b04`,
   the same as WayVR's). `vapostproc` scales 3840x2160 to 2194x1234 and
   converts to NV12 on the GPU. ffmpeg reads NV12 and only uploads it.
   `LINUXPLAY_PORTAL_GPU=0` goes back to the CPU feeder. A GPU feeder that
   fails in its first 60 s hands over to the CPU feeder by itself.
4. **Result: 60 fps with VR running.** The old feeder gave 10–12 fps in
   the 2-hour VR session. Danny confirmed the picture on the Mac looks
   right.

## 2. Measurements (doggoos, 2026-11-13 22:30–22:55)

Same 640x360 60 fps test window (`videotestsrc pattern=ball ! waylandsink`)
for both runs, 20 s each, sampled every second.

| | old: SHM feeder | new: GPU feeder |
|---|---|---|
| captured fps (per second) | 30.0 every second | 59.8 mean (59.0–60.3); a second run 60.0 every second |
| VR during the run | WayVR capture paused | WayVR capturing, WiVRn using 27% of the video encoder |
| feeder (gst-launch) CPU, % of a core | 42 | 9–10 |
| encoder (ffmpeg) CPU | 26 | 9 |
| kwin_wayland main thread CPU | 16 (≈5.3 ms per captured frame) | 8 (≈1.3 ms per frame) |
| pipe bytes per frame | 10,829,584 (BGRx 2194x1234) | 4,064,796 (NV12, padded to 2196x1234) |
| PipeWire buffers | MemFd | DMA-BUF |

- Captured fps is the encoder's pipe reads: `/proc/<ffmpeg>/io` rchar delta
  ÷ bytes per frame. KWin itself delivered ~30/s to the old feeder
  (pipewiresrc's thread woke 31.7/s), so the feeder was not dropping frames.
- Readback on its own (a 40-line C copy of KWin's per-frame steps, idle GPU):
  5.4 ms per 4K frame, 8.6 ms at worst.
- GPU feeder, synthetic: up to 337 fps for 4K BGRx to 2194x1234 NV12,
  2.55 ms of CPU per frame.
- After the switch, Danny's own desktop streamed at 59.8 fps. The encoder
  engine took 13.6% (2.3 ms a frame), with Parsec idle.

## 3. Why exactly 30, 20 or 12 (source verified, timing inferred)

KWin 6.8 paces each screencast on a fixed 60 Hz software-vsync grid
(`screencaststream.cpp`, `utils/softwarevsyncmonitor.cpp`). New damage arms
the next grid tick, and the frame is recorded on that tick. The main thread
must be free again before the next tick to arm it. So a stream gets 60/n
fps: 60, 30, 20, 15, 12, 10. All the observed rates are on that list: 30
(now), median 28 (the Nov 10–13 run, 5-second means), 10–12 (the VR
session).

*Inferred:* the SHM record (re-render, texture allocation, synchronous
readback, plus the main thread's other work) took more than one 16.7 ms
slot. With VR loading the GPU, the readback's wait grew to about five
slots, which gave 12 fps. The DMA-BUF record does not wait for the GPU
(`glFlush` only), so it fits in one slot. KWin's main thread was not
traced: it is non-dumpable (`cap_sys_nice`), so perf and wchan are root-only.

## 4. The handoff's open questions

1. **Cap or content?** A cap (§1.1).
2. **Why 30 after the 21:47 reconnect, while the old stream stayed at 12
   after VR went idle at 21:45?** Not settled. There are no host-side
   numbers for 21:45–21:47. The GPU feeder makes the question moot: it
   gave 60 with VR on.
3. **Why does WayVR get more frames?** It takes DMA-BUF (§1.2). Verified in
   KWin's source and by the A/B in §2.
4. **Did the capture path change since "before"?** Not settled. KWin went
   6.7.5 → 6.8.0 (Oct 16) → 6.8.1 (Oct 27). 6.7.5 has the same SHM
   re-render and readback. Only the pacing differs: 6.7 waits ≥ 16 ms after
   the last frame was sent, 6.8 uses the grid. With a slow readback,
   neither version is fast.

## 5. Details the GPU feeder depends on

- **NV12 layout.** GStreamer pads NV12 rows to 4 bytes and the row count to
  even (`video-info.c`), so 2194x1234 arrives as 2196x1234. 600 frames were
  exactly 600 × 4,064,796 bytes. The encoder reads
  `-video_size 2196x1234` and crops with `crop=2194:1234:0:0`. Reading it
  as 2194 would start each frame 3,702 bytes early, and the picture would
  roll. `portal_capture.nv12_frame_dims` is the single source of this size
  (`test_portal.py` and `test_host.py` pin it).
- **Colour.** The client uses BT.601 for an untagged stream. vapostproc
  writes BT.709 by default. `colorimetry=bt601` also converts the
  primaries to SMPTE 170M, which moved saturated reds and greens by up to
  13 code values. `colorimetry=2:4:7:1` (limited range, BT.601 matrix, the
  source's own transfer and primaries) gave exactly swscale's values on
  SMPTE bars: yellow 210/16/146, green 145/54/34, red 81/90/240.
- **Negotiation.** pipewiresrc lists DMA-BUF formats before SHM ones.
  PipeWire walks the consumer's list in order. KWin re-offers SHM when a
  DMA-BUF modifier fails (`onStreamParamChanged`), and vapostproc also takes
  SHM. So a compositor without DMA-BUF still negotiates; it was checked
  against an SHM test source (59.1 fps). `videorate drop-only` needs a
  source at ≥ 60 fps or a variable rate; KWin offers a variable rate.
- **Availability.** The va plugin (gst-plugins-bad) only registers
  vapostproc when the VA driver can post-process. Without it, the host
  uses the CPU feeder.

## 6. Left open

- **Encoder engine sharing.** LinuxPlay, Parsec and WiVRn encode on the same
  GPU. At 60 fps LinuxPlay needs about twice the encoder time it needed at
  30.
- **Full GPU path** (`vapostproc ! vah265enc`, ffmpeg only remuxing). This
  would also remove the 4 MB download and the upload. It needs rate
  control, GOP, slices and `--adaptive` mapped to vah265enc (fix-plan
  item 14).
- **Untested:** GNOME, wlroots, Intel and NVIDIA hosts. With NVIDIA there is
  no vapostproc, so the host uses the CPU feeder.
- **Not reached:** the 30 fps cap itself is in KWin. It still applies to any
  SHM consumer and to hosts without vapostproc.
