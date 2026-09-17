#!/usr/bin/env bash
set -euo pipefail

PY="${PYTHON:-python3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

banner() {
  printf '\n\033[1;34m╔══════════════════════════════════════════════╗\033[0m\n'
  printf '\033[1;34m║\033[0m         LinuxPlay Bootstrap Environment      \033[1;34m║\033[0m\n'
  printf '\033[1;34m╚══════════════════════════════════════════════╝\033[0m\n'
}

section() {
  printf '\n\033[1;36m── %s ─────────────────────────────────────\033[0m\n' "$1"
}

msg()  { printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[!!]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[ERR]\033[0m %s\n" "$*"; }

need_cmd() { command -v "$1" >/dev/null 2>&1; }
bail() { err "$1"; exit 1; }

usage() {
  cat <<EOF
Usage:
  ./run.sh check              # Check/install tools and Python deps
  ./run.sh start [args...]    # Run start.py (GUI launcher)
  ./run.sh host  [args...]    # Run host.py
  ./run.sh client [args...]   # Run client.py
  ./run.sh test [name...]     # Run the test_*.py scripts (all, or those matching a name)
  ./run.sh autostart <cmd>    # Start the host at login (see below)

Autostart commands:
  ./run.sh autostart enable [--headless]   # install the login autostart entry
  ./run.sh autostart disable               # remove it
  ./run.sh autostart status                # entry, running host, portal token
  ./run.sh autostart stop                  # stop a running host

Behavior:
  - Host: Linux only (X11 or Wayland). Clients: Linux, Windows, macOS
  - Uses a local .venv in this repo (no global pip installs)
  - Installs only missing system deps (where supported)
  - Installs only missing Python deps into .venv
  - The autostart entry re-reads the launcher's saved Host settings each login

  PyQt5 PyOpenGL PyOpenGL_accelerate av numpy pynput pyperclip psutil evdev cryptography jeepney
  (pynput/evdev are skipped on macOS — client-only platform)
EOF
}

detect_pm() {
  if [ "$(uname -s)" = "Darwin" ]; then
    need_cmd brew && { echo brew; return; }
    echo unknown
    return
  fi
  if   need_cmd apt-get; then echo apt
  elif need_cmd dnf;      then echo dnf
  elif need_cmd zypper;   then echo zypper
  elif need_cmd pacman;   then echo pacman
  elif need_cmd apk;      then echo apk
  elif need_cmd emerge;   then echo emerge
  elif need_cmd nix-env || need_cmd nix; then echo nix
  else echo unknown; fi
}

pkg_for() {
  local pm="$1" cmd="$2"
  case "$pm" in
    brew)
      case "$cmd" in
        ffmpeg|ffplay)          echo "ffmpeg" ;;
        python3)                echo "python" ;;
      esac
      ;;
    apt)
      case "$cmd" in
        ffmpeg|ffplay)          echo "ffmpeg" ;;
        xdotool)                echo "xdotool" ;;
        xclip)                  echo "xclip" ;;
        pactl)                  echo "pulseaudio-utils" ;;
        setcap)                 echo "libcap2-bin" ;;
        wg)                     echo "wireguard-tools" ;;
        qrencode)               echo "qrencode" ;;
        glxinfo)                echo "mesa-utils" ;;
        pkg-config)             echo "pkg-config" ;;
        python3)                echo "python3" ;;
        python3-venv)           echo "python3-venv" ;;
        python3-pip)            echo "python3-pip" ;;
      esac
      ;;
    dnf)
      case "$cmd" in
        ffmpeg|ffplay)          echo "ffmpeg" ;;
        xdotool)                echo "xdotool" ;;
        xclip)                  echo "xclip" ;;
        pactl)                  echo "pulseaudio-utils" ;;
        setcap)                 echo "libcap" ;;
        wg)                     echo "wireguard-tools" ;;
        qrencode)               echo "qrencode" ;;
        glxinfo)                echo "mesa-demos" ;;
        pkg-config)             echo "pkgconf" ;;
        python3)                echo "python3" ;;
        python3-pip)            echo "python3-pip" ;;
        python3-venv)           echo "python3-virtualenv" ;;
      esac
      ;;
    zypper)
      case "$cmd" in
        ffmpeg|ffplay)          echo "ffmpeg" ;;
        xdotool)                echo "xdotool" ;;
        xclip)                  echo "xclip" ;;
        pactl)                  echo "pulseaudio-utils" ;;
        setcap)                 echo "libcap-progs" ;;
        wg)                     echo "wireguard-tools" ;;
        qrencode)               echo "qrencode" ;;
        glxinfo)                echo "Mesa-demo-x" ;;
        pkg-config)             echo "pkg-config" ;;
        python3)                echo "python3" ;;
        python3-venv)           echo "python3-venv" ;;
        python3-pip)            echo "python3-pip" ;;
      esac
      ;;
    pacman)
      case "$cmd" in
        ffmpeg|ffplay)          echo "ffmpeg" ;;
        xdotool)                echo "xdotool" ;;
        xclip)                  echo "xclip" ;;
        pactl)                  echo "pulseaudio" ;;
        setcap)                 echo "libcap" ;;
        wg)                     echo "wireguard-tools" ;;
        qrencode)               echo "qrencode" ;;
        glxinfo)                echo "mesa-demos" ;;
        pkg-config)             echo "pkgconf" ;;
        python3)                echo "python" ;;
        python3-pip)            echo "python-pip" ;;
      esac
      ;;
    apk)
      case "$cmd" in
        ffmpeg|ffplay)          echo "ffmpeg" ;;
        xdotool)                echo "xdotool" ;;
        xclip)                  echo "xclip" ;;
        pactl)                  echo "pulseaudio-utils" ;;
        setcap)                 echo "libcap-utils" ;;
        wg)                     echo "wireguard-tools" ;;
        qrencode)               echo "qrencode" ;;
        pkg-config)             echo "pkgconf" ;;
        python3)                echo "python3" ;;
        python3-pip)            echo "py3-pip" ;;
        python3-venv)           echo "py3-virtualenv" ;;
      esac
      ;;
  esac
}

