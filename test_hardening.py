#!/usr/bin/env python3
"""Hardening tests: PIN lockout, trust-DB robustness, upload token gate,
non-fatal channel errors, stream restart policy, pairing approval,
tunnel classification and the host log file."""
import logging
import os
import socket
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host    # noqa: E402
import client  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
HOST_SRC = open(os.path.join(HERE, "host.py"), encoding="utf-8").read()
CLIENT_SRC = open(os.path.join(HERE, "client.py"), encoding="utf-8").read()
START_SRC = open(os.path.join(HERE, "start.py"), encoding="utf-8").read()

PASS = []
def ok(name):
    PASS.append(name)
    print(f"  PASS: {name}")


# ── 1. PIN brute force: per-address cool-off, cleared on success ──
host.host_state.pin_failures.clear()
ip = "192.0.2.77"
assert host._pin_lockout_remaining(ip) == 0.0
host._pin_note_failure(ip)
host._pin_note_failure(ip)
assert host._pin_lockout_remaining(ip) == 0.0, "no cool-off before the threshold"
host._pin_note_failure(ip)
first = host._pin_lockout_remaining(ip)
assert first > 0, "the third bad PIN must lock the address out"
host._pin_note_failure(ip)
assert host._pin_lockout_remaining(ip) > first, "repeat offenders wait longer"
host._pin_clear_failures(ip)
assert host._pin_lockout_remaining(ip) == 0.0
assert "hmac.compare_digest(provided_pin, expected)" in HOST_SRC
assert "provided_pin == str(host_state.pin_code)" not in HOST_SRC
fail_branch = HOST_SRC.split("attempted reuse of consumed PIN")[1].split("def ")[0]
assert "_pin_note_failure(peer_ip)" in fail_branch
assert "pin_rotate" not in fail_branch, "a bad PIN must not force a rotation"
ok("bad PINs earn a growing lockout; no rotation, constant-time compare")


# ── 2. a malformed trust DB fails closed instead of raising ──
tmp = tempfile.mkdtemp(prefix="lp-hardening-")
cwd = os.getcwd()
os.chdir(tmp)
try:
    for bad in ("[]", "null", '{"trusted_clients": null}',
                '{"trusted_clients": [1, 2]}', '{"trusted_clients": "nope"}', "{"):
        with open(host.TRUSTED_DB, "w") as f:
            f.write(bad)
        db = host._load_trust_db()
        assert db == {"trusted_clients": []}, (bad, db)
        assert host._trust_record_for("AB", db) is None
        assert host._verify_fingerprint_trusted("AB") is False
    with open(host.TRUSTED_DB, "w") as f:
        f.write('{"trusted_clients": [{"fingerprint": "AB", "status": "trusted"}]}')
    assert host._verify_fingerprint_trusted("AB") is True
    with open(host.TRUSTED_DB, "w") as f:
        f.write('{"trusted_clients": [{"fingerprint": "AB", "status": "revoked"}]}')
    assert host._verify_fingerprint_trusted("AB") is False
finally:
    os.chdir(cwd)
assert "trigger_shutdown" not in HOST_SRC.split("def _handle_handshake_conn")[1].split("def tcp_handshake_server")[0], \
    "a bad handshake packet must not stop the host"
ok("malformed trust DB fails closed; bad handshakes cannot kill the host")


# ── 3. upload AUTH header: bounded read ──
a, b = socket.socketpair()
try:
    b.sendall(b"AUTH deadbeef\n\x00\x01")
    assert host._read_line(a) == "AUTH deadbeef"
    assert a.recv(2) == b"\x00\x01", "bytes after the line must stay for the header parser"
    b.sendall(b"x" * 200)
    assert host._read_line(a, limit=16) is None, "an overlong line must be rejected"
    b.close()
    assert host._read_line(a) is None, "EOF must not raise"
finally:
    a.close()
    b.close()
