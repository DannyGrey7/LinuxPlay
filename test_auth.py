#!/usr/bin/env python3
"""Challenge-response certificate auth + session token tests (offline, tempdir)."""
import base64
import datetime
import os
import secrets
import sys
import tempfile

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

# ── OK/TOKEN/CERT response parsing ──
info, token, cert = client._parse_ok_response("OK:h.264:1920x1080+0+0;2560x1440+1080+162\nTOKEN deadbeef01")
assert info == ("h.264", "1920x1080+0+0;2560x1440+1080+162"), info
assert token == "deadbeef01", token
assert cert == "", cert
info2, token2, cert2 = client._parse_ok_response("OK:h.265:1920x1080\nTOKEN ab\nCERT Zm9v")
assert token2 == "ab" and info2[0] == "h.265" and cert2 == "Zm9v"
ok("OK/TOKEN/CERT handshake response parsed correctly")

print(f"\nALL {len(PASS)} AUTH TESTS PASSED")