auto_install_missing_system() {
  local missing_cmds=("$@")
  local pm; pm="$(detect_pm)"

  [ "${#missing_cmds[@]}" -eq 0 ] && return 0

  if [[ "$pm" == "unknown" || "$pm" == "emerge" || "$pm" == "nix" ]]; then
    warn "Auto-install not supported on this system."
    warn "Please install manually: ${missing_cmds[*]}"
    return 1
  fi

  local pkgs=()
  local seen=""

  for cmd in "${missing_cmds[@]}"; do
    local pkg; pkg="$(pkg_for "$pm" "$cmd" || true)"
    if [ -n "$pkg" ] && [[ " $seen " != *" $pkg "* ]]; then
      pkgs+=("$pkg")
      seen+=" $pkg"
    elif [ -z "$pkg" ] && [[ " $seen " != *" $cmd "* ]]; then
      pkgs+=("$cmd")
      seen+=" $cmd"
    fi
  done

  [ "${#pkgs[@]}" -eq 0 ] && return 0

  section "Installing missing system packages ($pm)"
  printf 'Packages: %s\n' "${pkgs[*]}"

  case "$pm" in
    apt)
      sudo apt-get update -y
      sudo apt-get install -y --no-install-recommends "${pkgs[@]}"
      ;;
    dnf)
      sudo dnf install -y "${pkgs[@]}"
      ;;
    zypper)
      sudo zypper --non-interactive refresh
      sudo zypper --non-interactive install --no-recommends "${pkgs[@]}"
      ;;
    pacman)
      sudo pacman -Sy --noconfirm --needed "${pkgs[@]}"
      ;;
    brew)
      brew install "${pkgs[@]}"
      ;;
    apk)
      sudo apk add --no-cache "${pkgs[@]}"
      ;;
  esac
}

check_python_version() {
  if ! need_cmd "$PY"; then
    printf "ERROR: python3 not found\n"
    return 1
  fi
  local want="3.9.0"
  local got
  got="$("$PY" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo "0")"
  # Compare with Python itself: BusyBox sort has no -V, so on Alpine the old
  # `sort -V -C` check always failed and reported a bogus version error.
  if "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info[:3] >= (3, 9, 0) else 1)' 2>/dev/null; then
    printf "OK:    Python3 %s >= %s\n" "$got" "$want"
    return 0
  else
    printf "ERROR: Python3 %s < %s\n" "$got" "$want"
    return 1
  fi
}

