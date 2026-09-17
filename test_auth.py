#!/usr/bin/env python3
"""Challenge-response certificate auth + session token tests (offline, tempdir)."""
import base64
import contextlib
import datetime
import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host    # noqa: E402
import client  # noqa: E402

PASS = []
def ok(name):
    PASS.append(name)
    print(f"  PASS: {name}")

assert host.HAVE_CRYPTO, "cryptography package required for these tests"
from cryptography import x509                                     # noqa: E402
from cryptography.x509.oid import NameOID                         # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization   # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa          # noqa: E402


def build_cert(signing_key, issuer_name, cn):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .issuer_name(issuer_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=30))
        .sign(signing_key, hashes.SHA256())
    )
    return key, cert


def write_pair(tmp, name, key, cert):
    cert_p = os.path.join(tmp, f"{name}_cert.pem")
    key_p = os.path.join(tmp, f"{name}_key.pem")
    with open(cert_p, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_p, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    return cert_p, key_p


with tempfile.TemporaryDirectory() as tmp:
    # Point the host's CA/trust files at the tempdir.
    host.CA_CERT = os.path.join(tmp, "host_ca.pem")
    host.CA_KEY = os.path.join(tmp, "host_ca.key")
    host.TRUSTED_DB = os.path.join(tmp, "trusted_clients.json")
    assert host._ensure_ca()
    assert os.stat(host.CA_KEY).st_mode & 0o777 == 0o600
    ok("CA private key written with 0600 permissions")

    with open(host.CA_KEY, "rb") as f:
        ca_key = serialization.load_pem_private_key(f.read(), password=None)
    with open(host.CA_CERT, "rb") as f:
        ca_cert = x509.load_pem_x509_certificate(f.read())

    # ── challenge-response round trip (client signs, host verifies) ──
    key, cert = build_cert(ca_key, ca_cert.subject, "test-client")
    cert_p, key_p = write_pair(tmp, "good", key, cert)
    fp = cert.fingerprint(hashes.SHA256()).hex().upper()
    nonce = secrets.token_bytes(32)

    proof = client._build_client_proof(cert_p, key_p, nonce.hex())
    assert proof, "client could not build proof"
    parts = proof.split()
    assert len(parts) == 4 and parts[0] == "CERT" and parts[2] == "SIG"
    assert host._verify_client_proof(fp, parts[1], parts[3], nonce)
    ok("valid cert + signature over nonce verifies")

    assert not host._verify_client_proof(fp, parts[1], parts[3], secrets.token_bytes(32))
    ok("signature over a different nonce is rejected")

    assert not host._verify_client_proof("AB" * 32, parts[1], parts[3], nonce)
    ok("fingerprint mismatch is rejected")

    # Self-signed cert: fingerprint matches itself, but not issued by the host CA.
    rogue_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rogue_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rogue-client")])
    rogue_key, rogue_cert = build_cert(rogue_key, rogue_name, "rogue-client")
    rogue_cert_p, rogue_key_p = write_pair(tmp, "rogue", rogue_key, rogue_cert)
    rogue_fp = rogue_cert.fingerprint(hashes.SHA256()).hex().upper()
    rparts = client._build_client_proof(rogue_cert_p, rogue_key_p, nonce.hex()).split()
    assert not host._verify_client_proof(rogue_fp, rparts[1], rparts[3], nonce)
    ok("self-signed (non-CA) certificate is rejected")

    # ── KEYREQ pairing: client-held key, host signs only the public key ──
    ck = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    spki = ck.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        issued = host._issue_client_cert(
            export_hint_ip="127.0.0.1",
            public_key_pem=base64.b64encode(spki).decode("ascii"),
        )
    finally:
        os.chdir(cwd)
    assert issued and issued.get("cert_pem")
    kcert = x509.load_pem_x509_certificate(issued["cert_pem"])
    kfp = kcert.fingerprint(hashes.SHA256()).hex().upper()
    assert host._verify_fingerprint_trusted(kfp)
    kcert_p, kkey_p = write_pair(tmp, "keyreq", ck, kcert)
    knonce = secrets.token_bytes(32)
    kparts = client._build_client_proof(kcert_p, kkey_p, knonce.hex()).split()
    assert host._verify_client_proof(kfp, kparts[1], kparts[3], knonce)
    ok("KEYREQ-issued certificate authenticates via challenge-response")

# ── session-token gate on the control plane ──
host.host_state.session_token = "tok123"
assert host._extract_authed_cmd("AUTH tok123 MOUSE_PKT 1 0 10 20") == "MOUSE_PKT 1 0 10 20"
assert host._extract_authed_cmd("AUTH wrongtok MOUSE_PKT 1 0 10 20") is None
assert host._extract_authed_cmd("MOUSE_PKT 1 0 10 20") is None
assert host._extract_authed_cmd("AUTH tok123") is None
ok("AUTH token prefix enforced on control packets")
host.host_state.session_token = None

# ── OK/TOKEN/CERT/STREAM response parsing ──
info, token, cert = client._parse_ok_response("OK:h.264:1920x1080+0+0;2560x1440+1080+162\nTOKEN deadbeef01")
assert info == ("h.264", "1920x1080+0+0;2560x1440+1080+162", ""), info
assert token == "deadbeef01", token
assert cert == "", cert
info2, token2, cert2 = client._parse_ok_response("OK:h.265:1920x1080\nTOKEN ab\nCERT Zm9v")
assert token2 == "ab" and info2[0] == "h.265" and cert2 == "Zm9v"
assert info2[2] == "", info2
info3, token3, cert3 = client._parse_ok_response(
    "OK:h.265:1707x1067+0+0\nTOKEN ab\nSTREAM 1280x720")
assert info3[1] == "1707x1067+0+0" and info3[2] == "1280x720", info3
assert client._size_from_text(info3[2]) == (1280, 720)
assert client._size_from_text("") is None
ok("OK/TOKEN/STREAM/CERT handshake response parsed correctly")

# ── failure reasons distinguish a stale client key from a mangled message ──
with tempfile.TemporaryDirectory() as tmp:
    host.CA_CERT = os.path.join(tmp, "host_ca.pem")
    host.CA_KEY = os.path.join(tmp, "host_ca.key")
    host.TRUSTED_DB = os.path.join(tmp, "trusted_clients.json")
    assert host._ensure_ca()
    with open(host.CA_KEY, "rb") as f:
        ca_key = serialization.load_pem_private_key(f.read(), password=None)
    with open(host.CA_CERT, "rb") as f:
        ca_cert = x509.load_pem_x509_certificate(f.read())

    key, cert = build_cert(ca_key, ca_cert.subject, "reason-probe")
    cert_p, key_p = write_pair(tmp, "probe", key, cert)
    fp = cert.fingerprint(hashes.SHA256()).hex().upper()
    nonce = secrets.token_bytes(32)
    proof = client._build_client_proof(cert_p, key_p, nonce.hex()).split()

    okd, why = host._verify_client_proof_detailed(fp, proof[1], proof[3], nonce)
    assert okd and why == "", (okd, why)
    okd, why = host._verify_client_proof_detailed("AB" * 32, proof[1], proof[3], nonce)
    assert not okd and "fingerprint" in why, why
    okd, why = host._verify_client_proof_detailed(fp, proof[1], proof[3], secrets.token_bytes(32))
    assert not okd and "challenge" in why, why
    okd, why = host._verify_client_proof_detailed(fp, "bm90LWEtY2VydA==", proof[3], nonce)
    assert not okd and "parsed" in why, why
    ok("proof failures are reported with the check that failed")

# ── a stale credential must still let you connect ──
# The host closes its end when it refuses a proof, so the PIN handshake that
# follows needs a fresh connection; the client also has to notice a cert/key
# pair that cannot sign for each other before the host rejects it.
with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
    # The host writes issued certificates to ./issued_clients, so run from tmp.
    host.CA_CERT = os.path.join(tmp, "host_ca.pem")
    host.CA_KEY = os.path.join(tmp, "host_ca.key")
    host.TRUSTED_DB = os.path.join(tmp, "trusted_clients.json")
    assert host._ensure_ca()
    with open(host.CA_KEY, "rb") as f:
        ca_key = serialization.load_pem_private_key(f.read(), password=None)
    with open(host.CA_CERT, "rb") as f:
        ca_cert = x509.load_pem_x509_certificate(f.read())

    def trust(fp_hex):
        try:
            with open(host.TRUSTED_DB, "r", encoding="utf-8") as f:
                entries = json.load(f)["trusted_clients"]
        except Exception:
            entries = []
        entries.append({"fingerprint": fp_hex, "common_name": "linuxplay-client",
                        "issued_on": "now", "trusted_since": "now", "last_seen": "now",
                        "status": "trusted"})
        with open(host.TRUSTED_DB, "w", encoding="utf-8") as f:
            json.dump({"trusted_clients": entries}, f)

    def mint(name):
        k, c = build_cert(ca_key, ca_cert.subject, "linuxplay-client")
        cp, kp = write_pair(tmp, name, k, c)
        trust(c.fingerprint(hashes.SHA256()).hex().upper())
        return cp, kp

    good_cert, good_key = mint("good")
    other_cert, other_key = mint("other")

    # The client resolves its credentials from next to its own file.
    client.__file__ = os.path.join(tmp, "client.py")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.2)
    client.TCP_HANDSHAKE_PORT = srv.getsockname()[1]

    def serve():
        while not host.host_state.should_terminate:
            try:
                conn, addr = srv.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=host._handle_handshake_conn,
                             args=(conn, addr[0], "h.264", None), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()

    def reset_host(pin=None):
        st = host.host_state
        st.session_active = False
        st.authed_client_ip = None
        st.session_token = None
        st.pin_code = pin
        st.pin_expiry = (time.time() + 300) if pin else 0
        st.pin_failures = {}

    def install(cert_p, key_p):
        for dst, src in ((os.path.join(tmp, "client_cert.pem"), cert_p),
                         (os.path.join(tmp, "client_key.pem"), key_p)):
            if src:
                with open(src, "rb") as s, open(dst, "wb") as d:
                    d.write(s.read())
            elif os.path.exists(dst):
                os.unlink(dst)

    assert client._credential_problem(good_cert, good_key) == ""
    assert "does not match" in client._credential_problem(good_cert, other_key)
    ok("client spots a certificate/key pair that cannot sign for each other")

    reset_host()
    install(good_cert, good_key)
    auth_ok, info, rejected = client._try_cert_handshake("127.0.0.1", good_cert, good_key)
    assert auth_ok and not rejected, (auth_ok, info, rejected)
    ok("stored certificate authenticates without a PIN")

    reset_host(pin="123456")
    install(good_cert, other_key)
    conn_ok, info = client.tcp_handshake_client("127.0.0.1", pin="123456", interactive=False)
    assert conn_ok, "mismatched pair must not dead-end the connection"
    ok("mismatched cert/key falls back to the PIN instead of a broken pipe")

    reset_host()
    conn_ok, info = client.tcp_handshake_client("127.0.0.1", pin=None, interactive=False)
    assert conn_ok, "the re-paired certificate should be usable straight away"
    assert client._credential_problem(os.path.join(tmp, "client_cert.pem"),
                                      os.path.join(tmp, "client_key.pem")) == ""
    backups = [n for n in os.listdir(tmp) if ".replaced-" in n]
    assert len(backups) == 2, backups
    ok("PIN pairing replaces the stale pair and the next connect skips the PIN")

    # A ~1.7 KB proof spans more than one TCP segment on any real path.
    reset_host()
    install(good_cert, good_key)
    fp_hex = client._read_pem_cert_fingerprint(good_cert)
    s = socket.create_connection(("127.0.0.1", client.TCP_HANDSHAKE_PORT), timeout=10)
    s.sendall(f"HELLO CERTFP:{fp_hex}\n".encode("utf-8"))
    challenge = s.recv(4096).decode("utf-8", errors="replace").strip()
    assert challenge.startswith("CHALLENGE "), challenge
    proof = client._build_client_proof(good_cert, good_key, challenge.split(None, 1)[1].strip())
    assert len(proof) > 1400, len(proof)
    s.sendall(proof[:1200].encode("utf-8"))
    time.sleep(0.4)
    s.sendall(proof[1200:].encode("utf-8"))
    resp = s.recv(4096).decode("utf-8", errors="replace").strip()
    s.close()
    assert resp.startswith("OK:"), resp
    ok("proof split across TCP segments still authenticates")

    # ...and so does a client from before that change, which sends HELLO and
    # KEYREQ in one write with no trailing newline.
    reset_host(pin="654321")
    install(None, None)
    old_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    spki = base64.b64encode(old_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)).decode("ascii")
    s = socket.create_connection(("127.0.0.1", client.TCP_HANDSHAKE_PORT), timeout=10)
    s.sendall(f"HELLO 654321\nKEYREQ {spki}".encode("utf-8"))
    resp = s.recv(8192).decode("utf-8", errors="replace").strip()
    s.close()
    assert resp.startswith("OK:") and "CERT " in resp, resp[:200]
    ok("a pre-fix client's PIN+KEYREQ line still gets a certificate")

    # ── the host's stream size reaches the client ──
    # Native (default): no STREAM line, so the client maps clicks by the desktop
    # size it already knows. With --resolution the host says what it encodes.
    reset_host()
    install(good_cert, good_key)
    host.host_state.stream_size = None
    ok_native, info_native = client.tcp_handshake_client("127.0.0.1", None, interactive=False)
    assert ok_native and info_native[2] == "", info_native
    reset_host()
    host.host_state.stream_size = (1280, 720)
    ok_scaled, info_scaled = client.tcp_handshake_client("127.0.0.1", None, interactive=False)
    assert ok_scaled, info_scaled
    assert info_scaled[2] == "1280x720", info_scaled
    assert client._size_from_text(info_scaled[2]) == (1280, 720)
    desktop = client._size_from_text(info_scaled[1].split(";")[0].split("+")[0])
    assert desktop and desktop != (1280, 720), (desktop, info_scaled)
    ok("handshake carries the stream size separately from the desktop geometry")
    host.host_state.stream_size = None

    srv.close()

print(f"\nALL {len(PASS)} AUTH TESTS PASSED")
