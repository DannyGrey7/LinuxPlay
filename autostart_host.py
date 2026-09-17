#!/usr/bin/env python3
"""Start the host the way the GUI launcher would, from the desktop session.

Called by the autostart entry (~/.config/autostart/linuxplay-host.desktop, see
`./run.sh autostart enable`). Everything here exists to remove the ways a
session-start launch differs from pressing "Start Host":

  * the working directory is the repo — host_ca.pem / host_ca.key /
    trusted_clients.json are plain relative paths in host.py, so starting
    elsewhere would mint a new CA and forget every paired device;
  * the interpreter is the repo's .venv python (host.py imports PyQt5 at module
    level, so a system python without PyQt5 cannot even start it);
  * the command line is rebuilt from ~/.linuxplay_start_cfg.json, the same file
    the launcher writes, so both start the same stream settings;
  * the desktop may still be coming up when autostart entries run, so we wait for
    the display/dbus before handing over to the host;
  * a host that is already running is left alone (one host per user, hostlock).

execv, not Popen: the window the user sees belongs to the host process itself,
so closing it stops the host exactly as in a launcher-started run.
"""
import os
import shlex
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hostcmd
import hostlock

try:
    from portal_capture import RESTORE_PATH as PORTAL_RESTORE_PATH
except Exception:                    # portal_capture needs jeepney; status must not
    PORTAL_RESTORE_PATH = os.path.join(
        os.path.expanduser("~"), ".config", "linuxplay", "portal_restore.json")

HERE = os.path.dirname(os.path.abspath(__file__))
HOST_PY = os.path.join(HERE, "host.py")
VENV_PY = os.path.join(HERE, ".venv", "bin", "python")
LOG_NAME = "autostart.log"
SESSION_WAIT_SECS = 20.0
LOG_MAX_BYTES = 256 * 1024


def _log_path():
    try:
        return os.path.join(hostlock.state_dir(), LOG_NAME)
    except Exception:
        return os.path.join(HERE, LOG_NAME)


def _log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [autostart] {msg}"
    print(line, file=sys.stderr)
    path = _log_path()
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _choose_python():
    """The venv python when it exists: host.py needs PyQt5 (and jeepney) from it."""
    if os.path.exists(VENV_PY) and os.access(VENV_PY, os.X_OK):
        return VENV_PY
    _log(f"WARNING: {VENV_PY} is missing — using {sys.executable}, which may lack PyQt5. "
         f"Run ./run.sh check to build the venv.")
    return sys.executable


def _display_ready():
    """Is the graphical session we were started into actually usable yet?"""
    wayland = (os.environ.get("WAYLAND_DISPLAY") or "").strip()
    if wayland:
        runtime = (os.environ.get("XDG_RUNTIME_DIR") or "").strip()
        if not runtime:
            return True                       # cannot verify; trust the env
        return os.path.exists(os.path.join(runtime, wayland))
    display = (os.environ.get("DISPLAY") or "").strip()
    if display:
        num = display.lstrip(":").split(".")[0]
        sock = f"/tmp/.X11-unix/X{num}"
        return os.path.exists(sock) or not num.isdigit()
    return False


def _dbus_ready():
    return bool((os.environ.get("DBUS_SESSION_BUS_ADDRESS") or "").strip())


def _wait_for_session(want_display, deadline_secs=SESSION_WAIT_SECS):
    """Block until the session looks usable. False if the display never appeared."""
    deadline = time.time() + deadline_secs
    waited = False
    while True:
        need = []
        if want_display and not _display_ready():
            need.append("display (WAYLAND_DISPLAY/DISPLAY)")
        if not _dbus_ready():
            need.append("session D-Bus")
        if not need:
            if waited:
                _log("Session is up; starting the host.")
            return True
        if time.time() >= deadline:
            _log(f"Timed out after {deadline_secs:.0f}s waiting for: {', '.join(need)}.")
            # A headless host needs neither the display nor a prompt-free bus.
            return not want_display
        if not waited:
            _log(f"Waiting up to {deadline_secs:.0f}s for: {', '.join(need)}.")
            waited = True
        time.sleep(0.25)