ok("upload AUTH header read is bounded and EOF-safe")


# ── 4. upload + control token gate fails closed ──
host.host_state.session_active = True
host.host_state.session_token = "tok-abc"
assert host._token_ok("tok-abc") is True
assert host._token_ok("tok-abd") is False
assert host._token_ok("") is False
assert host._token_ok(None) is False
host.host_state.session_token = None
assert host._token_ok("tok-abc") is False, "no live token ⇒ nothing authenticates"
host.host_state.session_active = False

upload_src = HOST_SRC.split("def file_upload_listener")[1].split("def heartbeat_manager")[0]
assert "not host_state.session_active or peer_ip != host_state.authed_client_ip" in upload_src, \
    "the upload listener must still pin the session and the authed client IP"
assert "_token_ok(token)" in upload_src, "the upload listener must verify the session token"
assert 'AUTH {token}\\n' in CLIENT_SRC, "the client must send the token with uploads"
ok("upload channel requires the session token (both sides)")


# ── 5. channel errors are logged and retried, not fatal ──
for needle in ("Clipboard send error", "Clipboard apply error",
               "Clipboard listener error", "Control listener error",
               "Handshake server error"):
    assert f"trigger_shutdown(f\"{needle}" not in HOST_SRC, f"{needle} must not stop the host"
assert "except socket.timeout as e:" in HOST_SRC, "upload listener must survive a timeout"
assert "listener continues" in HOST_SRC
assert "Streams did not start" in HOST_SRC, "stream start needs a backoff path"
assert "start_streams_for_current_client(args)\n            if host_state.video_threads" in HOST_SRC
ok("channel and start-up failures log/retry instead of stopping the host")


# ── 6. recurring warnings are throttled ──
records = []
class _Capture(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())
cap = _Capture()
logging.getLogger().addHandler(cap)
try:
    host._warn_throttle.clear()
    host._warn_throttled("t1", "first", interval=60)
    host._warn_throttled("t1", "second", interval=60)
    host._warn_throttled("t2", "other", interval=60)
finally:
    logging.getLogger().removeHandler(cap)
assert [m for m in records if m in ("first", "second", "other")] == ["first", "other"], records
ok("recurring channel errors are throttled instead of flooding the log")


# ── 7. a crashing encoder retries, then retires — the host stays up ──
max_restarts, max_delay = host.STREAM_MAX_RESTARTS, host.STREAM_RESTART_MAX_DELAY
host.STREAM_MAX_RESTARTS, host.STREAM_RESTART_MAX_DELAY = 1, 0.1
host.host_state.should_terminate = False
t = host.StreamThread(["/bin/false"], "Video test")
host.host_state.video_threads[7] = t
try:
    t.start()
    t.join(timeout=15)
    assert not t.is_alive(), "the supervisor must stop after its retry budget"
    assert 7 not in host.host_state.video_threads, \
        "a retired stream is dropped so the session manager can retry it"
    assert host.host_state.should_terminate is False, "a crashing encoder must NOT stop the host"
finally:
    host.STREAM_MAX_RESTARTS, host.STREAM_RESTART_MAX_DELAY = max_restarts, max_delay
    host.host_state.video_threads.pop(7, None)
    host.host_state.should_terminate = False
ok("crashing encoder retries with backoff, then retires — host stays up")


# ── 8. pairing approval: GUI asks, headless auto-approves ──
host.host_state.gui_window = None
assert host.request_pair_approval("192.0.2.9") is True, "headless hosts keep auto-pairing"
host.host_state.gui_window = object()          # a GUI is up, nobody answers
timeout, host.PAIR_APPROVAL_TIMEOUT = host.PAIR_APPROVAL_TIMEOUT, 0.3
try:
    assert host.request_pair_approval("192.0.2.9") is False, "an unanswered prompt must deny"
finally:
    host.PAIR_APPROVAL_TIMEOUT = timeout
    host.host_state.gui_window = None