check_apt_dev_libs() {
  local missing_apt=()
  local apt_libs=(
    pkg-config
    python3-dev
    libavdevice-dev
    libavfilter-dev
    libavformat-dev
    libavcodec-dev
    libswscale-dev
    libswresample-dev
    libavutil-dev
    libgl1
  )

  for p in "${apt_libs[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || missing_apt+=("$p")
  done

  if [ "${#missing_apt[@]}" -gt 0 ]; then
    section "Installing FFmpeg/OpenGL dev libraries (apt)"
    printf 'Packages: %s\n' "${missing_apt[*]}"
    sudo apt-get update -y
    sudo apt-get install -y --no-install-recommends "${missing_apt[@]}"
  fi
}

check_system_deps() {
  section "System dependency check"
  local missing=()

  check_python_version || missing+=("python3")

  local tool_list
  if [ "$(uname -s)" = "Darwin" ]; then
    # macOS is a client-only platform; host-side tools are not required
    tool_list="ffmpeg ffplay python3"
    msg "macOS detected: client mode (host-only tools skipped)."
  else
    tool_list="ffmpeg ffplay xdotool xclip pactl setcap wg qrencode pkg-config"
  fi
  for cmd in $tool_list; do
    if need_cmd "$cmd"; then
      printf "OK:    %-10s (%s)\n" "$cmd" "$cmd"
    else
      printf "MISSING: %-8s\n" "$cmd"
      missing+=("$cmd")
    fi
  done

  if [ "$(uname -s)" != "Darwin" ] && [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
    if need_cmd gst-launch-1.0 && gst-inspect-1.0 pipewiresrc >/dev/null 2>&1; then
      msg "Wayland portal capture available (pipewiresrc + GStreamer)."
    else
      warn "Wayland session: GStreamer pipewiresrc not found (install gst-plugin-pipewire + gstreamer)."
      warn "  -> Required for screen capture on KDE/GNOME Wayland (portal capture)."
    fi
  fi

  if need_cmd glxinfo; then
    printf "OK:    OpenGL probe (glxinfo)\n"
  else
    printf "WARN:  glxinfo not found (OpenGL probe skipped)\n"
  fi

  if [ "${#missing[@]}" -gt 0 ]; then
    auto_install_missing_system "${missing[@]}" || return 1

    section "Re-check after install"
    for cmd in "${missing[@]}"; do
      if ! need_cmd "$cmd"; then
        err "Still missing required tool: $cmd"
        return 1
      else
        printf "OK:    %-10s (%s)\n" "$cmd" "$cmd"
      fi
    done
  fi

  if [ "$(detect_pm)" = "apt" ]; then
    check_apt_dev_libs
  fi

  msg "All core system tools detected."
  return 0
}

ensure_venv() {
  if [ ! -d ".venv" ]; then
    section "Python virtual environment"
    msg "Creating local virtualenv at: $SCRIPT_DIR/.venv"
    "$PY" -m venv .venv || bail "Failed to create virtualenv (install python3-venv)."
  fi

  source .venv/bin/activate || bail "Failed to activate .venv"

  if [ -z "${VIRTUAL_ENV:-}" ]; then
    bail "VIRTUAL_ENV not set after activation (refusing to touch global pip)."
  fi

  printf '\n\033[1;35m[env]\033[0m Using isolated Python environment: \033[1m%s\033[0m\n' "$VIRTUAL_ENV"
}

ensure_pydeps() {
  section "Python dependencies (.venv)"
  echo "Required:"
  echo "  PyQt5 PyOpenGL PyOpenGL_accelerate av numpy pynput pyperclip psutil evdev cryptography"
  echo

  local missing
  set +e
  missing="$(
    python - <<'EOF'
import importlib, sys

MAC = sys.platform == "darwin"
MODS = {
    "PyQt5":                "PyQt5",
    "OpenGL":               "PyOpenGL",
    "OpenGL.GL":            "PyOpenGL",
    "PyOpenGL_accelerate":  "PyOpenGL_accelerate",
    "av":                   "av",
    "numpy":                "numpy",
    "jeepney":              "jeepney",
    "pyperclip":            "pyperclip",
    "psutil":               "psutil",
    "cryptography":         "cryptography",
}
if not MAC:
    # host / Linux-client only modules
    MODS["pynput"] = "pynput"
    MODS["evdev"] = "evdev"

missing = {}
for mod, pkg in MODS.items():
    try:
        importlib.import_module(mod)
        print(f"OK:    {pkg}")
    except Exception:
        missing[pkg] = True
        print(f"MISSING: {pkg}")

if missing:
    sys.stdout.write("MISSING_PKGS " + " ".join(sorted(missing.keys())))
    sys.exit(1)
sys.exit(0)
EOF
  )"
  local status=$?
  set -e

  local to_install=""
  if [ $status -ne 0 ]; then
    to_install="$(printf "%s\n" "$missing" | awk '/^MISSING_PKGS /{ $1=""; sub(/^ /,""); print }')"
    if [ -z "$to_install" ]; then
      # The probe itself failed (a broken venv python prints no MISSING_PKGS
      # line). Reporting "all packages available" here used to hand the user a
      # launcher that immediately died on an import.
      err "The .venv Python could not run the dependency probe:"
      printf "%s\n" "$missing" | sed 's/^/  /'
      bail "The environment is broken — recreate it: rm -rf .venv && ./run.sh check"
    fi
  fi

  if [ -n "$to_install" ]; then
    # The names come from the probe's hardcoded module list; whitelist them so
    # a tampered probe cannot smuggle pip options into the install line.
    local pkgs=()
    local tok
    for tok in $to_install; do
      case "$tok" in
        PyQt5|PyOpenGL|PyOpenGL_accelerate|av|numpy|jeepney|pyperclip|psutil|cryptography|pynput|evdev)
          pkgs+=("$tok") ;;
        *)
          warn "Ignoring unexpected package token from the dependency probe: '$tok'" ;;
      esac
    done
    [ "${#pkgs[@]}" -eq 0 ] && bail "No valid Python packages to install."

    echo
    msg "Installing missing Python packages into .venv:"
    echo "  ${pkgs[*]}"
    python -m pip install -U pip wheel setuptools
    python -m pip install --no-input "${pkgs[@]}" || {
      err "pip install failed for: ${pkgs[*]}"
      warn "If 'av' failed to build, ensure FFmpeg dev libraries are installed (see README)."
      exit 1
    }
  else
    msg "All required Python packages are available in .venv."
  fi
}