def _status():
    """Everything `run.sh autostart status` reports, gathered in one place."""
    info, pids = hostlock.running_host()
    if info:
        print(f"  host running:   {hostlock.format_holder(info)}")
        if info.get("legacy"):
            print("                  (started before the instance lock existed — "
                  "restart it once to register)")
    else:
        print("  host running:   no")

    cfg = hostcmd.load_host_cfg()
    if cfg:
        print(f"  saved settings: {hostcmd.CFG_PATH} ({len(cfg)} keys)")
    else:
        print(f"  saved settings: none yet — defaults will be used ({hostcmd.CFG_PATH})")

    if os.path.exists(PORTAL_RESTORE_PATH):
        print("  portal capture: restore token present (clients connect without a dialog)")
    else:
        print("  portal capture: NO restore token — the next client connect shows the share")
        print("                  dialog; tick 'remember' there to make it a one-time grant")

    argv, _ = build_launch(headless=False)
    print("  would launch:   " + " ".join(shlex.quote(a) for a in argv))
    print(f"  autostart log:  {_log_path()}")
    return 0


def _stop_running_host():
    info, pids = hostlock.running_host()
    if not info:
        print("No LinuxPlay host is running.")
        return 0
    if not pids:
        print(f"Something is holding TCP {hostlock.HANDSHAKE_PORT} but no LinuxPlay "
              f"host process was found — stop it by hand.", file=sys.stderr)
        return 1
    _log(f"Stopping the running host ({hostlock.format_holder(info)}, "
         f"PID {', '.join(str(p) for p in pids)}).")
    if hostlock.shutdown_host(pids, hostlock.released_predicate(info)):
        print(f"Stopped the LinuxPlay host ({hostlock.format_holder(info)}).")
        return 0
    print(f"Could not stop the LinuxPlay host ({hostlock.format_holder(info)}). "
          f"See {_log_path()}.", file=sys.stderr)
    return 1


def build_launch(headless=False):
    """(argv, env) for host.py, from the launcher's saved settings."""
    cfg = hostcmd.load_host_cfg()
    python = _choose_python()
    argv = hostcmd.build_host_argv(python, HOST_PY, cfg, gui=not headless)
    env = hostcmd.build_host_env(cfg)
    return argv, env


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    headless = False
    print_cmd = False
    for arg in argv:
        if arg in ("-h", "--help"):
            print(__doc__.strip())
            print("\nUsage: autostart_host.py [--headless] [--print-cmd] [--status] [--stop]")
            return 0
        if arg == "--headless":
            headless = True
        elif arg == "--print-cmd":
            print_cmd = True
        elif arg == "--status":
            return _status()
        elif arg == "--stop":
            return _stop_running_host()
        else:
            print(f"autostart_host.py: unknown argument {arg!r}", file=sys.stderr)
            return 2

    try:
        os.chdir(HERE)
    except Exception as e:
        _log(f"Could not enter {HERE}: {e}")
        return 1

    launch_argv, env = build_launch(headless=headless)

    if print_cmd:
        print(" ".join(shlex.quote(a) for a in launch_argv))
        for key in ("LINUXPLAY_CAPTURE", "LINUXPLAY_MARKER", "LP_OPUS_APP", "LP_OPUS_FD"):
            print(f"{key}={env.get(key)}")
        return 0

    _log("Launching: " + " ".join(shlex.quote(a) for a in launch_argv))

    info, _pids = hostlock.running_host()
    if info:
        _log(f"A host is already running ({hostlock.format_holder(info)}) — not starting a second one.")
        return 0

    if not _wait_for_session(want_display=not headless):
        _log("No display appeared; not starting a GUI host.")
        return 1

    try:
        os.execve(launch_argv[0], launch_argv, env)
    except Exception as e:
        _log(f"Could not start the host: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