assert "log_emitter.pair_request.connect(self._on_pair_request)" in HOST_SRC
assert "QMessageBox.question" in HOST_SRC
ok("a new device needs local approval when a GUI is up; headless auto-approves")


# ── 9. Tailscale / CGNAT peers are not treated as LAN ──
assert client._ip_in_cgnat("100.76.206.96") is True
assert client._ip_in_cgnat("100.63.0.1") is False
assert client._ip_in_cgnat("100.128.0.1") is False
assert client._ip_in_cgnat("192.168.0.39") is False
assert client._ip_in_cgnat("not-an-ip") is False
for iface in ("tailscale0", "wg0", "tun0", "utun4", "tap0"):
    assert client._is_tunnel_iface(iface) is True, iface
for iface in ("wlan0", "eth0", "enp5s0", ""):
    assert client._is_tunnel_iface(iface) is False, iface
assert client.detect_network_mode("100.76.206.96") == "vpn"
assert '"vpn"' in CLIENT_SRC
assert 'mode in ("wifi", "lan", "vpn")' in HOST_SRC, "the host must accept NET vpn"
assert 'net_mode in ("wifi", "vpn")' in HOST_SRC, "vpn uses the cautious buffer profile"
assert '"vpn"' in START_SRC
ok("Tailscale/CGNAT links are 'vpn': no ultra-low-latency profile, cautious buffers")


# ── 10. video input: MTU-derived pkt size and a read timeout ──
assert client._best_ts_pkt_size(1280, False) == 1128
assert client._best_ts_pkt_size(1500, False) == 1316
assert client._best_ts_pkt_size(1500, True) == 1316
assert client._best_ts_pkt_size(0, False) == 1316
assert client._best_ts_pkt_size(1360, False) == 1316
assert client._best_ts_pkt_size(1360, True) == 1128, "IPv6 overhead must be accounted for"
assert "rw_timeout=5000000" in CLIENT_SRC, "a blackholed stream must fail, not freeze"
assert "_video_url_for_path" in CLIENT_SRC
assert "self.video_url = self._video_url_for_path()" in CLIENT_SRC, \
    "the URL is recomputed on every decoder (re)start"
assert "def _route_mtu" in CLIENT_SRC
ok("video input re-probes the path MTU and cannot freeze forever")


# ── 11. the host always leaves a log, and the launcher keeps child output ──
assert "_setup_file_logging" in HOST_SRC and "RotatingFileHandler" in HOST_SRC
assert "host_state.log_path = _setup_file_logging(args.debug)" in HOST_SRC
assert 'host-launch.log' in START_SRC and 'client-launch.log' in START_SRC
assert "stdout=log_file or subprocess.DEVNULL" in START_SRC
assert "stderr=subprocess.DEVNULL,\n                env=env" not in START_SRC, \
    "the host's stderr must no longer be discarded"
assert "_tail_text" in START_SRC, "failures must be surfaced to the user"
ok("host log file + launcher capture stderr and report failures")


# ── 12. live upload: AUTH is required, and one bad connection cannot kill it ──
import shutil
import threading

drop_home = tempfile.mkdtemp(prefix="lp-drop-")
old_home = os.environ.get("HOME")
prev_active, prev_ip, prev_token = (host.host_state.session_active,
                                    host.host_state.authed_client_ip,
                                    host.host_state.session_token)
prev_term = host.host_state.should_terminate
os.environ["HOME"] = drop_home
host.host_state.should_terminate = False
host.host_state.session_active = True
host.host_state.authed_client_ip = "127.0.0.1"
host.host_state.session_token = "tok-e2e"
listener = threading.Thread(target=host.file_upload_listener, daemon=True)
listener.start()
for _ in range(40):                       # wait for the listener to bind 7003
    if host.host_state.file_upload_sock is not None:
        break
    time.sleep(0.05)
