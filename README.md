# LinuxPlay

> An experimental, ultra low latency remote desktop and game streaming stack for Linux.
> Built with FFmpeg, UDP, Qt, and zero corporate junk.

[![License: GPLv2](https://img.shields.io/badge/License-GPLv2-blue.svg)](LICENSE)
![Host: Linux (X11 + Wayland)](https://img.shields.io/badge/Host-Linux%20(X11%20%2B%20Wayland)-green.svg)
![Clients: Linux Windows macOS](https://img.shields.io/badge/Clients-Linux%20%2B%20Windows%20%2B%20macOS-blue.svg)
![FFmpeg](https://img.shields.io/badge/FFmpeg-Required-critical)
[![GitHub stars](https://img.shields.io/github/stars/Techlm77/LinuxPlay?style=flat)](https://github.com/Techlm77/LinuxPlay/stargazers)

**Personal fork of [Techlm77/LinuxPlay](https://github.com/Techlm77/LinuxPlay), targeting a KDE Plasma host and a macOS client.** Upstream is the general-purpose project — start there if your setup differs.

---

## Project Status: Experimental & Power User Focused

LinuxPlay is not a polished commercial product.
It’s an **experimental, community-driven toy for power users** who:

- are comfortable with FFmpeg, networking, and Linux internals,
- want **full control** over codec, bitrate, capture, buffers and behavior,
- are happy to run it on **trusted LANs or inside a secure VPN/WireGuard tunnel**,
- understand that **traffic is not end-to-end encrypted by LinuxPlay itself**.

If you expose these ports raw to the internet or run it on an untrusted network without a VPN:
**you are doing something this project does not recommend.**

For fast, safe usage:

- Use it on a wired home LAN **or**
- Run it strictly through **WireGuard/OpenVPN/SSH tunnels** and point the client at the tunnel IP.

---

## A Message from the Developer

Hey everyone,

First, the thing I should say up front: **this is a personal fork.** It is not the upstream LinuxPlay project, and it isn’t trying to be a general-purpose, distro-agnostic streaming stack. It’s a working copy I tune, patch and occasionally break for **exactly one setup**:

- **Host:** my Linux desktop running **KDE Plasma** (Wayland session — xdg-desktop-portal/PipeWire capture, `kscreen-doctor` for monitor layout).
- **Client:** **macOS**.

Everything else this codebase can do — X11 hosts, GNOME/wlroots capture, Windows clients, the various hardware encoders — is upstream’s work that I kept, not something I test or maintain. If your setup looks different from mine, go to [upstream](https://github.com/Techlm77/LinuxPlay) instead; it’s a one-person project that has earned the stars, and this fork exists only because of it.

The changes here are the ones my own setup demanded: KDE-specific capture fixes, macOS client work (VideoToolbox decode, audio, certificate auth), and the sort of logging and hardening you only add after your own machine has annoyed you enough times. None of it is validated against other people’s hardware.

So: read the code, take what’s useful, but assume anything described as “tested” was tested on one Plasma desktop and one Mac.

Thanks to the upstream author for building the thing in the first place — without it there would be nothing here to fork.

---

## Features

- **Codecs**
  - H.264 and H.265 (HEVC)
  - Hardware acceleration via **NVENC, QSV, VAAPI, AMF**, or CPU fallback.
- **Transport**
  - Ultra low latency design.
  - Video over MPEG-TS on UDP.
  - Audio over UDP.
  - Input (mouse/keyboard/gamepad) over UDP.
  - Clipboard sync over UDP.
  - Handshake and file upload over TCP.
- **Audio Features**
  - Surround Sound Support (5.1 / 7.1): Host detects and captures up to 8 audio channels.
  - Client performs intelligent downmixing (FFplay filters) to stereo for local speakers when necessary.
  - Playback is drift/gap tolerant (`aresample=async=1`) — no more jitter on a lossy link — and a
    watchdog restarts the player if its playout clock stops advancing, instead of going silent
    for the rest of the session.
- **Granular Encoder Control**
  - Direct control over FFmpeg parameters: **GOP, QP/CRF, Preset, Tune, and Pixel Format (`yuv420p`, `yuv444p`)** are fully exposed via command line arguments.
- **Advanced Capture Methods**
  - On Wayland (KDE/GNOME/wlroots): capture via the **xdg-desktop-portal ScreenCast API (PipeWire)** — permission dialog on first connect, persistable; cursor drawn into the stream.
  - On X11: **x11grab**; **kmsgrab** (lowest latency, requires setcap) as the compositor-agnostic fallback on GNOME/wlroots Wayland.
- **Secure Handshake Layer**
  - Rotating 6-digit PIN, refreshes every 30 seconds.
  - PIN rotation pauses while a session is active.
  - Single-session lock: new clients get `BUSY` while a session is live.
  - Certificate-based login after the first trusted PIN session.
- **PIN → Certificate Upgrade**
  - On first successful PIN auth, the host:
    - Acts as a mini CA.
    - Issues a per-device client certificate + key.
    - Exports: `client_cert.pem`, `client_key.pem`, `host_ca.pem` under `issued_clients/...`.
  - Copy these to the client folder next to `start.py` and `client.py` (via USB, SCP, etc.).
  - The client:
    - Detects the bundle.
    - Skips PIN.
    - Uses certificate-based authentication automatically.
- **Controller Support**
  - Full gamepad forwarding over UDP using a virtual uinput device.
  - Works with Xbox, DualSense, 8BitDo and other HID controllers (Linux client → Linux host; not yet available on the macOS client).
- **Multi-Monitor**
  - Stream one or more displays.
  - Resolutions and offsets auto-detected per monitor.
- **Clipboard & File Transfer**
  - Bi-directional clipboard sync.
  - Client → host file uploads over TCP with validation and safe paths.
- **Link-Aware Streaming**
  - Adapts buffer strategies for LAN vs Wi-Fi to reduce stalls and jitter.
- **Resilience**
  - Heartbeat (PING/PONG).
  - Host stops streams and returns to waiting state on timeout/disconnect.
- **Stats Overlay (Client)**
  - On-screen panel (**F1**, or start with `--stats`) with live numbers *and* graphs:
    video and encoder Mb/s, latency (RTT) and jitter, decode time, decode FPS,
    client CPU/GPU, host CPU/GPU, dropped frames — each with a rolling one-minute
    sparkline. Draws with QPainter over the video, ignores mouse events, never
    touches the OpenGL path.
- **Cross-Platform**
  - Host: Linux (X11 and Wayland sessions).
  - Clients: Linux, Windows, and macOS.
  - Wayland hosts: input via virtual uinput devices (kernel-level, no X11 required); monitor layout auto-detected via kscreen-doctor / wlr-randr / hyprctl.

---

## Why LinuxPlay?

Because sometimes you want:

- **No accounts.**
- **No vendor lock-in.**
- **No mystery processes.**
- A streaming stack you can **read**, **modify**, and **tune**.

LinuxPlay is intentionally **hands-on**:

- You choose the encoder, bitrate, GOP, pix_fmt, buffer sizes.
- You can see every FFmpeg command.
- You can measure every change.

If that sounds fun instead of scary, you’re the target audience.

---

## Architecture

```
Client                        Network           Host
------                        -------           ----
TCP handshake (7001)   <-------------------->  Handshake
UDP control (7000)      -------------------->  Input (mouse/keyboard)
UDP clipboard (7002)   <-------------------->  Clipboard sync
UDP heartbeat (7004)   <-------------------->  Keepalive (PING/PONG)
UDP gamepad (7005)      -------------------->  Virtual gamepad (uinput)
UDP video (5000+idx)   <--------------------   FFmpeg capture + encode
UDP audio (6001)       <--------------------   FFmpeg Opus audio
TCP upload (7003)      --------------------->  File upload handler
```

---

## Installation

### Option 1: Using run.sh (recommended)

```bash
chmod +x run.sh

# Check & install required tools and Python deps (on supported distros)
./run.sh check

# Launch the GUI
./run.sh start

# Or directly:
./run.sh host --gui --encoder h.264 --hwenc auto --bitrate 8M --audio enable
./run.sh client --host_ip 192.168.1.20 --decoder h.264 --hwaccel auto

# Run the test scripts (all of them, or those matching a name)
./run.sh test
./run.sh test host parsers
```

- Uses a local `.venv` inside the repo.
- Installs only missing dependencies inside that venv.
- Never installs Python packages globally.

---

### Option 2: Manual setup (Ubuntu 24.04 example)

#### System packages

```bash
sudo apt update
sudo apt install -y ffmpeg xdotool xclip pulseaudio-utils libcap2-bin wireguard-tools qrencode python3 python3-venv python3-pip libgl1 python3-evdev
```

If `pip install av` or `pip install cryptography` fails, install FFmpeg/Python dev headers:

```bash
sudo apt install -y pkg-config python3-dev libavdevice-dev libavfilter-dev libavformat-dev libavcodec-dev libswscale-dev libswresample-dev libavutil-dev
```

#### Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

#### Python packages (inside `.venv`)

```bash
python3 -m pip install -U pip wheel setuptools
python3 -m pip install PyQt5 PyOpenGL PyOpenGL_accelerate av numpy pynput pyperclip psutil evdev cryptography jeepney
```

`evdev` is required on Linux clients for controller capture.

### Option 3: macOS client (Homebrew)

The host is Linux-only. macOS runs the **client** — and on this fork that is the primary client, not a side path:

```bash
brew install ffmpeg python
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -U pip wheel setuptools
python3 -m pip install PyQt5 PyOpenGL PyOpenGL_accelerate av numpy pyperclip psutil cryptography jeepney

# Exactly like on Linux:
python3 client.py --host_ip 192.168.1.20 --decoder h.264 --hwaccel auto --audio enable
```

- `--hwaccel auto` selects **VideoToolbox** decoding when available (CPU fallback is automatic).
  The decoder is built by the **PyAV wheel's own FFmpeg**, which must offer the device — the client
  log says exactly what was negotiated: `Hardware decode <type> not offered by this FFmpeg build.`
  means that wheel can't do it and decoding stays on the CPU. The stats overlay shows the decoder
  in use (`decode CPU` vs `decode videotoolbox`).
- `./run.sh check` / `./run.sh client ...` also work on macOS — the bootstrap detects Homebrew and skips Linux-only pieces.
- Controller (gamepad) forwarding is not yet available on macOS; keyboard/mouse, video, audio, clipboard and certificate auth all work.
- `ffplay` for audio playback ships with Homebrew's `ffmpeg`.

---

## Usage

### GUI launcher

```bash
python3 start.py
```

- **Host** tab: pick a preset → **Start Host**.
- **Client** tab: enter host LAN IP or WireGuard tunnel IP → **Start Client**.
- If `client_cert.pem`, `client_key.pem`, `host_ca.pem` are present:
  - PIN field is disabled.
  - Client uses certificate auth automatically.

### Command line

```bash
# Host
python3 host.py --gui --encoder h.264 --hwenc auto --framerate 60 --bitrate 8M --audio enable --gop 15 --pix_fmt yuv420p --resolution native

# Client
python3 client.py --host_ip 192.168.1.20 --decoder h.264 --hwaccel auto --audio enable --monitor 0 --gamepad enable --debug
```

---

## Run the host at login (autostart)

```bash
./run.sh autostart enable              # install it (host window at login)
./run.sh autostart enable --headless   # ...or with no window
./run.sh autostart status              # entry, running host, portal state
./run.sh autostart stop                # stop a running host
./run.sh autostart disable             # remove it
```

It writes `~/.config/autostart/linuxplay-host.desktop`, so it runs **inside your desktop
session** — that is not a limitation to work around: Wayland capture goes through
xdg-desktop-portal on the session bus, and the X11/KMS paths need `DISPLAY` or `/dev/dri`.
There is no boot-time (root) variant, because there is nothing to capture before login.
The entry also shows up in **System Settings → Autostart**, where it can be toggled off.

- **Settings**: the command line is rebuilt at every login from the launcher's saved Host
  tab (`~/.linuxplay_start_cfg.json`), so what starts is what you last picked in the GUI.
  No saved settings yet? The Default profile is used (h.264, auto backend, 30 fps, 8M).
- **Working directory**: the entry pins it to the repo, because `host_ca.pem`,
  `host_ca.key` and `trusted_clients.json` are relative paths. Starting the host from
  another directory would create a *new* CA and forget every paired device.
- **One host per user**: the host holds a lock (`~/.local/state/linuxplay/host.lock`).
  A second start exits with a message instead of failing on the port bind, and the
  launcher notices a host it did not start — press **Start Host** and it offers to stop
  that one and take over. `./run.sh autostart stop` does the same from the shell.
- **Portal capture is a one-time grant**: the screen-share dialog appears when the *first
  client connects*, not at login. Approve it once and tick **remember** — the host stores a
  restore token (`~/.config/linuxplay/portal_restore.json`) and later logins capture without
  a prompt. Without that grant, a client connecting while you are away waits on a dialog
  nobody can click and gives up after two minutes. `./run.sh autostart status` says whether
  the token is there. Changing monitor layout can invalidate it, so re-grant when you rearrange.
- **Stopping**: closing the host window stops the host (it waits for encoders and the portal
  session to close). Logging out does the same via SIGTERM. A host started from this launcher
  keeps its own Stop button.
- **Logs**: `~/.local/state/linuxplay/autostart.log` records what the entry did (command line,
  waits, refusals to double-start); the host itself keeps logging to `host.log`.

---

## Network Modes

- Client auto-detects Wi-Fi vs Ethernet vs VPN tunnel and sends `NET WIFI` / `NET LAN` / `NET VPN`.
- Host adjusts buffers accordingly.
- Tailscale/WireGuard links (`tailscale0`, `wg*`, `tun*`, peers in `100.64.0.0/10`) are classified
  as `vpn`: they get the cautious buffering profile and `--ultra` is auto-disabled, because the
  tunnel path can change (direct ↔ relay) even when the local link looks fine.
- Manual override:
  - `client.py --net wifi`
  - `client.py --net lan`
  - `client.py --net vpn`
- Default: `auto`.

---

## Heartbeat & Reconnects

- Host sends `PING` every second.
- Expects `PONG` within 10 seconds.
- On timeout or client exit:
  - Stops streams.
  - Clears session state.
  - Returns to “Waiting for connection…”.
- Reconnects start video/audio again without manual restart.

---

## Stats Overlay

Press **F1** in the client (or launch with `--stats`) for a translucent panel in the
top-left corner. Everything on it is graphed over the last 60 seconds:

| Metric | Where it comes from |
|--------|---------------------|
| video in (Mb/s) | bytes actually demuxed on the client — the real received rate |
| encoder out (Mb/s) | the host's own `ffmpeg -progress` bitrate |
| latency / jitter (ms) | heartbeat round trip (the client echoes the host's timestamp, so no clock sync is needed) |
| decode (ms) / decode (fps) | frame timing in the decoder thread |
| client cpu / gpu (%) | this machine |
| host cpu / gpu (%) | the host's `STATS` broadcast |
| dropped (/s) | frames the host's encoder dropped, per second |

The header also shows link mode, connection state, uptime, reconnects, resolution,
hardware decoder and renderer; the footer shows total received data and keyframes/s.
Comparing *video in* against *encoder out* is the quickest way to spot packet loss.

Note: with host and client on the **same** machine the two processes collide on UDP 7004
(heartbeat/stats), so latency and host stats will read zero there. On separate machines
(the normal case, including over Tailscale) they work.

---

## Logs

- Host: `~/.local/state/linuxplay/host.log` (rotating, 2 MB × 3). The GUI prints the path at
  startup, and the file is written even when the host runs without a GUI — so a host that
  dies overnight leaves evidence instead of nothing.
- Launcher: `host-launch.log` / `client-launch.log` in the same directory. If a host or
  client exits with a non-zero code, the launcher shows the last lines in a dialog.
- Autostart: `autostart.log` in the same directory — what the login entry did (the exact
  command line, session waits, refusal to start a second host).
- Override the directory anywhere with `LINUXPLAY_STATE_DIR`.

---

## Ports on Host

| Purpose                 | Protocol | Port            |
|-------------------------|----------|-----------------|
| Handshake               | TCP      | 7001            |
| Video per monitor       | UDP      | 5000 + index    |
| Audio                   | UDP      | 6001            |
| Control (mouse/keyboard)| UDP      | 7000            |
| Clipboard               | UDP      | 7002            |
| File upload             | TCP      | 7003            |
| Heartbeat (PING/PONG)   | UDP      | 7004            |
| Gamepad controller      | UDP      | 7005            |

---

## Linux Capture Notes

- **kmsgrab** (lowest overhead, no cursor):
  ```bash
  sudo setcap cap_sys_admin+ep "$(command -v ffmpeg)"
  ```
- **x11grab** fallback on X11 when kmsgrab is not available.
- **VAAPI**:
  - Needs `/dev/dri/renderD128`.
  - Add your user to `video` group if needed.

### Wayland Hosts

LinuxPlay detects the session type automatically (`XDG_SESSION_TYPE` / `WAYLAND_DISPLAY`):

- **Capture**: on Wayland, LinuxPlay uses the **xdg-desktop-portal ScreenCast API (PipeWire)**
  — the same mechanism as OBS. Works on KDE Plasma, GNOME and wlroots compositors.
  The desktop shows a screen-share permission dialog on first connect (select your monitors;
  approval can be remembered via the portal's restore token). The cursor is drawn into the
  stream. Requires `gst-launch-1.0` with the `pipewiresrc` plugin (e.g. `gst-plugin-pipewire`
  on Arch, `gstreamer1.0-pipewire` on Debian/Ubuntu) and the pure-Python `jeepney` package.
  Fallback: `kmsgrab` (kernel/DRM level — works on GNOME/wlroots with
  `setcap cap_sys_admin+ep`, but **not** on KDE Plasma + amdgpu, where KWin allocates
  10-bit AR30 framebuffers that FFmpeg cannot read). `LINUXPLAY_CAPTURE=portal|kmsgrab|x11grab`
  overrides the automatic choice.
- **Monitors**: layout (including rotated/portrait displays and offsets) is auto-detected via
  `hyprctl` (Hyprland), `kscreen-doctor` (KDE Plasma) or `wlr-randr` (sway/wlroots), with
  `xrandr` under XWayland as a last resort.
- **Input**: mouse/keyboard are injected through virtual **uinput** devices
  (LinuxPlay Virtual Keyboard / Pointer / Wheel) — kernel-level, so they work identically on
  X11 and Wayland. Your user needs write access to `/dev/uinput`
  (e.g. `sudo usermod -aG input $USER` + re-login, or a udev rule; many distros already grant
  this to the active seat user). The gamepad server uses the same mechanism.
- **Audio / clipboard**: unchanged — PipeWire exposes PulseAudio compatibility on Wayland
  desktops, so `pactl` / `-f pulse` keep working.
- **Scaling**: the portal reports a monitor's *logical* size (1707x1067 for a 2560x1600 panel at
  150%), while the node hands out buffers at the panel's own size. LinuxPlay rescales in the
  `gst-launch` feeder, so fractionally-scaled desktops stream the same pixels you see.
- **Stream size**: `--resolution WxH` (launcher: *Stream Size*) encodes at that size instead of
  the monitor's, scaling the capture before it reaches the encoder — a smaller stream, and a
  smaller bill for bandwidth, host CPU and client decode. `--resolution native` (the default)
  streams each monitor at its own resolution. The desktop is untouched either way, and clicks
  stay aligned because the host tells the client both sizes. Two things to know: a size whose
  aspect ratio differs from the monitor's stretches the picture (the host log names a matching
  size, e.g. `1920x1200` for a 16:10 desktop), and streaming at a non-native size needs the
  matching client on the other end — an older client would map clicks by the scaled video.
  Odd sizes work but the encoder pads them to even (1707x1067 arrives as 1708x1068), so a
  standard size like 1920x1080 avoids the extra pixel row.
- **Capture rate**: the portal path is damage-driven, and how fast it can push depends on the
  panel's refresh and the captured resolution — measured ~48 fps at 2560x1440 on 60 Hz panels,
  but ~72 fps at 1707x1067 on a 119 Hz panel (a single PipeWire consumer either way; two
  consumers get roughly double between them). The feeder drops whatever exceeds `--framerate`
  (`videorate drop-only=true`), so the overlay's *encoder out* fps stays at your setting instead
  of running away with the bitrate.
- **Limitations**: each monitor must be selected in the portal dialog for multi-monitor
  streaming; NVIDIA kmsgrab users may need `nvidia-drm.modeset=1`.


---

## Recommended Presets

- **Lowest Latency**
  - H.264, 60–120 fps, GOP 8–15, low-latency tune.
- **Balanced**
  - H.264, 45–75 fps, 4–10 Mbit/s, GOP 15.
- **High Quality**
  - H.265, 30–60 fps, 12–20 Mbit/s, `yuv444p` if supported.

---

## Tests

The tests are standalone self-checking scripts (`test_*.py`), tied together by
`test_all.py`:

```bash
./run.sh test               # everything
./run.sh test host parsers  # only files whose name matches a substring
```

- Exit 0 = pass, exit 77 = **skipped** (this machine cannot run that section:
  no Wayland session, no `/dev/uinput`, an occupied port because a host is
  running, …), anything else = fail. `test_all.py` exits non-zero only on
  failures, so it can gate a commit.
- `test_portal.py` drives the real portal and needs you to approve the share
  dialog; run it explicitly rather than as part of a quick check. The portal is
  damage-driven, so on an idle desktop it produces no frames — the test reports
  that as SKIP instead of pretending to have verified capture.
- A few tests need a port that a running host/client owns (7003 uploads,
  7004 heartbeat, 6001 audio). Stop the host first, or expect SKIP.
- Nothing in the suite injects input into your session: the uinput tests only
  create the virtual devices, check their capability bitmaps and remove them.

---

## Security (Please Read!!)

- **LinuxPlay does not encrypt media/control traffic itself.**
- For **WAN / public / untrusted Wi-Fi**:
  - **Always run through WireGuard/OpenVPN/SSH tunnels.**
  - Point the client to the VPN/tunnel IP (e.g. `10.x.x.x`).
- For **trusted wired LAN at home**:
  - May be acceptable without VPN **if you fully trust all devices**.
- Authentication:
  - One active client at a time (`BUSY` for others).
  - First login via rotating PIN.
  - Subsequent logins via per-device certificate.
  - Private keys stay on the client; host tracks fingerprints.
  - Bad PINs cost the sender: after 3 failures from one address the host refuses that
    address for a growing cool-off (30 s, doubling up to 15 min). A bad PIN no longer
    rotates the displayed PIN, so an attacker cannot keep the code out of your hands.
  - Pairing a **new** device needs your approval in the host window before a certificate
    is issued (headless hosts auto-approve, since nobody is there to ask).
  - Every channel is session-token gated, including file uploads (TCP 7003).
- To revoke:
  - Edit or remove entries in `trusted_clients.json` on the host (read at handshake time),
    or set `"status": "revoked"` on the record.
- Trust boundary, stated plainly: the session token travels in **cleartext UDP**
  (every PONG carries it). Both ends pin the peer address — the client answers
  heartbeat/STATS only from the host it negotiated with, the host accepts
  control/upload only from the authenticated client IP — so an off-path peer
  cannot ask for the token or inject input, but a passive sniffer sees it, and
  one-way UDP source spoofing needs no reply. At the same time FFmpeg's
  video/audio *receive* ports accept datagrams from any source. That is the
  home-LAN trust model; anything you would not run an unencrypted stream on
  needs the VPN/tunnel.
- If a client asks for the PIN even though it was paired:
  - Enter the PIN — the client asks the host for a replacement certificate during that
    pairing, so the next connection skips the PIN again. A replaced pair is kept on the
    client as `client_cert.pem.replaced-<timestamp>` / `client_key.pem.replaced-<timestamp>`.
  - The host log names the check that failed, e.g. `[AUTH] Certificate proof from …: signature
    over the challenge does not match the certificate's key` (the client's key and
    certificate no longer belong together) or `… does not match the offered fingerprint`
    (the host no longer trusts that certificate).

---

## Support Upstream

LinuxPlay is:

- fully open-source,
- built from scratch,
- maintained in spare time by a solo developer.

**This fork earns nothing and takes no donations.** If you like it enough to want to say thanks, sponsor the project it is built on:

[Sponsor @Techlm77](https://github.com/sponsors/Techlm77)

Support helps cover upstream's hardware and testing, and makes it easier for others to join in and help turn this into a stronger ecosystem project.

---

## License

LinuxPlay is licensed under **GNU GPL v2.0 only**. See [`LICENSE`](LICENSE).
External tools like FFmpeg, xdotool, xclip, ffplay retain their own licenses.

---