bootstrap() {
  banner
  check_system_deps || bail "System dependency check failed."
  ensure_venv
  ensure_pydeps
}

# ── autostart: run the host at login ─────────────────────────────────────────
# The entry is an XDG autostart .desktop file, so it runs inside the desktop
# session (portal capture needs the session bus; x11grab/kmsgrab need the
# display). Every one of these subcommands stays off bootstrap(): enabling an
# autostart entry must not try to install system packages.

AUTOSTART_NAME="linuxplay-host.desktop"

autostart_target() {
  printf '%s' "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$AUTOSTART_NAME"
}

# Desktop Entry Exec values are not shell words: quote only when needed.
exec_field() {
  case "$1" in
    *[[:space:]]*) printf '"%s"' "$1" ;;
    *)             printf '%s' "$1" ;;
  esac
}

autostart_enable() {
  local headless=0
  for a in "$@"; do
    case "$a" in
      --headless) headless=1 ;;
      *) err "autostart enable: unknown option '$a'"; return 2 ;;
    esac
  done

  local py="$SCRIPT_DIR/.venv/bin/python"
  local runner="$SCRIPT_DIR/autostart_host.py"
  local target; target="$(autostart_target)"
  local extra=""
  if [ "$headless" -eq 1 ]; then extra=" --headless"; fi

  if [ ! -x "$py" ]; then
    warn "No Python at $py — run ./run.sh check first, or the entry cannot start the host."
  fi

  mkdir -p "$(dirname "$target")" || bail "Could not create $(dirname "$target")"
  cat > "$target" <<EOF