assert host.host_state.file_upload_sock is not None, "upload listener did not bind 7003"

def _send_upload(payload_name, payload, token="tok-e2e", send_auth=True):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(("127.0.0.1", host.FILE_UPLOAD_PORT))
    if send_auth:
        s.sendall(f"AUTH {token}\n".encode("utf-8") if token else b"\n")
    name = payload_name.encode("utf-8")
    s.sendall(len(name).to_bytes(4, "big") + name + len(payload).to_bytes(8, "big"))
    s.sendall(payload)
    s.close()

try:
    _send_upload("good.txt", b"hello-drop")
    dest = os.path.join(drop_home, "LinuxPlayDrop", "good.txt")
    for _ in range(40):
        if os.path.exists(dest):
            break
        time.sleep(0.05)
    assert os.path.exists(dest), "a correctly authenticated upload must be stored"
    assert open(dest, "rb").read() == b"hello-drop", "stored bytes must match"
    assert oct(os.stat(dest).st_mode & 0o777) == "0o600", "drop files are 0600"
    assert oct(os.stat(os.path.dirname(dest)).st_mode & 0o777) == "0o700", \
        "the drop directory is 0700"

    _send_upload("evil.txt", b"nope", token="wrong-token")
    _send_upload("noauth.txt", b"nope", send_auth=False)
    # a connection that dies mid-header must not stop the listener
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(("127.0.0.1", host.FILE_UPLOAD_PORT))
    s.sendall(b"AUTH tok-e2e\n")
    s.close()
    time.sleep(0.5)
    assert not os.path.exists(os.path.join(drop_home, "LinuxPlayDrop", "evil.txt"))
    assert not os.path.exists(os.path.join(drop_home, "LinuxPlayDrop", "noauth.txt"))
    _send_upload("after.txt", b"still-alive")     # listener survived all of it
    dest2 = os.path.join(drop_home, "LinuxPlayDrop", "after.txt")
    for _ in range(40):
        if os.path.exists(dest2):
            break
        time.sleep(0.05)
    assert os.path.exists(dest2), "the listener must still accept uploads afterwards"
finally:
    host.host_state.should_terminate = True
    try:
        if host.host_state.file_upload_sock is not None:
            host.host_state.file_upload_sock.close()
    except Exception:
        pass
    listener.join(timeout=3)
    host.host_state.should_terminate = prev_term
    host.host_state.session_active = prev_active
    host.host_state.authed_client_ip = prev_ip
    host.host_state.session_token = prev_token
    host.host_state.file_upload_sock = None
    if old_home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = old_home
    shutil.rmtree(drop_home, ignore_errors=True)
ok("live upload: token required, traversal-free, aborted uploads cannot kill it")


# ── 13. an approval prompt that is answered actually approves ──
answered = {}
def _approve(req):
    answered["ip"] = req.peer_ip
    req.approved = True
    req.event.set()
try:
    host.log_emitter.pair_request.connect(_approve)
    host.host_state.gui_window = object()
    assert host.request_pair_approval("192.0.2.10") is True, \
        "an answered 'yes' must approve the pairing"
    assert answered.get("ip") == "192.0.2.10"
finally:
    try:
        host.log_emitter.pair_request.disconnect(_approve)
    except Exception:
        pass
    host.host_state.gui_window = None
ok("approving the pairing prompt issues the approval (not just the timeout path)")


# ── 14. a new session/disconnect clears the stream backoff ──
assert "host_state.session_epoch += 1" in HOST_SRC
assert HOST_SRC.count("host_state.session_epoch += 1") >= 3, \
    "connect, GOODBYE and heartbeat-timeout must all bump the epoch"
assert "if host_state.session_epoch != seen_epoch:" in HOST_SRC, \
    "the session manager must reset its failure backoff on a new session"
ok("session changes reset the stream-restart backoff")


print(f"\nALL {len(PASS)} HARDENING TESTS PASSED")
