#!/usr/bin/env python3
"""Autostart tests: the settings->command mapping shared by the launcher and the
login runner, the single-instance lock, and the generated desktop entry.

Nothing here needs a display or a live host; the lock tests use a child process
that holds the lock the way host.py does.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hostcmd      # noqa: E402
import hostlock     # noqa: E402
import autostart_host  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, ".venv", "bin", "python")
HOST_SRC = open(os.path.join(HERE, "host.py"), encoding="utf-8").read()
START_SRC = open(os.path.join(HERE, "start.py"), encoding="utf-8").read()

PASS = []


def ok(name):
    PASS.append(name)
    print(f"  PASS: {name}")


def value_of(argv, flag):
    assert flag in argv, f"{flag} missing from {argv}"
    return argv[argv.index(flag) + 1]


def build(cfg, gui=True):
    return hostcmd.build_host_argv("/python", "/repo/host.py", cfg, gui=gui)


# ── 1. saved launcher settings become the host's command line ──
saved = {
    "profile": "Balanced", "encoder": "h.264", "hwenc": "vaapi", "framerate": "60",
    "resolution": "1920x1080", "bitrate": "8M", "audio": "enable",
    "audio_mode": "Voice (low-latency)", "adaptive": True, "display": ":1",
    "preset": "fast", "gop": "15", "qp": "23", "tune": "ll", "pix_fmt": "yuv444p",
    "capture": "portal",
}
argv = build(saved)
assert argv[0] == "/python" and argv[1] == "/repo/host.py", argv
assert argv[2] == "--gui", "the login autostart runs the windowed host"
assert value_of(argv, "--encoder") == "h.264"
assert value_of(argv, "--hwenc") == "vaapi"
assert value_of(argv, "--framerate") == "60"
assert value_of(argv, "--resolution") == "1920x1080"
assert value_of(argv, "--bitrate") == "8M"
assert value_of(argv, "--audio") == "enable"
assert value_of(argv, "--pix_fmt") == "yuv444p"
assert value_of(argv, "--display") == ":1"
assert value_of(argv, "--preset") == "fast"
assert value_of(argv, "--gop") == "15"
assert value_of(argv, "--qp") == "23"
assert value_of(argv, "--tune") == "ll"
assert "--adaptive" in argv
ok("saved settings map onto the host command line")

# headless is a launcher choice, not a settings one
assert "--gui" not in build(saved, gui=False)
ok("--gui is dropped for a headless host")

# ── 2. combo placeholders mean "let the host decide" ──
argv = build({"resolution": "Native (desktop)", "preset": "Default", "qp": "None",
              "tune": "None", "gop": "Auto", "adaptive": False, "encoder": "h.265",
              "audio": "disable"})
assert value_of(argv, "--resolution") == "native", "'Native (desktop)' is a label, not a size"
assert "--preset" not in argv and "--qp" not in argv and "--tune" not in argv
assert "--adaptive" not in argv
assert "--gop" not in argv, "gop 'Auto' with a normal preset adds no --gop"
assert value_of(argv, "--audio") == "disable"

argv = build({"preset": "ultra-low-latency", "gop": "Auto"})
assert value_of(argv, "--gop") == "1", "a zero-latency preset means a 1-frame GOP"
ok("placeholder values are translated the way the launcher translates them")

# ── 3. defaults, and values hostile to argparse never reach the host ──
argv = build({})
assert value_of(argv, "--encoder") == "h.264", "README's default encoder"
assert value_of(argv, "--resolution") == "native"
assert value_of(argv, "--audio") == "enable", "the launcher's Default profile has audio on"
assert value_of(argv, "--hwenc") == "auto"
assert value_of(argv, "--display") == ":0"
assert "--gop" in argv and value_of(argv, "--gop") == "30"

argv = build({"encoder": "none", "hwenc": "amf", "audio": "banana",
              "resolution": "wat", "framerate": "", "pix_fmt": None})
assert value_of(argv, "--encoder") == "h.264", "'none' cannot host: fall back, do not fail"
assert value_of(argv, "--hwenc") == "auto", "'amf' is not a host --hwenc choice"
assert value_of(argv, "--audio") == "enable", "an unknown audio value means the default"
assert value_of(argv, "--resolution") == "native", "an unparsable size streams native"
assert value_of(argv, "--framerate") == "30" and value_of(argv, "--pix_fmt") == "yuv420p"
ok("missing and invalid settings fall back instead of reaching argparse")

# every generated flag is one host.py actually accepts
host_choices = {"--encoder": {"none", "h.264", "h.265"},
                "--hwenc": {"auto", "cpu", "nvenc", "qsv", "vaapi"},
                "--audio": {"enable", "disable"},
                "--pix_fmt": set(),      # free-form; do not check
                "--resolution": set()}
for flag, allowed in host_choices.items():
    if allowed:
        assert value_of(argv, flag) in allowed
ok("generated values respect host.py's argparse choices")

# ── 3b. host.py itself accepts every line we generate ──
import host  # noqa: E402  (imported late: module-level Qt objects, no app needed)

_real_argv = sys.argv
try:
    for cfg in (saved, {}, {"encoder": "none", "hwenc": "amf", "audio": "banana",
                            "resolution": "wat", "gop": "Auto", "qp": "None"},
                {"preset": "ultra-low-latency", "gop": "Auto", "resolution": "1280x720"},
                {"debug": True, "adaptive": True, "audio_mode": "Music (quality)"}):
        generated = build(cfg)
        sys.argv = ["host.py"] + generated[2:]      # drop python + host.py
        parsed = host.parse_args()                  # SystemExit on any bad value
        assert parsed.encoder in ("h.264", "h.265"), parsed
        assert parsed.audio in ("enable", "disable")
        assert parsed.hwenc in ("auto", "cpu", "nvenc", "qsv", "vaapi")
finally:
    sys.argv = _real_argv
ok("the generated command line parses through host.py's own argparse")


env = hostcmd.build_host_env(saved, base_env={}, sid="SID-1")
assert env["LINUXPLAY_MARKER"] == "LinuxPlayHost"
assert env["LINUXPLAY_SID"] == "SID-1"
assert env["LINUXPLAY_CAPTURE"] == "portal"
assert (env["LP_OPUS_APP"], env["LP_OPUS_FD"]) == ("voip", "10")
assert "LINUXPLAY_KMS_DEVICE" not in env

env = hostcmd.build_host_env({"audio_mode": "Music (quality)", "capture": "nope",
                              "kms_device": "/dev/dri/card1"}, base_env={})
assert (env["LP_OPUS_APP"], env["LP_OPUS_FD"]) == ("audio", "20")
assert env["LINUXPLAY_CAPTURE"] == "auto", "an unknown capture mode means auto"
assert env["LINUXPLAY_KMS_DEVICE"] == "/dev/dri/card1"
assert env["LINUXPLAY_SID"], "a run without one gets a fresh session id"

inherited = hostcmd.build_host_env({}, base_env={"LINUXPLAY_SID": "FROM-LAUNCHER"})
assert inherited["LINUXPLAY_SID"] == "FROM-LAUNCHER"
ok("host environment carries the marker, session id, audio mode and capture mode")

# ── 5. one host per user: the lock ──
state = tempfile.mkdtemp(prefix="lp-lock-")
os.environ["LINUXPLAY_STATE_DIR"] = state
try:
    assert hostlock.probe() is None, "no host yet"
    fd = hostlock.try_acquire()
    assert fd is not None, "the first host takes the lock"
    info = hostlock.probe()
    assert info and int(info["pid"]) == os.getpid(), info
    assert hostlock.try_acquire() is None, "a second host must not start"
    assert "PID" in hostlock.format_holder(info)
    hostlock.release(fd)
    assert hostlock.probe() is None, "releasing the lock frees the slot"
    ok("the instance lock admits one host and reports its PID")

    # A second *process*: the launcher's takeover needs its PID from the lock.
    child_script = os.path.join(state, "host.py")   # so _looks_like_host() accepts it
    with open(child_script, "w") as f:
        f.write("import sys, time\n"
                f"sys.path.insert(0, {HERE!r})\n"
                "import hostlock\n"
                "fd = hostlock.try_acquire()\n"
                "print('acquired' if fd is not None else 'busy', flush=True)\n"
                "time.sleep(60)\n")
    child = subprocess.Popen([PY, child_script], env=dict(os.environ),
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "acquired"
        info = hostlock.probe()
        assert info and int(info["pid"]) == child.pid, (info, child.pid)
        started = time.time()
        assert hostlock.shutdown_host([info["pid"]], released=hostlock.released_predicate(info),
                                      timeout=5.0) is True, "SIGTERM must free the lock"
        assert time.time() - started < 10, "stopping must not wait for the timeout"
        assert hostlock.probe() is None
    finally:
        child.kill()
        child.wait()
    ok("a running host is found, stopped and waited for")

    # A recycled PID must not be signalled: this test process is not a host.
    # (released stays False on purpose, so a refusal is what ends the call.)
    assert hostlock._looks_like_host(os.getpid()) is False
    assert hostlock.shutdown_host([os.getpid()], released=lambda: False, timeout=0.1) is False, \
        "refuse to kill something that is not host.py"
    os.kill(os.getpid(), 0)          # and we are still here
    ok("stopping refuses a PID that is not a LinuxPlay host")

    # ── 5b. a host that predates the lock is found by its port instead ──
    # Uses a private port so a live host on the real one cannot confuse the test.
    real_port = hostlock.HANDSHAKE_PORT
    hostlock.HANDSHAKE_PORT = 47001
    try:
        assert not hostlock.port_busy(), "the private test port starts free"
        assert hostlock.running_host() == (None, []), "nothing running, nothing to take over"

        holder = subprocess.Popen(
            [PY, "-c", "import socket,time\n"
                       "s = socket.socket(); s.bind(('', 47001)); s.listen(1)\n"
                       "time.sleep(30)\n"])
        try:
            time.sleep(0.5)
            assert hostlock.port_busy(), "a listener on the handshake port must be noticed"
            assert hostlock.probe() is None, "it holds no lock — that is the point"
            info, pids = hostlock.running_host()
            assert info and info.get("legacy"), info
            assert "TCP" in hostlock.format_holder(info)
            assert all(hostlock._looks_like_host(p) for p in pids), \
                "only real host processes may be offered for takeover"
            # The wait-for-gone check must watch the port here, not the lock.
            assert hostlock.released_predicate(info)() is False, "it is still listening"
        finally:
            holder.kill()
            holder.wait()
        time.sleep(0.3)
        assert not hostlock.port_busy()
        assert hostlock.released_predicate(info)() is True, "gone once the port unbinds"
        ok("the port probe catches a host that holds no instance lock")
    finally:
        hostlock.HANDSHAKE_PORT = real_port

    # ── 6. host.py wires the lock in before it can bind ports or show a window ──
    assert "_acquire_instance_lock(args)" in HOST_SRC
    assert HOST_SRC.index("_acquire_instance_lock(args)") < HOST_SRC.index("w = HostWindow(args)"), \
        "the duplicate-host check must run before the window exists"
    assert "return 3" in HOST_SRC
    assert "signal.signal(signal.SIGTERM, _gui_signal_handler)" in HOST_SRC, \
        "logout/takeover must reach the GUI through the Qt-safe handler"
    assert "self._core_running()" in HOST_SRC, \
        "the window must wait for stop_all() before quitting"
    assert "hostcmd.build_host_argv" in START_SRC and "hostlock.running_host()" in START_SRC, \
        "the launcher must build the same command line and check for a running host"
    ok("host.py and start.py are wired to the lock and the shared builder")
finally:
    os.environ.pop("LINUXPLAY_STATE_DIR", None)

# ── 7. the generated autostart entry ──
home = tempfile.mkdtemp(prefix="lp-home-")
env = dict(os.environ)
env["HOME"] = home
env.pop("XDG_CONFIG_HOME", None)
env.pop("LINUXPLAY_STATE_DIR", None)
entry = os.path.join(home, ".config", "autostart", "linuxplay-host.desktop")


def run(*args):
    return subprocess.run([os.path.join(HERE, "run.sh"), *args], cwd=HERE, env=env,
                          capture_output=True, text=True, timeout=120)


r = run("autostart", "enable")
assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
assert os.path.exists(entry), r.stdout
text = open(entry).read()
assert "[Desktop Entry]" in text and "Type=Application" in text
assert "Path=" + HERE in text, text
exec_line = [l for l in text.splitlines() if l.startswith("Exec=")][0][5:]
parts = exec_line.split()
assert parts[0] == PY, exec_line
assert parts[1] == os.path.join(HERE, "autostart_host.py"), exec_line
assert "--headless" not in exec_line, "the entry shows the host window by default"
assert "X-GNOME-Autostart-enabled=true" in text
ok("autostart enable writes a session entry that runs the repo's venv python")

r = run("autostart", "enable", "--headless")
assert r.returncode == 0 and "--headless" in open(entry).read()

r = run("autostart", "status")
assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
assert "Installed" in r.stdout, r.stdout
# Either nothing runs, or (on this very machine, a host can be live) the status
# names what is holding the port — but it must never claim nothing is there.
assert ("host running:   no" in r.stdout) or ("listening on TCP" in r.stdout), r.stdout
assert "portal capture" in r.stdout, r.stdout
ok("autostart status reports the entry, the host and the portal token")

r = run("autostart", "disable")
assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
assert not os.path.exists(entry), "disable removes the entry"
ok("autostart disable removes the entry")


print(f"\nALL {len(PASS)} AUTOSTART TESTS PASSED")
