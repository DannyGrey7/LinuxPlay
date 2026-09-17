#!/usr/bin/env python3
"""One host per user: an flock-based instance guard.

The host binds fixed ports, so a second copy used to die with "Address already
in use" from deep inside core_main. With the host now auto-starting at login,
that is a routine situation rather than a mistake — the launcher needs to know
a host is up (and which process to ask to stop) before it spawns its own.

The lock file also carries the holder's PID, which is what makes "stop the host
that is already running" possible without scanning /proc.

A host older than this module holds the port but no lock, so a port probe backs
up the lock: `probe()` for the normal case, `port_busy()` + `find_host_pids()`
for one started by an earlier version.
"""
import fcntl
import json
import logging
import os
import signal
import socket
import subprocess
import time

LOCK_NAME = "host.lock"
# host.py's TCP_HANDSHAKE_PORT: the one port a running host always holds.
HANDSHAKE_PORT = 7001


def state_dir():
    """Per-user state directory, matching host.py's _state_dir()."""
    base = os.environ.get("LINUXPLAY_STATE_DIR")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".local", "state", "linuxplay")
    os.makedirs(base, mode=0o700, exist_ok=True)
    return base


def lock_path():
    return os.path.join(state_dir(), LOCK_NAME)


def _open_lock_file():
    path = lock_path()
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return fd


def _read_info():
    try:
        with open(lock_path(), "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def try_acquire():
    """Take the instance lock; returns the fd to hold, or None if a host is up.

    Keep the returned fd open for the process lifetime — closing it (or dying)
    releases the lock.
    """
    fd = _open_lock_file()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    try:
        info = {"pid": os.getpid(), "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                "started_ts": time.time()}
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps(info).encode("utf-8"))
        os.fsync(fd)
    except Exception as e:
        logging.debug("Could not record instance-lock holder: %s", e)
    return fd


def release(fd):
    """Drop the lock early (the process exiting after this would too)."""
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass


def probe():
    """Info about the running host, or None when the lock is free.

    Never writes to the file: taking and releasing the lock here would overwrite
    the holder's PID with ours.
    """
    try:
        fd = _open_lock_file()
    except Exception:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        info = _read_info()
        info.setdefault("pid", None)
        info.setdefault("started", "")
        return info
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass
    return None


def _looks_like_host(pid):
    """Best-effort guard against signalling a recycled PID."""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", "replace")
    except Exception:
        return True                      # unreadable: trust the lock file
    return "host.py" in cmdline or "autostart_host.py" in cmdline


def port_busy(timeout=0.4):
    """Is anything already listening on the handshake port?

    Catches a host too old to hold the lock (and any unrelated squatter) — the
    bind failure it would otherwise cause surfaces far from its cause.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.bind(("", HANDSHAKE_PORT))
        return False
    except OSError:
        return True
    finally:
        s.close()


def find_host_pids():
    """PIDs of other host processes, for hosts that hold no lock file.

    Only used to offer a takeover of a pre-lock host; the returned PIDs are
    still checked by _looks_like_host() before anything is signalled.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,args"], universal_newlines=True,
            stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, args = int(parts[0]), parts[1]
        if pid == os.getpid() or "python" not in args:
            continue
        if "host.py" in args or "autostart_host.py" in args:
            pids.append(pid)
    return pids


def shutdown_host(pids, released, timeout=8.0):
    """SIGTERM each pid, wait for released(), then SIGKILL. True if it went away."""
    targets = [int(p) for p in (pids or []) if p]
    allowed = []
    for pid in targets:
        if _looks_like_host(pid):
            allowed.append(pid)
        else:
            logging.warning("Refusing to stop PID %s: it is not a LinuxPlay host.", pid)

    if not allowed:
        return released()          # nothing of ours to signal

    for pid in allowed:
        try:
            os.kill(pid, signal.SIGTERM)
            logging.info("Asked the running host (PID %s) to stop.", pid)
        except ProcessLookupError:
            pass
        except Exception as e:
            logging.warning("Could not signal host PID %s: %s", pid, e)

    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if released():
            return True
        time.sleep(0.1)

    logging.warning("Host PID(s) %s ignored SIGTERM for %.0fs; killing them.",
                    ", ".join(str(p) for p in allowed), timeout)
    for pid in allowed:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if released():
            return True
        time.sleep(0.1)
    return released()


def released_predicate(info):
    """The "has it gone?" check matching what was found.

    A lock holder is gone when the lock frees; a pre-lock host is gone when the
    port unbinds. Waiting on the wrong one would time out and escalate to SIGKILL
    against a process that already stopped.
    """
    if info and info.get("legacy"):
        return lambda: not port_busy()
    return lambda: probe() is None


def running_host():
    """The running host, from the lock or (for a pre-lock host) the port.

    Returns (info, pids): info for messages — None when nothing is running —
    and the PIDs that could be stopped, which is empty when a hostile stranger
    holds the port rather than one of our hosts.
    """
    info = probe()
    if info:
        pid = info.get("pid")
        return info, ([int(pid)] if pid else [])
    if port_busy():
        return {"legacy": True}, find_host_pids()
    return None, []


def format_holder(info):
    """Human-readable one-liner for dialogs and `run.sh autostart status`."""
    if not info:
        return "no host running"
    if info.get("legacy"):
        return f"something is listening on TCP {HANDSHAKE_PORT} (no instance lock)"
    pid = info.get("pid")
    started = info.get("started") or ""
    ts = info.get("started_ts")
    age = ""
    try:
        if ts:
            mins = int(max(0.0, time.time() - float(ts)) // 60)
            age = f" ({mins} min ago)" if mins else " (just now)"
    except Exception:
        age = ""
    if pid and started:
        return f"PID {pid}, started {started}{age}"
    if pid:
        return f"PID {pid}"
    return "a host is running (lock held, no PID recorded)"