[Desktop Entry]
Type=Application
Name=LinuxPlay Host
Comment=Start the LinuxPlay host at login with the launcher's saved Host settings
Exec=$(exec_field "$py") $(exec_field "$runner")$extra
Path=$SCRIPT_DIR
Terminal=false
StartupNotify=false
Hidden=false
X-GNOME-Autostart-enabled=true
X-KDE-autostart-after=panel
EOF
  chmod 0644 "$target"

  msg "Autostart entry written: $target"
  echo "  $(grep -m1 '^Exec=' "$target" || true)"
  echo "  Settings come from ~/.linuxplay_start_cfg.json (the launcher's Host tab)."
  echo
  echo "Test it without logging out:"
  echo "  $(exec_field "$py") $(exec_field "$runner")$extra"
  echo
  echo "One-time portal grant: connect a client once and tick 'remember' in the"
  echo "screen-share dialog. After that the host captures prompt-free at login,"
  echo "which is what makes it usable while you are away from the machine."
  echo
  echo "Turn it off again in System Settings > Autostart, or: ./run.sh autostart disable"
}

autostart_disable() {
  local target; target="$(autostart_target)"
  if [ -f "$target" ]; then
    rm -f "$target" && msg "Removed $target"
    echo "Note: KDE may keep its own copy of the entry's enabled/disabled state;"
    echo "      System Settings > Autostart should no longer list it."
  else
    warn "Nothing to remove (no $target)"
  fi
}

autostart_status() {
  local target; target="$(autostart_target)"

  section "Autostart entry"
  if [ ! -f "$target" ]; then
    warn "Not installed — run: ./run.sh autostart enable"
  elif grep -qi '^[[:space:]]*Hidden=true' "$target"; then
    warn "Installed but disabled (Hidden=true): $target"
    warn "  Re-enable it in System Settings > Autostart, or run: ./run.sh autostart enable"
  else
    msg "Installed: $target"
    grep -m1 '^Exec=' "$target" | sed 's/^/  /' || true
  fi

  section "Host state"
  local py="$SCRIPT_DIR/.venv/bin/python"
  if [ -x "$py" ]; then
    "$py" "$SCRIPT_DIR/autostart_host.py" --status || true
  else
    warn "No Python at $py — run ./run.sh check"
  fi
}

autostart_stop() {
  local py="$SCRIPT_DIR/.venv/bin/python"
  [ -x "$py" ] || bail "No Python at $py (run ./run.sh check)."
  "$py" "$SCRIPT_DIR/autostart_host.py" --stop
}

run_mode() {
  local mode="${1:-}"; shift || true

  case "$mode" in
    check)
      bootstrap
      echo
      msg "Environment ready."
      echo "You can now run:"
      echo "  ./run.sh start"
      echo "  ./run.sh host --gui ..."
      echo "  ./run.sh client --host_ip 1.2.3.4 ..."
      ;;
    start)
      bootstrap
      exec python3 start.py "$@"
      ;;
    host)
      bootstrap
      exec python3 host.py "$@"
      ;;
    client)
      bootstrap
      exec python3 client.py "$@"
      ;;
    test)
      # Runs the standalone test_*.py scripts through the runner; a bare
      # `./run.sh test` runs all of them.
      if [ ! -x "$SCRIPT_DIR/.venv/bin/python" ]; then
        bootstrap
      fi
      exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/test_all.py" "$@"
      ;;
    autostart)
      local cmd="${1:-status}"; shift || true
      case "$cmd" in
        enable)  autostart_enable "$@" ;;
        disable) autostart_disable ;;
        status)  autostart_status ;;
        stop)    autostart_stop ;;
        ""|-h|--help|help) usage ;;
        *)
          err "Unknown autostart command: $cmd"
          usage
          exit 1
          ;;
      esac
      ;;
    ""|-h|--help|help)
      usage
      ;;
    *)
      err "Unknown mode: $mode"
      usage
      exit 1
      ;;
  esac
}

run_mode "$@"
