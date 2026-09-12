#!/usr/bin/env python3
import os
import subprocess
import argparse
import sys
import logging
import json
import time
import threading
import psutil
import socket
import atexit
import signal
import struct
import datetime
import platform as py_platform
import re
import secrets
import base64
import hmac

from shutil import which

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QTextEdit, QPushButton, QLabel, QHBoxLayout,
    QMessageBox
)
from PyQt5.QtGui import QFont, QPalette, QColor, QKeySequence
from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal

UDP_VIDEO_PORT = 5000
UDP_CONTROL_PORT = 7000
TCP_HANDSHAKE_PORT = 7001
UDP_CLIPBOARD_PORT = 7002
FILE_UPLOAD_PORT = 7003
UDP_HEARTBEAT_PORT = 7004
UDP_GAMEPAD_PORT = 7005
UDP_AUDIO_PORT = 6001

ACTIVE_CLIENT = None
ACTIVE_CLIENT_LOCK = threading.Lock()
PIN_LENGTH = 6
PIN_ROTATE_SECS = 30

# A wrong PIN must cost the sender something: after PIN_MAX_FAILURES bad
# attempts one address is refused for a doubling cool-off (30s -> 15min).
PIN_MAX_FAILURES = 3
PIN_LOCKOUT_BASE = 30.0
PIN_LOCKOUT_MAX  = 900.0

# How long the local user has to approve pairing a brand-new device (GUI hosts).
PAIR_APPROVAL_TIMEOUT = 45.0

# A failing encoder is restarted with backoff instead of stopping the host.
STREAM_MAX_RESTARTS = 5
STREAM_RESTART_MAX_DELAY = 20.0

DEFAULT_FPS = "30"
LEGACY_BITRATE = "8M"
DEFAULT_RES = "1920x1080"

IS_LINUX   = py_platform.system() == "Linux"

HEARTBEAT_INTERVAL = 1.0
HEARTBEAT_TIMEOUT  = 10.0
RECONNECT_COOLDOWN = 2.0

try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.backends import default_backend
    HAVE_CRYPTO = True
except Exception:
    HAVE_CRYPTO = False

CA_CERT = "host_ca.pem"
CA_KEY  = "host_ca.key"
TRUSTED_DB = "trusted_clients.json"

def _chmod_600(path):
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass

def _harden_secret_files():
    """Best-effort 0600 on key material created by older versions."""
    for path in (CA_KEY, CA_CERT, TRUSTED_DB):
        if os.path.exists(path):
            _chmod_600(path)
    try:
        if os.path.isdir("issued_clients"):
            for entry in os.listdir("issued_clients"):
                d = os.path.join("issued_clients", entry)
                if os.path.isdir(d):
                    for f in os.listdir(d):
                        _chmod_600(os.path.join(d, f))
    except Exception:
        pass

def _ensure_ca():
    if not HAVE_CRYPTO:
        logging.warning("[AUTH] cryptography not available; certificate auth disabled.")
        return False
    if os.path.exists(CA_CERT) and os.path.exists(CA_KEY):
        return True
    try:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u"LinuxPlay Host CA")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        with open(CA_KEY, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        os.chmod(CA_KEY, 0o600)
        with open(CA_CERT, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        os.chmod(CA_CERT, 0o600)
        logging.info("[AUTH] Created new host CA (host_ca.pem / host_ca.key)")
        return True
    except Exception as e:
        logging.error("[AUTH] Failed to create CA: %s", e)
        return False

def _trust_db_sane(db) -> bool:
    """A hand-edited or truncated trust DB must never crash the host."""
    if not isinstance(db, dict):
        return False
    entries = db.get("trusted_clients", [])
    if not isinstance(entries, list):
        return False
    return all(isinstance(rec, dict) for rec in entries)


def _load_trust_db():
    db = {"trusted_clients": []}
    try:
        if os.path.exists(TRUSTED_DB):
            with open(TRUSTED_DB, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if _trust_db_sane(loaded):
                db = loaded
            else:
                logging.error("%s is malformed — treating it as empty "
                              "(no client trusted until it is repaired).", TRUSTED_DB)
    except Exception:
        pass
    return db

def _save_trust_db(db):
    try:
        with open(TRUSTED_DB, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2)
        _chmod_600(TRUSTED_DB)
        return True
    except Exception as e:
        logging.error("[AUTH] Failed to write %s: %s", TRUSTED_DB, e)
        return False

def _trust_record_for(fp_hex, db):
    if not isinstance(db, dict):
        return None
    for rec in db.get("trusted_clients", []) or []:
        if isinstance(rec, dict) and rec.get("fingerprint") == fp_hex:
            return rec
    return None

def _verify_fingerprint_trusted(fp_hex):
    db = _load_trust_db()
    rec = _trust_record_for(fp_hex, db)
    return (rec is not None) and (rec.get("status") == "trusted")

def _issue_client_cert(client_name="linuxplay-client", export_hint_ip="", public_key_pem=None):
    """Issue a client certificate.

    With public_key_pem (KEYREQ flow) the client generated its own keypair
    and only the certificate needs to cross the network; without it the host
    generates a key and exports the full bundle to issued_clients/.
    """
    if not _ensure_ca():
        return None

    try:
        with open(CA_KEY, "rb") as f:
            ca_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
        with open(CA_CERT, "rb") as f:
            ca_cert = x509.load_pem_x509_certificate(f.read(), default_backend())

        key_pem = None
        if public_key_pem:
            client_public_key = serialization.load_pem_public_key(
                base64.b64decode(public_key_pem), backend=default_backend()
            )
        else:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            client_public_key = key.public_key()
            key_pem = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )

        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, client_name)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_cert.subject)
            .public_key(client_public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=1825))
            .sign(ca_key, hashes.SHA256())
        )

        cert_pem = cert.public_bytes(serialization.Encoding.PEM)

        fp_hex = cert.fingerprint(hashes.SHA256()).hex().upper()

        db = _load_trust_db()
        if _trust_record_for(fp_hex, db) is None:
            now = datetime.datetime.utcnow().isoformat() + "Z"
            db.setdefault("trusted_clients", []).append({
                "fingerprint": fp_hex,
                "common_name": client_name,
                "issued_on": now,
                "trusted_since": now,
                "last_seen": now,
                "status": "trusted"
            })
            _save_trust_db(db)

        stamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        export_dir = os.path.join("issued_clients", f"{stamp}_{export_hint_ip or 'client'}")
        os.makedirs(export_dir, exist_ok=True)
        with open(os.path.join(export_dir, "client_cert.pem"), "wb") as f: f.write(cert_pem)
        _chmod_600(os.path.join(export_dir, "client_cert.pem"))
        if key_pem is not None:
            with open(os.path.join(export_dir, "client_key.pem"), "wb") as f: f.write(key_pem)
            _chmod_600(os.path.join(export_dir, "client_key.pem"))
        try:
            with open(CA_CERT, "rb") as f: ca_pem = f.read()
            with open(os.path.join(export_dir, "host_ca.pem"), "wb") as f: f.write(ca_pem)
        except Exception:
            pass

        logging.info("[AUTH] Issued client cert '%s' (FP %s…), exported to %s",
                     client_name, fp_hex[:12], export_dir)
        return {"fingerprint": fp_hex, "export_dir": export_dir, "cert_pem": cert_pem}
    except Exception as e:
        logging.error("[AUTH] Issue client cert failed: %s", e)
        return None

_PSS_PADDING = None
if HAVE_CRYPTO:
    from cryptography.hazmat.primitives.asymmetric import padding as _x509_padding
    _PSS_PADDING = _x509_padding.PSS(
        mgf=_x509_padding.MGF1(hashes.SHA256()),
        salt_length=_x509_padding.PSS.MAX_LENGTH,
    )

def _verify_client_proof(fp_hex, cert_b64, sig_b64, nonce):
    """Verify the client owns the private key of a trusted certificate.

    Checks, in order: the offered cert's SHA-256 fingerprint matches the
    trusted fingerprint, the cert was signed by this host's CA, and the
    signature over the fresh nonce verifies with the cert's public key.
    """
    if not HAVE_CRYPTO or not nonce:
        return False
    try:
        cert = x509.load_pem_x509_certificate(base64.b64decode(cert_b64), default_backend())
        if cert.fingerprint(hashes.SHA256()).hex().upper() != fp_hex:
            return False
        with open(CA_CERT, "rb") as f:
            ca_cert = x509.load_pem_x509_certificate(f.read(), default_backend())
        ca_cert.public_key().verify(
            cert.signature, cert.tbs_certificate_bytes,
            _x509_padding.PKCS1v15(), cert.signature_hash_algorithm,
        )
        cert.public_key().verify(base64.b64decode(sig_b64), nonce, _PSS_PADDING, hashes.SHA256())
        return True
    except Exception:
        return False

def _token_ok(token) -> bool:
    cur = host_state.session_token
    if not cur or not token:
        return False
    return hmac.compare_digest(str(token), cur)

def _extract_authed_cmd(msg: str):
    """Strip the 'AUTH <token>' prefix from a control-plane packet.

    Returns the remaining command text, or None when the packet is missing
    the prefix or carries the wrong token.
    """
    if not msg.startswith("AUTH "):
        return None
    parts = msg.split(None, 2)
    if len(parts) < 3:
        return None
    if not _token_ok(parts[1]):
        return None
    return parts[2]

def _marker_value() -> str:
    marker = os.environ.get("LINUXPLAY_MARKER", "LinuxPlayHost")
    sid = os.environ.get("LINUXPLAY_SID", "")
    return f"{marker}:{sid}" if sid else marker

def _ffmpeg_base_cmd() -> list:
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:2"]

def _marker_opt() -> list:
    return ["-metadata", f"comment={_marker_value()}"]

try:
    from pynput.mouse import Controller as MouseCtl, Button
    from pynput.keyboard import Controller as KeyCtl, Key
    HAVE_PYNPUT = True
    _mouse = MouseCtl()
    _keys = KeyCtl()
except Exception:
    HAVE_PYNPUT = False

try:
    import pyperclip
    HAVE_PYPERCLIP = True
except Exception:
    HAVE_PYPERCLIP = False

try:
    from evdev import UInput, ecodes, AbsInfo
    HAVE_UINPUT = True
except Exception:
    HAVE_UINPUT = False

try:
    import portal_capture
    HAVE_PORTAL = True
except Exception as _pe:
    portal_capture = None
    HAVE_PORTAL = False


def _resolve_capture_mode():
    """Pick the video capture backend once at startup: portal | kmsgrab | x11grab."""
    sess = _session_type()
    pref = (os.environ.get("LINUXPLAY_CAPTURE", "auto") or "auto").strip().lower()
    if sess != "wayland":
        return "x11grab"
    if pref in ("portal", "pipewire"):
        return "portal"
    if pref in ("kmsgrab", "kms"):
        return "kmsgrab"
    if pref == "x11grab":
        return "x11grab"
    # auto under Wayland: portal first (KDE/GNOME/wlroots), kmsgrab as fallback
    if HAVE_PORTAL and portal_capture.gst_pipewiresrc_available():
        return "portal"
    return "kmsgrab"

_warn_throttle = {}
_warn_throttle_lock = threading.Lock()

def _warn_throttled(key: str, msg: str, interval: float = 30.0, exc_info: bool = False):
    """Log a recurring non-fatal error at most once per interval (per key)."""
    now = time.time()
    with _warn_throttle_lock:
        if now - _warn_throttle.get(key, 0.0) < interval:
            return
        _warn_throttle[key] = now
    logging.warning(msg, exc_info=exc_info)


class HostState:
    def __init__(self):
        self.video_threads = {}
        self.session_active = False
        self.authed_client_ip = None
        self.pin_code = None
        self.pin_expiry = 0.0
        self.pin_lock = threading.Lock()
        self.audio_thread = None
        self.current_bitrate = LEGACY_BITRATE
        self.last_clipboard_content = ""
        self.ignore_clipboard_update = False
        self.should_terminate = False
        self.video_thread_lock = threading.Lock()
        self.clipboard_lock = threading.Lock()
        self.handshake_sock = None
        self.control_sock = None
        self.clipboard_listener_sock = None
        self.file_upload_sock = None
        self.heartbeat_sock = None
        self.last_pong_ts = 0.0
        self.last_disconnect_ts = 0.0
        self.client_ip = None
        self.monitors = []
        self.shutdown_lock = threading.Lock()
        self.shutdown_reason = None
        self.net_mode = "lan"
        self.starting_streams = False
        self.gamepad_thread = None
        self.session_token = None
        self.pin_failures = {}     # peer ip -> [fail_count, locked_until_ts]
        self.pin_fail_lock = threading.Lock()
        self.gui_window = None     # set by HostWindow: enables pairing prompts
        self.log_path = None
        self.session_epoch = 0     # bumped on connect/disconnect to clear backoff
        self.last_rtt_ms = 0.0
        self.last_jitter_ms = 0.0
        self.encoder_stats = {}    # stream name -> latest ffmpeg -progress values
        self.current_fps = 0.0

host_state = HostState()
HOST_ARGS = None

def _map_nvenc_tune(tune: str) -> str:
    t = (tune or "").strip().lower()
    if not t or t in ("auto", "default", "none"):
        return ""

    alias_map = {
        "ull": "ull",
        "ultra-low-latency": "ull",
        "ultra_low_latency": "ull",
        "zerolatency": "ull",
        "realtime": "ull",

        "low-latency": "ll",
        "low_latency": "ll",
        "ll": "ll",

        "hq": "hq",
        "high-quality": "hq",
        "high_quality": "hq",
        "hp": "hp",
        "high-performance": "hp",
        "high_performance": "hp",
        "performance": "hp",

        "lossless": "lossless",
        "lossless-highperf": "losslesshp",
        "lossless_highperf": "losslesshp",

        "blu-ray": "bd",
        "bluray": "bd",
    }

    mapped = alias_map.get(t)
    if mapped:
        return mapped

    logging.warning("Unrecognized NVENC tune '%s' — passing through as-is.", t)
    return t

def _vaapi_fmt_for_pix_fmt(pix_fmt: str, codec: str) -> str:
    pf = (pix_fmt or "").strip().lower()

    valid_vaapi_fmts = {
        "nv12", "yuv420p", "yuyv422", "uyvy422", "yuv422p",
        "yuv444p", "rgb0", "bgr0", "rgba", "bgra",
        "p010", "p010le", "yuv420p10", "yuv420p10le",
        "yuv422p10", "yuv422p10le", "yuv444p10", "yuv444p10le",
        "yuv444p12le", "yuv444p16le",
    }

    if pf in valid_vaapi_fmts:
        logging.info("Using requested VAAPI pix_fmt '%s' for codec %s.", pf, codec)
        return pf

    if pf in ("yuv420", "420p"):
        return "yuv420p"
    if pf in ("yuv420p10bit", "yuv420p10b"):
        return "yuv420p10le"
    if pf in ("yuv444", "444p"):
        return "yuv444p"

    logging.warning("Unrecognized pix_fmt '%s' — falling back to 'nv12'.", pf)
    return "nv12"

def trigger_shutdown(reason: str):
    with host_state.shutdown_lock:
        if host_state.should_terminate:
            return
        host_state.should_terminate = True
        host_state.shutdown_reason = reason
        logging.critical("FATAL/STOP: %s -- stopping all streams and listeners.", reason)

        for s in (
            host_state.handshake_sock,
            host_state.control_sock,
            host_state.clipboard_listener_sock,
            host_state.file_upload_sock,
        ):
            try:
                if s:
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                    s.close()
            except Exception:
                pass

        set_status(f"Stopping… ({reason})")

def stop_all():
    host_state.should_terminate = True

    with host_state.video_thread_lock:
        for thread in list(host_state.video_threads.values()):
            thread.stop()
            thread.join(timeout=2)
        host_state.video_threads.clear()

    if host_state.audio_thread:
        host_state.audio_thread.stop()
        host_state.audio_thread.join(timeout=2)
        host_state.audio_thread = None
    if host_state.gamepad_thread:
        try:
            host_state.gamepad_thread.stop()
            host_state.gamepad_thread.join(timeout=2)
        except Exception:
            pass
        host_state.gamepad_thread = None
    for s in (
        host_state.handshake_sock,
        host_state.control_sock,
        host_state.clipboard_listener_sock,
        host_state.file_upload_sock,
    ):
        try:
            if s:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                s.close()
        except Exception:
            pass

    host_state.starting_streams = False
    portal = getattr(host_state, "portal", None)
    if portal:
        try:
            portal.close()
        except Exception:
            pass
        host_state.portal = None

def stop_streams_only():
    with host_state.video_thread_lock:
        if host_state.video_threads:
            logging.info("Stopping active video streams...")
            for t in list(host_state.video_threads.values()):
                try:
                    t.stop()
                    t.join(timeout=2)
                except Exception as e:
                    logging.debug(f"Error stopping video thread: {e}")
            host_state.video_threads.clear()

        if host_state.audio_thread:
            try:
                host_state.audio_thread.stop()
                host_state.audio_thread.join(timeout=2)
            except Exception as e:
                logging.debug(f"Error stopping audio thread: {e}")
            host_state.audio_thread = None

        host_state.starting_streams = False
        host_state.last_disconnect_ts = time.time()
        logging.info("All streams stopped and cooldown set.")

def cleanup():
    stop_all()
atexit.register(cleanup)

def _pin_lockout_remaining(peer_ip: str) -> float:
    with host_state.pin_fail_lock:
        rec = host_state.pin_failures.get(peer_ip)
        if not rec:
            return 0.0
        return max(0.0, float(rec[1]) - time.time())


def _pin_note_failure(peer_ip: str):
    """Count a bad PIN; repeated failures from one address earn a cool-off."""
    with host_state.pin_fail_lock:
        now = time.time()
        if len(host_state.pin_failures) > 500:
            for ip in [k for k, v in host_state.pin_failures.items()
                       if v[1] < now - 3600.0]:
                host_state.pin_failures.pop(ip, None)
        rec = host_state.pin_failures.setdefault(peer_ip, [0, 0.0])
        rec[0] += 1
        if rec[0] >= PIN_MAX_FAILURES:
            over = min(rec[0] - PIN_MAX_FAILURES, 5)
            lock = min(PIN_LOCKOUT_BASE * (2 ** over), PIN_LOCKOUT_MAX)
            rec[1] = now + lock
            logging.warning("[AUTH] %s: %d bad PINs — locked out for %.0fs.",
                            peer_ip, rec[0], lock)
        else:
            logging.info("[AUTH] %s: bad PIN (%d/%d before cool-off).",
                         peer_ip, rec[0], PIN_MAX_FAILURES)


def _pin_clear_failures(peer_ip: str):
    with host_state.pin_fail_lock:
        host_state.pin_failures.pop(peer_ip, None)


def _gen_pin(length=PIN_LENGTH):
    import secrets
    n = secrets.randbelow(10**length)
    return f"{n:0{length}d}"

def pin_rotate_if_needed(force=False):
    now = time.time()
    with host_state.pin_lock:
        if host_state.session_active:
            return
        if force or not host_state.pin_code or now >= host_state.pin_expiry:
            host_state.pin_code = _gen_pin()
            host_state.pin_expiry = now + PIN_ROTATE_SECS
            logging.info("[AUTH] New PIN: %s (valid %ds)", host_state.pin_code, PIN_ROTATE_SECS)
            try:
                set_status(f"Waiting for PIN: {host_state.pin_code}")
            except Exception:
                pass
            try:
                set_pin_display(f"PIN:  {host_state.pin_code}")
            except Exception:
                pass

def pin_manager_thread():
    while not host_state.should_terminate:
        if not host_state.session_active:
            pin_rotate_if_needed()
        time.sleep(1)

def has_nvidia():
    return which("nvidia-smi") is not None

def is_intel_cpu():
    try:
        if IS_LINUX:
            with open("/proc/cpuinfo","r") as f:
                return "GenuineIntel" in f.read()
        p = (py_platform.processor() or "").lower()
        return "intel" in p or "intel" in py_platform.platform().lower()
    except Exception:
        return False

def has_vaapi():
    return IS_LINUX and os.path.exists("/dev/dri/renderD128")

def ffmpeg_has_encoder(name: str) -> bool:
    try:
        out = subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stderr=subprocess.STDOUT, universal_newlines=True
        ).lower()
        return name.lower() in out
    except Exception:
        return False

def ffmpeg_has_demuxer(name: str) -> bool:
    try:
        out = subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-demuxers"],
            stderr=subprocess.STDOUT, universal_newlines=True
        ).lower()
        for line in out.splitlines():
            line = line.strip().lower()
            if line.startswith("d ") or line.startswith(" d "):
                parts = line.split()
                if len(parts) >= 2 and parts[1] == name.lower():
                    return True
        return False
    except Exception:
        return False

def ffmpeg_has_device(name: str) -> bool:
    try:
        out = subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-devices"],
            stderr=subprocess.STDOUT, universal_newlines=True
        ).lower()
        for line in out.splitlines():
            line = line.strip().lower()
            if line.startswith("d ") or line.startswith(" d "):
                parts = line.split()
                if len(parts) >= 2 and parts[1] == name.lower():
                    return True
        return False
    except Exception:
        return False

def _handle_pong(msg: str, peer_ip: str, now: float) -> bool:
    """Accept a heartbeat PONG and measure the round trip when we get a stamp.

    The client echoes the timestamp we put in the PING, so the RTT here needs
    no clock sync between the two machines.
    """
    parts = msg.split()
    if (not parts or parts[0] != "PONG" or peer_ip != host_state.client_ip
            or len(parts) not in (2, 3) or not _token_ok(parts[1])):
        return False
    host_state.last_pong_ts = now
    if len(parts) == 3:
        try:
            rtt = (now - float(parts[2])) * 1000.0
        except Exception:
            return True
        if 0.0 <= rtt < 60000.0:
            prev = host_state.last_rtt_ms
            host_state.last_rtt_ms = rtt
            if prev > 0.0:
                host_state.last_jitter_ms = (0.8 * host_state.last_jitter_ms
                                             + 0.2 * abs(rtt - prev))
    return True

def _stats_payload(cpu: float, gpu: float, mem: float) -> str:
    """The STATS datagram the client's overlay reads.

    Kept separate from the sender so the wire format is testable on its own.
    """
    enc = _video_encoder_stats()
    return (f"STATS {cpu:.1f} {gpu:.1f} {mem:.1f} "
            f"{float(enc.get('fps') or 0.0):.1f} "
            f"{host_state.last_rtt_ms:.1f} {host_state.last_jitter_ms:.1f} "
            f"{float(enc.get('bitrate_kbps') or 0.0):.0f} "
            f"{float(enc.get('drop_frames') or 0.0):.0f} "
            f"{float(enc.get('frame') or 0.0):.0f}")

_PROGRESS_KEYS = {"frame", "fps", "bitrate", "total_size", "out_time_ms",
                  "out_time_us", "speed", "drop_frames", "dup_frames"}
# Any other "name=value" line is still -progress metadata (progress=continue,
# out_time=…, stream_0_0_q=…), not an encoder error worth logging.
_PROGRESS_LINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

def _progress_update(store: dict, line: str) -> bool:
    """Fold one ffmpeg -progress line into store. Returns True if it was progress."""
    key, sep, val = line.partition("=")
    key = key.strip()
    if not sep or key not in _PROGRESS_KEYS:
        return False
    val = val.strip()
    if key == "bitrate":
        store["bitrate"] = val
        try:
            store["bitrate_kbps"] = float(val.split("k", 1)[0])
        except Exception:
            pass
        return True
    try:
        store[key] = float(val)
    except Exception:
        store[key] = val
    if key == "fps":
        try:
            host_state.current_fps = float(val)
        except Exception:
            pass
    return True

def _video_encoder_stats() -> dict:
    """Latest ffmpeg -progress numbers for the lowest-indexed video stream."""
    try:
        names = [n for n in (host_state.encoder_stats or {})
                 if str(n).lower().startswith("video")]
        if names:
            best = sorted(names, key=lambda n: str(n))[0]
            stats = dict(host_state.encoder_stats.get(best) or {})
            stats["stream"] = str(best)
            return stats
    except Exception:
        pass
    return {}


class StreamThread(threading.Thread):
    """Run one ffmpeg encoder (plus optional feeder) and keep it running.

    A transient encoder failure used to call trigger_shutdown() and take the
    whole host with it. Now the child is restarted with backoff; only after
    STREAM_MAX_RESTARTS consecutive failures does the thread retire, which
    lets the session manager retry the stream later.
    """

    def __init__(self, cmd, name, feeder_cmd=None):
        super().__init__(daemon=True)
        self.cmd = cmd
        self.name = name
        self.feeder_cmd = feeder_cmd
        self.feeder = None
        self.process = None
        self._running = True

    def _kill_children(self):
        """Terminate and reap the encoder and its feeder, if they are alive."""
        for proc in (self.process, self.feeder):
            if proc is None:
                continue
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1.5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            proc.wait(timeout=1.0)
                        except Exception:
                            pass
                else:
                    proc.wait(timeout=0.2)          # reap an already-dead child
            except Exception:
                pass

    def _start_children(self) -> bool:
        # Never inherit an older encoder or feeder: it would keep encoding to
        # the same UDP port as its replacement.
        self._kill_children()
        self.process = None
        self.feeder = None

        if self.feeder_cmd:
            try:
                self.feeder = subprocess.Popen(
                    self.feeder_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                logging.error("%s feeder failed to start: %s", self.name, e)
                self.feeder = None
                return False
        try:
            self.process = subprocess.Popen(
                self.cmd,
                stdin=(self.feeder.stdout if self.feeder else None),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )
        except Exception as e:
            logging.error("%s failed to start: %s", self.name, e)
            self._kill_children()
            self.process = None
            self.feeder = None
            return False

        if not self._running or host_state.should_terminate:
            # stop() ran while we were spawning: do not orphan a live encoder
            # that would then fight the next session for the same UDP port.
            logging.info("%s was starting while the stream stopped — terminating it.",
                         self.name)
            self._kill_children()
            self.process = None
            self.feeder = None
            return False

        try:
            import psutil, os
            ps = psutil.Process(self.process.pid)
            ps.nice(-10)
            cpu_count = os.cpu_count()
            if cpu_count and cpu_count > 4:
                ps.cpu_affinity(list(range(0, min(cpu_count, 8))))
            logging.debug(f"Affinity + priority applied to {self.name}")
        except Exception as e:
            logging.debug(f"Affinity set failed: {e}")

        self._stderr_tail = []
        threading.Thread(target=self._drain_stderr, args=(self.process,),
                         name=f"{self.name}-stderr", daemon=True).start()
        return True

    def _drain_stderr(self, proc):
        """Read the encoder's stderr continuously.

        ffmpeg -progress writes here as well, and an unread pipe fills up
        (~64 KB) and then blocks the encoder, so this both keeps the stream
        alive and feeds the client's stats overlay with real encoder numbers.
        """
        store = host_state.encoder_stats.setdefault(self.name, {})
        try:
            for raw in iter(proc.stderr.readline, ""):
                line = raw.strip()
                if not line:
                    continue
                if _progress_update(store, line):
                    continue
                if _PROGRESS_LINE_RE.match(line):
                    continue
                tail = getattr(self, "_stderr_tail", None)
                if tail is None:
                    tail = self._stderr_tail = []
                tail.append(line)
                if len(tail) > 25:
                    del tail[0]
                logging.warning("%s encoder: %s", self.name, line)
        except Exception:
            pass

    def _wait_for_exit(self):
        """Block until the encoder exits; returns (returncode, stderr)."""
        while self._running and not host_state.should_terminate:
            try:
                ret = self.process.poll()
            except Exception:
                return None, ""
            if ret is not None:
                try:
                    self.process.wait(timeout=0.5)      # stderr is drained live
                except Exception:
                    pass
                err = "\n".join(getattr(self, "_stderr_tail", None) or [])
                if not err:
                    try:
                        _, err = self.process.communicate(timeout=0.5)
                    except Exception:
                        err = ""
                if self.feeder and self.feeder.poll() is not None:
                    try:
                        self.feeder.wait(timeout=0.5)
                    except Exception:
                        pass
                return ret, err
            time.sleep(0.2)
        return None, ""

    def _retire(self):
        """Drop this thread from host_state so the session manager retries later."""
        self._kill_children()
        self.process = None
        self.feeder = None
        with host_state.video_thread_lock:
            for idx, t in list(host_state.video_threads.items()):
                if t is self:
                    host_state.video_threads.pop(idx, None)
        if host_state.audio_thread is self:
            host_state.audio_thread = None

    def run(self):
        logging.info("Starting %s: %s", self.name, " ".join(self.cmd))
        if self.feeder_cmd:
            logging.info("Starting %s feeder: %s", self.name, " ".join(self.feeder_cmd))

        failures = 0
        while self._running and not host_state.should_terminate:
            if self._start_children():
                ret, err = self._wait_for_exit()
                if not self._running or host_state.should_terminate:
                    break
                if ret == 0:
                    logging.error("%s stopped unexpectedly (exit 0).", self.name)
                elif ret is not None:
                    logging.error("%s exited (%s). stderr tail:\n%s", self.name, ret,
                                  (err or "(no output)").strip()[-1500:])
            else:
                logging.error("%s could not be started.", self.name)

            failures += 1
            if failures > STREAM_MAX_RESTARTS:
                logging.error("%s failed %d times in a row — pausing this stream; "
                              "the session manager will retry.", self.name, failures)
                set_status(f"{self.name} failed — retrying shortly…")
                self._retire()
                return

            delay = min(1.0 * (2 ** (failures - 1)), STREAM_RESTART_MAX_DELAY)
            logging.warning("%s restarting in %.0fs (attempt %d/%d).",
                            self.name, delay, failures, STREAM_MAX_RESTARTS)
            set_status(f"{self.name} restarting in {int(delay)}s…")
            waited = 0.0
            while waited < delay and self._running and not host_state.should_terminate:
                time.sleep(0.2)
                waited += 0.2

    def stop(self):
        self._running = False
        try:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        except Exception:
            pass
        if self.feeder:
            try:
                if self.feeder.poll() is None:
                    self.feeder.terminate()
                    try:
                        self.feeder.wait(timeout=1.5)
                    except subprocess.TimeoutExpired:
                        self.feeder.kill()
            except Exception:
                pass

def _detect_monitors_xrandr():
    try:
        out = subprocess.check_output(["xrandr", "--listmonitors"], universal_newlines=True)
    except Exception as e:
        logging.warning("xrandr failed (%s); using default single monitor.", e)
        return []
    mons = []
    for line in out.strip().splitlines()[1:]:
        parts = line.split()
        for part in parts:
            if 'x' in part and '+' in part:
                try:
                    res, ox, oy = part.split('+')
                    w,h = res.split('x')
                    w = int(w.split('/')[0]); h = int(h.split('/')[0])
                    mons.append((w,h,int(ox),int(oy))); break
                except Exception:
                    continue
    return mons

def _session_type():
    st = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    if st in ("x11", "wayland"):
        return st
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"

def _detect_monitors_kscreen():
    """KDE Plasma Wayland: kscreen-doctor -o (Geometry: X,Y WxH)."""
    if not which("kscreen-doctor"):
        return []
    try:
        out = subprocess.check_output(
            ["kscreen-doctor", "-o"], universal_newlines=True, stderr=subprocess.DEVNULL
        )
    except Exception:
        return []
    import re
    out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", out)   # kscreen-doctor colorizes even when piped
    mons = []
    for block in out.split("Output: ")[1:]:
        if not any(ln.strip() == "enabled" for ln in block.splitlines()):
            continue
        m = re.search(r"Geometry:\s*(-?\d+)\s*,\s*(-?\d+)\s+(\d+)x(\d+)", block)
        if m:
            ox, oy, w, h = (int(g) for g in m.groups())
            mons.append((w, h, ox, oy))
    return mons

def _detect_monitors_hyprctl():
    """Hyprland: hyprctl -j monitors."""
    if not which("hyprctl"):
        return []
    try:
        import json
        out = subprocess.check_output(
            ["hyprctl", "-j", "monitors"], universal_newlines=True, stderr=subprocess.DEVNULL
        )
        data = json.loads(out)
    except Exception:
        return []
    mons = []
    for m in data:
        if m.get("disabled"):
            continue
        try:
            mons.append((int(m["width"]), int(m["height"]), int(m["x"]), int(m["y"])))
        except Exception:
            continue
    return mons

def _detect_monitors_wlr_randr():
    """wlroots compositors (sway, etc.): wlr-randr."""
    if not which("wlr-randr"):
        return []
    try:
        out = subprocess.check_output(["wlr-randr"], universal_newlines=True, stderr=subprocess.DEVNULL)
    except Exception:
        return []
    import re
    mons = []
    cur = None
    def _flush():
        if cur and cur.get("enabled") and cur.get("mode") and cur.get("pos") is not None:
            w, h = cur["mode"]
            ox, oy = cur["pos"]
            mons.append((w, h, ox, oy))
    for ln in out.splitlines():
        if not ln.strip():
            continue
        if not ln.startswith((" ", "\t")):
            _flush()
            cur = {"name": ln.strip().split()[0]}
        elif cur is not None:
            s = ln.strip()
            if s.startswith("Enabled:"):
                cur["enabled"] = s.split(":", 1)[1].strip().lower() == "yes"
            elif s.startswith("Position:"):
                m = re.match(r"(-?\d+)\s*,\s*(-?\d+)", s.split(":", 1)[1].strip())
                cur["pos"] = (int(m.group(1)), int(m.group(2))) if m else None
            elif "(current)" in s:
                m = re.match(r"(\d+)x(\d+)", s)
                if m:
                    cur["mode"] = (int(m.group(1)), int(m.group(2)))
    _flush()
    return mons

def _detect_monitors_linux():
    sess = _session_type()
    if sess == "wayland":
        for probe in (_detect_monitors_hyprctl, _detect_monitors_kscreen, _detect_monitors_wlr_randr):
            try:
                mons = probe()
            except Exception as e:
                logging.debug("Monitor probe %s failed: %s", probe.__name__, e)
                mons = []
            if mons:
                logging.info("Detected %d monitor(s) via %s (Wayland session).", len(mons), probe.__name__)
                return mons
        logging.warning("Wayland monitor probes failed; falling back to xrandr (XWayland).")
    return _detect_monitors_xrandr()

def detect_monitors():
    return _detect_monitors_linux()

def _kmsgrab_perms_ok():
    try:
        if os.geteuid() == 0:
            return True
        ff = which("ffmpeg")
        if not ff:
            return True
        r = subprocess.run(["getcap", ff], capture_output=True, universal_newlines=True, timeout=5)
        return "cap_sys_admin" in (r.stdout or "")
    except Exception:
        return True


def _input_ll_flags():
    return [
        "-fflags","nobuffer","-avioflags","direct",
        "-use_wallclock_as_timestamps","1",
        "-thread_queue_size","64",
        "-probesize","32",
        "-analyzeduration","0",
    ]

def _output_sync_flags():
    return ["-fps_mode","passthrough"]

def _mpegts_ll_mux_flags():
    return ["-flush_packets","1","-max_interleave_delta","0","-muxdelay","0","-muxpreload","0","-mpegts_flags","resend_headers"]

def _best_ts_pkt_size(mtu_guess: int, ipv6: bool) -> int:
    if mtu_guess <= 0:
        mtu_guess = 1500
    overhead = 48 if ipv6 else 28
    max_payload = max(512, mtu_guess - overhead)
    return max(188, (max_payload // 188) * 188)
def _route_mtu(ip: str) -> int:
    """Best-effort path MTU to a client via the kernel routing table (Linux `ip`); 0 when unknown."""
    try:
        out = subprocess.check_output(
            ["ip", "route", "get", str(ip)],
            stderr=subprocess.DEVNULL, universal_newlines=True, timeout=1.0,
        )
        m = re.search(r"\bdev (\S+)", out)
        if not m:
            return 0
        out = subprocess.check_output(
            ["ip", "-o", "link", "show", "dev", m.group(1)],
            stderr=subprocess.DEVNULL, universal_newlines=True, timeout=1.0,
        )
        m = re.search(r"\bmtu (\d+)", out)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0

def _udp_ts_pkt_size(ip: str) -> int:
    """MPEG-TS UDP payload size that fits inside the path MTU to ip (never IP-fragmented)."""
    mtu_guess = 1500
    rt_mtu = _route_mtu(ip)
    if rt_mtu:
        mtu_guess = min(mtu_guess, rt_mtu)
    pkt = _best_ts_pkt_size(mtu_guess, ":" in str(ip))
    if mtu_guess < 1500:
        logging.info("Path MTU to %s is %d → TS pkt_size %d (avoids IP fragmentation).",
                     ip, mtu_guess, pkt)
    return pkt

def _parse_bitrate_bits(bstr: str) -> int:
    if not bstr: return 0
    s = str(bstr).strip().lower()
    try:
        if s.endswith("k"):
            return int(float(s[:-1]) * 1000)
        if s.endswith("m"):
            return int(float(s[:-1]) * 1_000_000)
        if s.endswith("g"):
            return int(float(s[:-1]) * 1_000_000_000)
        return int(float(s))
    except Exception:
        return 0

def _format_bits(bits: int) -> str:
    if bits >= 1_000_000:
        return f"{max(1, int(bits/1_000_000))}M"
    if bits >= 1000:
        return f"{max(1, int(bits/1000))}k"
    return str(max(1, bits))

def _target_bpp(codec: str, fps: int) -> float:
    c = (codec or "h.264").lower()
    if c in ("h.265","hevc"):
        base = 0.045
    else:
        base = 0.07
    if fps >= 90:
        base += 0.02
    return base

def _safe_nvenc_preset(preset: str) -> str:
    preset = (preset or "").strip().lower()
    alias_map = {
        "ultrafast": "p1", "superfast": "p2", "veryfast": "p3",
        "fast": "p4", "medium": "p5", "slow": "p6", "slower": "p7",
        "veryslow": "p7",
        "ll": "ll", "low-latency": "ll", "low_latency": "ll",
        "llhq": "llhq", "llhp": "llhp",
        "ull": "llhp", "ultra-low-latency": "llhp", "ultra_low_latency": "llhp",
        "zerolatency": "llhp", "realtime": "llhp",
        "hq": "hq", "hp": "hp",
        "lossless": "lossless", "lossless-highperf": "losslesshp",
        "bd": "bd", "high-quality": "hq", "high-performance": "hp"
    }

    allowed = {
        "default", "fast", "medium", "slow", "hp", "hq", "bd",
        "ll", "llhq", "llhp", "lossless", "losslesshp",
        "p1", "p2", "p3", "p4", "p5", "p6", "p7"
    }

    mapped = alias_map.get(preset, preset)
    return mapped if mapped in allowed else "p4"

_CPU_PRESET_ALIAS = {
    "p1": "ultrafast", "p2": "superfast", "p3": "veryfast", "p4": "fast",
    "p5": "medium", "p6": "slow", "p7": "veryslow",
    "ll": "ultrafast", "llhq": "ultrafast", "llhp": "ultrafast",
    "hq": "veryfast", "hp": "veryfast", "bd": "slow",
    "speed": "ultrafast", "balanced": "medium", "quality": "slow",
    "lossless": "veryslow", "default": "",
}
_X264_PRESETS = {
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow", "placebo",
}

def _safe_x264_preset(preset: str) -> str:
    """Map NVENC/QSV-style preset names onto libx264/libx265 presets.

    The GUI offers presets from several backends in one list; passing e.g.
    'llhp' or 'p4' straight to libx264 makes ffmpeg abort at startup.
    """
    p = (preset or "").strip().lower()
    if not p:
        return ""
    mapped = _CPU_PRESET_ALIAS.get(p, p)
    if mapped == "":
        return ""
    if mapped not in _X264_PRESETS:
        logging.warning("Preset '%s' is not a libx264/libx265 preset — using 'ultrafast'.", preset)
        return "ultrafast"
    return mapped

_X264_TUNES = {"film", "animation", "grain", "stillimage", "psnr", "ssim", "fastdecode", "zerolatency"}
_X265_TUNES = {"psnr", "ssim", "grain", "fastdecode", "zerolatency", "animation"}
_CPU_TUNE_ALIAS = {
    "zerolatency": "zerolatency", "ull": "zerolatency",
    "ultra-low-latency": "zerolatency", "ultra_low_latency": "zerolatency",
    "low-latency": "zerolatency", "low_latency": "zerolatency",
    "ll": "zerolatency", "realtime": "zerolatency",
    "performance": "zerolatency", "high-performance": "zerolatency",
    "hq": "", "high-quality": "", "quality": "", "auto": "", "default": "",
    "none": "", "lossless": "", "lossless-highperf": "", "blu-ray": "", "bluray": "",
}

def _safe_cpu_tune(codec: str, tune: str) -> str:
    """Map backend-specific tune names onto valid x264/x265 tunes ('' = default)."""
    t = (tune or "").strip().lower()
    if not t:
        return ""
    mapped = _CPU_TUNE_ALIAS.get(t, t)
    if mapped == "":
        return ""
    valid = _X265_TUNES if codec in ("h.265", "hevc") else _X264_TUNES
    if mapped not in valid:
        logging.warning("Tune '%s' not valid for %s CPU encode — using 'zerolatency'.", tune, codec)
        return "zerolatency"
    return mapped

def _norm_qp(qp):
    try:
        q = int(qp)
        return str(max(0, min(51, q)))
    except Exception:
        return ""

def _slices_count() -> int:
    """Slices per frame for VAAPI encodes (1 = whole frame; >1 confines loss to one band).

    LINUXPLAY_SLICES env var overrides the --slices flag for quick testing.
    """
    val = None
    env = os.environ.get("LINUXPLAY_SLICES")
    if env:
        try:
            val = int(env)
        except Exception:
            pass
    if val is None:
        try:
            val = int(getattr(HOST_ARGS, "slices", 4))
        except Exception:
            val = 4
    return max(1, min(16, val))

def _udp_buffer_sizes(pkt_size):
    """(fifo_size, buffer_size) for UDP outputs. Tunnels (sub-1500 path MTU)
    and Wi-Fi get the larger burst buffers — undersized buffers drop TS
    packets, which shows up as slice/macroblock artifacts on the client."""
    if host_state.net_mode in ("wifi", "vpn") or pkt_size < 1316:
        return 131072, 262144
    return 65536, 262144

def _pick_encoder_args(codec: str, hwenc: str, preset: str, gop: str, qp: str,
                       tune: str, bitrate: str, pix_fmt: str):
    codec = (codec or "h.264").lower()
    hwenc = (hwenc or "auto").lower()
    preset_l = (preset or "").strip().lower()
    tune_l = (tune or "").strip().lower()
    qp = _norm_qp(qp)
    extra_filters, enc = [], []

    def ensure(name: str) -> bool:
        ok = ffmpeg_has_encoder(name)
        if not ok:
            logging.warning("Requested encoder '%s' not found; falling back to CPU.", name)
        return ok

    if hwenc == "auto":
        if codec == "h.264":
            if has_nvidia() and ffmpeg_has_encoder("h264_nvenc"):
                hwenc = "nvenc"
            elif is_intel_cpu() and ffmpeg_has_encoder("h264_qsv"):
                hwenc = "qsv"
            elif has_vaapi() and ffmpeg_has_encoder("h264_vaapi"):
                hwenc = "vaapi"
            else:
                hwenc = "cpu"
        elif codec == "h.265":
            if has_nvidia() and ffmpeg_has_encoder("hevc_nvenc"):
                hwenc = "nvenc"
            elif is_intel_cpu() and ffmpeg_has_encoder("hevc_qsv"):
                hwenc = "qsv"
            elif has_vaapi() and ffmpeg_has_encoder("hevc_vaapi"):
                hwenc = "vaapi"
            else:
                hwenc = "cpu"
        else:
            hwenc = "cpu"

    adaptive = getattr(HOST_ARGS, "adaptive", False)
    bitrate_s = str(bitrate or "").lower()

    if "vaapi" in hwenc:
        if bitrate and str(bitrate).lower() not in ("0", "auto"):
            # Peak-constrained VBR: caps per-frame size (esp. I-frames) and total
            # datagram rate. CQP ignores the bitrate cap entirely — I-frames can
            # hit ~10x the average frame size, and on lossy paths every IDR
            # datagram is a corruption opportunity.
            dynamic_flags = [
                "-rc_mode", "VBR",
                "-b:v", bitrate,
                "-maxrate", bitrate,
                "-bufsize", bitrate,
            ]
        else:
            dynamic_flags = [
                "-rc_mode", "CQP",
                "-qp", qp or "23",
            ]

    elif "nvenc" in hwenc:
        if adaptive:
            dynamic_flags = [
                "-rc", "vbr",
                "-maxrate", bitrate or "15M",
                "-cq", qp or "23",
            ]
        else:
            dynamic_flags = [
                "-rc", "constqp",
            ] + (["-qp", qp] if qp else [])

    elif "qsv" in hwenc:
        dynamic_flags = [
            "-rc_mode", "ICQ",
            "-icq_quality", qp or "23",
        ]

    else:
        dynamic_flags = [
            "-crf", qp or "23",
        ]

    try:
        gop_val = int(gop)
        use_gop = gop_val > 0
    except Exception:
        gop_val, use_gop = 0, False

    def _nvenc_tune_args():
        if tune_l in ("zerolatency", "ull", "ultra-low-latency", "ultra_low_latency", "realtime"):
            return ["-tune", "ull"]
        if tune_l in ("low-latency", "ll", "low_latency"):
            return ["-tune", "ll"]
        if tune_l in ("hq", "film", "quality", "high_quality"):
            return ["-tune", "hq"]
        if tune_l in ("lossless",):
            return ["-tune", "lossless"]
        return ["-tune", "ll"]

    if codec == "h.264":
        if hwenc == "nvenc" and ensure("h264_nvenc"):
            enc = [
                "-c:v", "h264_nvenc",
                "-preset", _safe_nvenc_preset(preset_l or "llhq"),
                *(["-g", str(gop_val)] if use_gop else []),
                "-bf", "0", "-rc-lookahead", "0", "-refs", "1",
                "-flags2", "+fast",
                *dynamic_flags,
                "-pix_fmt", pix_fmt,
                "-bsf:v", "h264_mp4toannexb",
                *_nvenc_tune_args()
            ]

        elif hwenc == "qsv" and ensure("h264_qsv"):
            enc = ["-c:v", "h264_qsv", *dynamic_flags,
                   "-pix_fmt", pix_fmt, "-bsf:v", "h264_mp4toannexb"]

        elif hwenc == "vaapi" and has_vaapi() and ensure("h264_vaapi"):
            va_fmt = _vaapi_fmt_for_pix_fmt(pix_fmt, codec)
            extra_filters += [
                "-vf", f"format={va_fmt},hwupload",
                "-vaapi_device", "/dev/dri/renderD128"
            ]
            enc = [
                "-c:v", "h264_vaapi",
                "-bf", "0",
                *(["-g", str(gop_val)] if use_gop else []),
                *dynamic_flags,
                "-slices", str(_slices_count()),
                "-pix_fmt", pix_fmt,
                "-bsf:v", "h264_mp4toannexb"
            ]

        else:
            enc = [
                "-c:v", "libx264",
                "-preset", _safe_x264_preset(preset_l) or "ultrafast",
                "-tune", _safe_cpu_tune(codec, tune_l) or "zerolatency",
                *(["-g", str(gop_val)] if use_gop else []),
                *dynamic_flags,
                "-pix_fmt", pix_fmt,
                "-bsf:v", "h264_mp4toannexb"
            ]
            if tune_l in ("zerolatency", "ultra-low-latency", "ull", "low-latency", "ll"):
                enc += ["-x264-params", "scenecut=0"]

    elif codec == "h.265":
        if hwenc == "nvenc" and ensure("hevc_nvenc"):
            enc = [
                "-c:v", "hevc_nvenc",
                "-preset", _safe_nvenc_preset(preset_l or "p5"),
                *(["-g", str(gop_val)] if use_gop else []),
                "-bf", "0", "-rc-lookahead", "0", "-refs", "1",
                "-flags2", "+fast",
                *dynamic_flags,
                "-pix_fmt", pix_fmt,
                "-bsf:v", "hevc_mp4toannexb",
                *_nvenc_tune_args()
            ]

        elif hwenc == "qsv" and ensure("hevc_qsv"):
            enc = ["-c:v", "hevc_qsv", *dynamic_flags,
                   "-pix_fmt", pix_fmt, "-bsf:v", "hevc_mp4toannexb"]

        elif hwenc == "vaapi" and has_vaapi() and ensure("hevc_vaapi"):
            va_fmt = _vaapi_fmt_for_pix_fmt(pix_fmt, codec)
            extra_filters += [
                "-vf", f"format={va_fmt},hwupload",
                "-vaapi_device", "/dev/dri/renderD128"
            ]
            enc = [
                "-c:v", "hevc_vaapi",
                "-bf", "0",
                *(["-g", str(gop_val)] if use_gop else []),
                *dynamic_flags,
                "-slices", str(_slices_count()),
                "-pix_fmt", pix_fmt,
                "-bsf:v", "hevc_mp4toannexb"
            ]

        else:
            enc = [
                "-c:v", "libx265",
                "-preset", _safe_x264_preset(preset_l) or "ultrafast",
                "-tune", _safe_cpu_tune(codec, tune_l) or "zerolatency",
                *(["-g", str(gop_val)] if use_gop else []),
                *dynamic_flags,
                "-pix_fmt", pix_fmt,
                "-bsf:v", "hevc_mp4toannexb"
            ]
            if tune_l in ("zerolatency", "ultra-low-latency", "ull", "low-latency", "ll"):
                enc += ["-x265-params", "scenecut=0:rc-lookahead=0"]

    return extra_filters, enc

def _pick_kms_device():
    for cand in ("card0","card1","card2"):
        p = f"/dev/dri/{cand}"
        if os.path.exists(p):
            return p
    return "/dev/dri/card0"

def build_video_cmd(args, bitrate, monitor_info, video_port, portal_stream=None):
    try:
        fps_i = int(str(args.framerate))
    except Exception:
        fps_i = 60

    w, h, ox, oy = monitor_info
    preset = args.preset.strip().lower() if args.preset else ""
    gop, qp, tune, pix_fmt = args.gop, args.qp, args.tune, args.pix_fmt

    codec_name = (args.encoder if args.encoder and args.encoder.lower() != "none" else "h.264")
    min_bits = int(w) * int(h) * max(1, fps_i) * _target_bpp(codec_name, fps_i)
    cur_bits = _parse_bitrate_bits(bitrate)
    bitrate_off = str(bitrate).strip().lower() in ("0", "auto", "")
    if cur_bits < min_bits and not bitrate_off:
        logging.warning(
            "Bitrate %s is below the recommended %s for %dx%d@%dfps — honoring your "
            "setting (expect quality loss on busy scenes). --bitrate 0 = CQP quality "
            "mode; leave unset for the auto floor.",
            str(bitrate), _format_bits(int(min_bits)), w, h, fps_i
        )
    elif bitrate_off and str(bitrate).strip() not in ("0", "auto"):
        safe_str = _format_bits(int(min_bits))
        logging.warning("No bitrate set; using %s for %dx%d@%dfps.", safe_str, w, h, fps_i)
        bitrate = safe_str
        host_state.current_bitrate = safe_str

    ip = getattr(host_state, "client_ip", None)
    if not ip or not isinstance(ip, str) or ip.strip().lower() in ("none", "", "0.0.0.0"):
        logging.error(f"build_video_cmd: invalid client IP ({ip!r}) — refusing to build ffmpeg command.")
        return None

    base_in = [*(_ffmpeg_base_cmd()), *(_input_ll_flags())]
    disp = args.display
    if "." not in disp:
        disp = f"{disp}.0"

    capture_pref = (os.environ.get("LINUXPLAY_CAPTURE", "auto") or "auto").lower()
    kms_available = ffmpeg_has_device("kmsgrab")
    vaapi_available = has_vaapi()

    def _vaapi_possible_for_codec():
        enc = (args.encoder or "h.264").lower()
        return (
            (enc == "h.264" and ffmpeg_has_encoder("h264_vaapi")) or
            (enc == "h.265" and ffmpeg_has_encoder("hevc_vaapi"))
        )

    use_kms = False
    if capture_pref == "kmsgrab":
        use_kms = True
    elif capture_pref == "auto" and kms_available:
        if ((args.hwenc in ("auto", "vaapi") and vaapi_available and _vaapi_possible_for_codec())
            or (args.hwenc == "cpu")):
            use_kms = True

    session = _session_type()
    if portal_stream:
        pw, ph = int(portal_stream["w"]), int(portal_stream["h"])
        logging.info("Linux capture: portal/PipeWire node %s (%sx%s) selected (pref=%s).",
                     portal_stream.get("node"), pw, ph, capture_pref)
        input_side = [
            *base_in,
            "-f", "rawvideo",
            "-pixel_format", "bgr0",
            "-video_size", f"{pw}x{ph}",
            "-framerate", str(fps_i),
            "-i", "-",
        ]
        extra_filters, encode = _pick_encoder_args(
            codec=args.encoder, hwenc=args.hwenc, preset=preset,
            gop=gop, qp=qp, tune=tune, bitrate=bitrate, pix_fmt=pix_fmt
        )
        if any(x in encode for x in ("h264_vaapi", "hevc_vaapi")):
            extra_filters = ["-vf", "format=nv12,hwupload",
                             "-vaapi_device", "/dev/dri/renderD128"]
        else:
            extra_filters = ["-vf", f"format={pix_fmt or 'yuv420p'}"]
        output_side = _output_sync_flags()
        pkt_size = _udp_ts_pkt_size(ip)
        if pkt_size < 1316 and getattr(host_state, "net_mode", "lan") == "lan":
            logging.info("Tunneled path detected (pkt_size %d) — using larger UDP buffers.", pkt_size)
        fifo_size, buffer_size = _udp_buffer_sizes(pkt_size)
        out = [
            *(_mpegts_ll_mux_flags()),
            "-flags", "+low_delay",
            "-f", "mpegts",
            *_marker_opt(),
            (
                f"udp://{ip}:{video_port}"
                f"?pkt_size={pkt_size}"
                f"&buffer_size={buffer_size}"
                f"&fifo_size={fifo_size}"
                f"&overrun_nonfatal=1"
                f"&max_delay=0"
            ),
        ]
        return input_side + output_side + (extra_filters or []) + encode + out

    if session == "wayland" and not use_kms:
        if capture_pref == "auto" and kms_available:
            logging.warning("Wayland session: forcing kmsgrab capture (x11grab only sees XWayland windows).")
            use_kms = True
        elif capture_pref == "auto":
            raise RuntimeError(
                "Wayland session detected but neither portal capture nor kmsgrab is available. "
                "Native Wayland windows cannot be captured with x11grab; "
                "install gst-launch-1.0 with the pipewiresrc plugin (gst-plugin-pipewire) "
                "and jeepney, or run the host under X11."
            )
        else:
            logging.warning(
                "LINUXPLAY_CAPTURE=%s forced under Wayland: native Wayland windows will NOT appear in the stream.",
                capture_pref,
            )
    if use_kms and session == "wayland" and not _kmsgrab_perms_ok():
        logging.warning(
            "kmsgrab under Wayland needs elevated capture permissions; if capture fails, run:\n"
            "    sudo setcap cap_sys_admin+ep \"$(command -v ffmpeg)\""
        )
    if use_kms:
        kms_dev = os.environ.get("LINUXPLAY_KMS_DEVICE", _pick_kms_device())
        logging.info("Linux capture: kmsgrab (%s) selected (pref=%s).", kms_dev, capture_pref)
        input_side = [
            *base_in,
            "-f", "kmsgrab",
            "-framerate", str(fps_i),
            "-device", kms_dev,
            "-i", "-",
        ]

        extra_filters, encode = _pick_encoder_args(
            codec=args.encoder, hwenc=args.hwenc, preset=preset,
            gop=gop, qp=qp, tune=tune, bitrate=bitrate, pix_fmt=pix_fmt
        )

        if any(x in encode for x in ("h264_vaapi", "hevc_vaapi")):
            _vaapi_fmt = {
                "nv12": "nv12", "yuv420p": "nv12",
                "p010": "p010", "yuv420p10": "p010"
            }.get((pix_fmt or "nv12").lower(), "nv12")
            extra_filters = ["-vf", f"hwmap=derive_device=vaapi,scale_vaapi=w={w}:h={h}:format={_vaapi_fmt}",
                             "-vaapi_device", "/dev/dri/renderD128"]
        elif args.hwenc == "cpu":
            extra_filters = ["-vf", f"hwdownload,format={pix_fmt or 'yuv420p'}"]

    else:
        logging.info("Linux capture: x11grab selected (pref=%s, kms=%s).", capture_pref, kms_available)
        input_arg = f"{disp}+{ox},{oy}"
        input_side = [
            *base_in,
            "-f", "x11grab",
            "-draw_mouse", "0",
            "-framerate", str(fps_i),
            "-video_size", f"{w}x{h}",
            "-i", input_arg,
        ]
        extra_filters, encode = _pick_encoder_args(
            codec=args.encoder, hwenc=args.hwenc, preset=preset,
            gop=gop, qp=qp, tune=tune, bitrate=bitrate, pix_fmt=pix_fmt
        )

    output_side = _output_sync_flags()
    pkt_size = _udp_ts_pkt_size(ip)
    if pkt_size < 1316 and getattr(host_state, "net_mode", "lan") == "lan":
        logging.info("Tunneled path detected (pkt_size %d) — using larger UDP buffers.", pkt_size)
    fifo_size, buffer_size = _udp_buffer_sizes(pkt_size)

    out = [
        *(_mpegts_ll_mux_flags()),
        "-flags", "+low_delay",
        "-f", "mpegts",
        *_marker_opt(),
        (
            f"udp://{ip}:{video_port}"
            f"?pkt_size={pkt_size}"
            f"&buffer_size={buffer_size}"
            f"&fifo_size={fifo_size}"
            f"&overrun_nonfatal=1"
            f"&max_delay=0"
        ),
    ]

    full_cmd = input_side + output_side + (extra_filters or []) + encode + out
    return full_cmd

def build_audio_cmd():
    opus_app = os.environ.get("LP_OPUS_APP", "voip")
    opus_fd  = os.environ.get("LP_OPUS_FD", "10")

    net_mode = getattr(host_state, "net_mode", "lan")
    aud_pkt = _udp_ts_pkt_size(str(host_state.client_ip))
    tunneled = aud_pkt < 1316
    aud_buf = "4194304" if (net_mode in ("wifi", "vpn") or tunneled) else "1048576"
    aud_delay = "150000" if (net_mode in ("wifi", "vpn") or tunneled) else "0"

    mon = os.environ.get("PULSE_MONITOR", "")
    if not mon and which("pactl"):
        try:
            out = subprocess.check_output(
                ["pactl", "list", "short", "sources"],
                text=True,
                stderr=subprocess.DEVNULL
            )
            best = None
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) >= 5:
                    name, state = parts[1], parts[4].upper()
                    if ".monitor" in name:
                        if state == "RUNNING":
                            best = name
                            break
                        elif state == "IDLE" and not best:
                            best = name
            if best:
                mon = best
        except Exception as e:
            logging.warning("PulseAudio monitor detection failed: %s", e)

    if not mon:
        mon = "default.monitor"
    elif not mon.endswith(".monitor"):
        mon += ".monitor"

    logging.info("Using PulseAudio source: %s", mon)

    channels = 2
    if which("pactl"):
        try:
            src_info = subprocess.check_output(
                ["pactl", "list", "sources"], text=True, stderr=subprocess.DEVNULL
            )
            name_idx = src_info.find(f"Name: {mon}")
            if name_idx != -1:
                m = re.search(r"\s(\d+)ch\s", src_info[name_idx:name_idx + 2000])
                if m:
                    channels = int(m.group(1))
        except Exception as e:
            logging.warning("Audio channel detection failed: %s", e)
    if channels not in [1, 2, 6, 8]:
        channels = 2

    logging.info("Detected %s channel(s): %s", channels, "Surround" if channels > 2 else "Stereo")

    input_side = [
        *(_ffmpeg_base_cmd()),
        *(_input_ll_flags()),
        "-f", "pulse",
        "-i", mon,
        "-ac", str(channels),
    ]

    output_side = _output_sync_flags()

    encode = [
        "-c:a", "libopus",
        "-b:a", "384k" if channels > 2 else "128k",
        "-application", opus_app,
        "-frame_duration", opus_fd,
    ]

    out = [
        *(_mpegts_ll_mux_flags()),
        *_marker_opt(),
        "-f", "mpegts",
        f"udp://{host_state.client_ip}:{UDP_AUDIO_PORT}"
        f"?pkt_size={aud_pkt}&buffer_size={aud_buf}&overrun_nonfatal=1&max_delay={aud_delay}"
    ]

    return input_side + output_side + encode + out

def _virtual_screen_size():
    mons = getattr(host_state, "monitors", None)
    if not mons:
        try:
            mons = detect_monitors()
        except Exception:
            mons = []
    if not mons:
        return 1920, 1080
    return max(m[0] + m[2] for m in mons), max(m[1] + m[3] for m in mons)

class _UInputInjector:
    """Virtual input devices via evdev/uinput (mouse + keyboard).

    Events are injected at the kernel input layer, so this works identically
    on X11 and Wayland — no xdotool/X-server access needed. Mirrors the
    device split used by other streaming hosts: an absolute pointer
    (position + buttons), a wheel-only relative device, and a keyboard.
    """

    def __init__(self):
        if not HAVE_UINPUT:
            raise RuntimeError("evdev/UInput not available")
        from evdev import UInput, ecodes, AbsInfo
        ec = self._ec = ecodes

        self._key_map = {
            "Escape": ec.KEY_ESC, "Tab": ec.KEY_TAB, "BackSpace": ec.KEY_BACKSPACE,
            "Return": ec.KEY_ENTER, "Insert": ec.KEY_INSERT, "Delete": ec.KEY_DELETE,
            "Pause": ec.KEY_PAUSE, "Print": getattr(ec, "KEY_PRINT", ec.KEY_SYSRQ),
            "Home": ec.KEY_HOME, "End": ec.KEY_END,
            "Left": ec.KEY_LEFT, "Right": ec.KEY_RIGHT, "Up": ec.KEY_UP, "Down": ec.KEY_DOWN,
            "Page_Up": ec.KEY_PAGEUP, "Page_Down": ec.KEY_PAGEDOWN,
            "Shift_L": ec.KEY_LEFTSHIFT, "Shift_R": ec.KEY_RIGHTSHIFT,
            "Control_L": ec.KEY_LEFTCTRL, "Control_R": ec.KEY_RIGHTCTRL,
            "Super_L": ec.KEY_LEFTMETA, "Super_R": ec.KEY_RIGHTMETA,
            "Alt_L": ec.KEY_LEFTALT, "Alt_R": ec.KEY_RIGHTALT,
            "Caps_Lock": ec.KEY_CAPSLOCK, "Num_Lock": ec.KEY_NUMLOCK,
            "Scroll_Lock": ec.KEY_SCROLLLOCK, "space": ec.KEY_SPACE,
            **{f"F{i}": getattr(ec, f"KEY_F{i}") for i in range(1, 13)},
        }
        base_chars = {
            "-": ec.KEY_MINUS, "=": ec.KEY_EQUAL, "[": ec.KEY_LEFTBRACE,
            "]": ec.KEY_RIGHTBRACE, "\\": ec.KEY_BACKSLASH, ";": ec.KEY_SEMICOLON,
            "'": ec.KEY_APOSTROPHE, "`": ec.KEY_GRAVE, ",": ec.KEY_COMMA,
            ".": ec.KEY_DOT, "/": ec.KEY_SLASH,
        }
        shifted_src = {
            "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6",
            "&": "7", "*": "8", "(": "9", ")": "0", "_": "-", "+": "=",
            "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "~": "`",
            "<": ",", ">": ".", "?": "/",
        }
        def _base_key(ch):
            if ch in base_chars:
                return base_chars[ch]
            if ch.isascii() and ch.isalpha():
                return ec.KEY_A + (ord(ch.lower()) - ord("a"))
            if ch.isdigit() and ch.isascii():
                return ec.KEY_1 + (int(ch) + 9) % 10   # '1'..'9' -> KEY_1..KEY_9, '0' -> KEY_0
            return None
        def _char_to_key(ch):
            if ch in base_chars:
                return base_chars[ch], False
            if ch in shifted_src:
                code = _base_key(shifted_src[ch])
                if code is not None:
                    return code, True
            if ch.isascii() and ch.isalpha():
                return ec.KEY_A + (ord(ch.lower()) - ord("a")), ch.isupper()
            if ch.isdigit() and ch.isascii():
                return (ec.KEY_1 + (int(ch) + 9) % 10), False
            return None, False
        self._char_to_key = _char_to_key

        key_caps = set(self._key_map.values()) | set(base_chars.values())
        key_caps |= {ec.KEY_A + i for i in range(26)}
        key_caps |= {ec.KEY_1 + i for i in range(10)}
        key_caps |= {ec.KEY_SPACE, ec.KEY_LEFTSHIFT, ec.KEY_RIGHTSHIFT}

        self._vw, self._vh = _virtual_screen_size()
        self.kbd = UInput(
            {ec.EV_KEY: sorted(key_caps)},
            name="LinuxPlay Virtual Keyboard",
        )
        self.abs_mouse = UInput(
            {
                ec.EV_KEY: [ec.BTN_LEFT, ec.BTN_MIDDLE, ec.BTN_RIGHT],
                ec.EV_ABS: [
                    (ec.ABS_X, AbsInfo(0, 0, max(0, self._vw - 1), 0, 0, 0)),
                    (ec.ABS_Y, AbsInfo(0, 0, max(0, self._vh - 1), 0, 0, 0)),
                ],
            },
            name="LinuxPlay Virtual Pointer",
        )
        self.wheel_dev = UInput(
            {ec.EV_REL: [ec.REL_WHEEL, ec.REL_HWHEEL]},
            name="LinuxPlay Virtual Wheel",
        )
        self._btn_map = {"1": ec.BTN_LEFT, "2": ec.BTN_MIDDLE, "3": ec.BTN_RIGHT}
        self._wheel_map = {
            "4": (ec.REL_WHEEL, 1), "5": (ec.REL_WHEEL, -1),
            "6": (ec.REL_HWHEEL, -1), "7": (ec.REL_HWHEEL, 1),
        }
        self._auto_shift_keys = set()
        self._shift_pressed = False
        logging.info("uinput virtual devices created; virtual screen %dx%d.", self._vw, self._vh)

    def _resolve_key(self, name):
        if isinstance(name, str) and name in self._key_map:
            return self._key_map[name], False
        if isinstance(name, str) and len(name) == 1:
            return self._char_to_key(name)
        return None, False

    def mouse_abs(self, x, y):
        ec = self._ec
        try:
            x = max(0, min(int(x), self._vw - 1))
            y = max(0, min(int(y), self._vh - 1))
            self.abs_mouse.write(ec.EV_ABS, ec.ABS_X, x)
            self.abs_mouse.write(ec.EV_ABS, ec.ABS_Y, y)
            self.abs_mouse.syn()
            return True
        except Exception as e:
            logging.debug("uinput mouse_abs failed: %s", e)
            return False

    def mouse_button(self, btn, down):
        ec = self._ec
        code = self._btn_map.get(str(btn))
        if code is None:
            return False
        try:
            self.abs_mouse.write(ec.EV_KEY, code, 1 if down else 0)
            self.abs_mouse.syn()
            return True
        except Exception as e:
            logging.debug("uinput mouse_button failed: %s", e)
            return False

    def wheel(self, btn):
        ec = self._ec
        entry = self._wheel_map.get(str(btn))
        if entry is None:
            return False
        axis, val = entry
        try:
            self.wheel_dev.write(ec.EV_REL, axis, val)
            self.wheel_dev.syn()
            return True
        except Exception as e:
            logging.debug("uinput wheel failed: %s", e)
            return False

    def key(self, action, name):
        ec = self._ec
        code, needs_shift = self._resolve_key(name)
        if code is None:
            return False
        down = (action == "down")
        try:
            if needs_shift:
                if down:
                    if not self._shift_pressed:
                        self.kbd.write(ec.EV_KEY, ec.KEY_LEFTSHIFT, 1)
                        self._shift_pressed = True
                    self.kbd.write(ec.EV_KEY, code, 1)
                    self.kbd.syn()
                    self._auto_shift_keys.add(code)
                else:
                    self.kbd.write(ec.EV_KEY, code, 0)
                    self.kbd.syn()
                    self._auto_shift_keys.discard(code)
                    if not self._auto_shift_keys and self._shift_pressed:
                        self.kbd.write(ec.EV_KEY, ec.KEY_LEFTSHIFT, 0)
                        self._shift_pressed = False
                        self.kbd.syn()
            else:
                self.kbd.write(ec.EV_KEY, code, 1 if down else 0)
                self.kbd.syn()
            return True
        except Exception as e:
            logging.debug("uinput key failed for %r: %s", name, e)
            return False

_uinput_injector = None
_uinput_injector_lock = threading.Lock()

def _get_uinput_injector():
    """Lazily create the uinput injector; None => fall back to legacy paths.

    Failure is cached as False so we don't retry (and log) on every packet.
    """
    global _uinput_injector
    if _uinput_injector is None and HAVE_UINPUT:
        with _uinput_injector_lock:
            if _uinput_injector is None:
                try:
                    _uinput_injector = _UInputInjector()
                except Exception as e:
                    logging.warning("uinput injection unavailable (%s); using legacy input path.", e)
                    _uinput_injector = False
    return _uinput_injector or None

def _inject_mouse_move(x,y):
    inj = _get_uinput_injector()
    if inj and inj.mouse_abs(x, y):
        return
    if HAVE_PYNPUT:
        try: _mouse.position = (int(x), int(y))
        except Exception as e: logging.debug("pynput move failed: %s", e)
    elif IS_LINUX:
        subprocess.Popen(["xdotool","mousemove",str(x),str(y)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def _inject_mouse_down(btn):
    inj = _get_uinput_injector()
    if inj and inj.mouse_button(btn, True):
        return
    if HAVE_PYNPUT:
        b = {"1": Button.left, "2": Button.middle, "3": Button.right}.get(btn, Button.left)
        try: _mouse.press(b)
        except Exception as e: logging.debug("pynput mousedown failed: %s", e)
    elif IS_LINUX:
        subprocess.Popen(["xdotool","mousedown",btn], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def _inject_mouse_up(btn):
    inj = _get_uinput_injector()
    if inj and inj.mouse_button(btn, False):
        return
    if HAVE_PYNPUT:
        b = {"1": Button.left, "2": Button.middle, "3": Button.right}.get(btn, Button.left)
        try: _mouse.release(b)
        except Exception as e: logging.debug("pynput mouseup failed: %s", e)
    elif IS_LINUX:
        subprocess.Popen(["xdotool","mouseup",btn], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def _inject_scroll(btn):
    inj = _get_uinput_injector()
    if inj and inj.wheel(btn):
        return
    if HAVE_PYNPUT:
        try:
            if btn == "4": _mouse.scroll(0, +1)
            elif btn == "5": _mouse.scroll(0, -1)
            elif btn == "6": _mouse.scroll(-1, 0)
            elif btn == "7": _mouse.scroll(+1, 0)
        except Exception as e:
            logging.debug("pynput scroll failed: %s", e)
    elif IS_LINUX:
        subprocess.Popen(["xdotool","click",btn], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

_key_map = {
    "Escape": Key.esc if HAVE_PYNPUT else None, "Tab": Key.tab if HAVE_PYNPUT else None, "BackSpace": Key.backspace if HAVE_PYNPUT else None,
    "Return": Key.enter if HAVE_PYNPUT else None, "Insert": Key.insert if HAVE_PYNPUT else None, "Delete": Key.delete if HAVE_PYNPUT else None,
    "Home": Key.home if HAVE_PYNPUT else None, "End": Key.end if HAVE_PYNPUT else None,
    "Left": Key.left if HAVE_PYNPUT else None, "Up": Key.up if HAVE_PYNPUT else None, "Right": Key.right if HAVE_PYNPUT else None, "Down": Key.down if HAVE_PYNPUT else None,
    "Page_Up": Key.page_up if HAVE_PYNPUT else None, "Page_Down": Key.page_down if HAVE_PYNPUT else None,
    "Shift_L": Key.shift if HAVE_PYNPUT else None, "Control_L": Key.ctrl if HAVE_PYNPUT else None,
    "Alt_L": Key.alt if HAVE_PYNPUT else None, "Alt_R": (Key.alt_gr if HAVE_PYNPUT and hasattr(Key,"alt_gr") else (Key.alt if HAVE_PYNPUT else None)),
    "Super_L": (Key.cmd if HAVE_PYNPUT else None), "Caps_Lock": (Key.caps_lock if HAVE_PYNPUT else None),
    "F1": Key.f1 if HAVE_PYNPUT else None, "F2": Key.f2 if HAVE_PYNPUT else None, "F3": Key.f3 if HAVE_PYNPUT else None,
    "F4": Key.f4 if HAVE_PYNPUT else None, "F5": Key.f5 if HAVE_PYNPUT else None, "F6": Key.f6 if HAVE_PYNPUT else None,
    "F7": Key.f7 if HAVE_PYNPUT else None, "F8": Key.f8 if HAVE_PYNPUT else None, "F9": Key.f9 if HAVE_PYNPUT else None,
    "F10": Key.f10 if HAVE_PYNPUT else None, "F11": Key.f11 if HAVE_PYNPUT else None, "F12": Key.f12 if HAVE_PYNPUT else None,
    "space": Key.space if HAVE_PYNPUT else None,
}

CHAR_TO_X11 = {
    '-':'minus', '=':'equal', '[':'bracketleft', ']':'bracketright', '\\':'backslash',
    ';':'semicolon', "'":'apostrophe', ',':'comma', '.':'period', '/':'slash', '`':'grave',
    '!':'exclam', '"':'quotedbl', '#':'numbersign', '$':'dollar', '%':'percent',
    '&':'ampersand', '*':'asterisk', '(':'parenleft', ')':'parenright', '_':'underscore',
    '+':'plus', '{':'braceleft', '}':'braceright', '|':'bar', ':':'colon',
    '<':'less', '>':'greater', '?':'question', '£':'sterling', '¬':'notsign', '¦':'brokenbar',
}

NAME_TO_CHAR = {
    'minus':'-', 'equal':'=', 'bracketleft':'[', 'bracketright':']', 'backslash':'\\',
    'semicolon':';', 'apostrophe':"'", 'comma':',', 'period':'.', 'slash':'/', 'grave':'`',
    'exclam':'!', 'quotedbl':'"', 'numbersign':'#', 'dollar':'$', 'percent':'%',
    'ampersand':'&', 'asterisk':'*', 'parenleft':'(', 'parenright':')', 'underscore':'_',
    'plus':'+', 'braceleft':'{', 'braceright':'}', 'bar':'|', 'colon':':',
    'less':'<', 'greater':'>', 'question':'?', 'sterling':'£', 'notsign':'¬', 'brokenbar':'¦',
}

def _inject_key(action, name):
    inj = _get_uinput_injector()
    if inj and inj.key(action, name):
        return
    if HAVE_PYNPUT:
        k = _key_map.get(name)
        try:
            if k:
                (_keys.press if action == "down" else _keys.release)(k)
                return

            if isinstance(name, str) and len(name) == 1:
                (_keys.press if action == "down" else _keys.release)(name)
                return

            ch = NAME_TO_CHAR.get(name)
            if ch:
                (_keys.press if action == "down" else _keys.release)(ch)
                return
        except Exception as e:
            logging.debug("pynput key %s failed for %r: %s", action, name, e)
        return

    if IS_LINUX:
        try:
            keyname = name
            if isinstance(name, str) and len(name) == 1:
                keyname = CHAR_TO_X11.get(name, name)
            cmd = ["xdotool", "keydown" if action == "down" else "keyup", keyname]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            logging.debug("xdotool key %s failed for %r: %s", action, name, e)

def _send_and_close(conn, payload: bytes):
    try:
        conn.sendall(payload)
        conn.shutdown(socket.SHUT_WR)
        time.sleep(0.05)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _monitors_payload():
    return ";".join(
        f"{w}x{h}+{ox}+{oy}" for (w, h, ox, oy) in host_state.monitors
    ) if host_state.monitors else DEFAULT_RES

def _begin_session_locked(peer_ip, encoder_str):
    """Mark the session active and mint a fresh session token.

    Caller must hold host_state.pin_lock. Returns the response text to send.
    """
    token = secrets.token_hex(16)
    host_state.session_active = True
    host_state.authed_client_ip = peer_ip
    host_state.pin_expiry = 0
    host_state.last_pong_ts = time.time()
    host_state.session_token = token
    host_state.client_ip = peer_ip
    host_state.session_epoch += 1
    return f"OK:{encoder_str}:{_monitors_payload()}\nTOKEN {token}"

def _handle_certfp_handshake(conn, peer_ip, fp_hex, encoder_str):
    if host_state.session_active:
        logging.warning(f"[AUTH] Rejected CERTFP client {peer_ip} — active session already running.")
        _send_and_close(conn, b"BUSY:ACTIVESESSION")
        return
    if not fp_hex or not _verify_fingerprint_trusted(fp_hex):
        logging.warning(f"[AUTH] Rejected cert from {peer_ip} — not trusted.")
        _send_and_close(conn, b"FAIL:UNTRUSTEDCERT")
        return
    if not HAVE_CRYPTO:
        logging.warning("[AUTH] cryptography unavailable — cannot verify cert proof; use PIN.")
        _send_and_close(conn, b"FAIL:UNTRUSTEDCERT")
        return

    nonce = secrets.token_bytes(32)
    try:
        conn.sendall(b"CHALLENGE " + nonce.hex().encode("utf-8"))
        resp = conn.recv(8192).decode("utf-8", errors="replace").strip()
    except Exception as e:
        logging.warning(f"[AUTH] Challenge exchange with {peer_ip} failed: {e}")
        _send_and_close(conn, b"FAIL:UNTRUSTEDCERT")
        return

    parts = resp.split()
    if (len(parts) == 4 and parts[0] == "CERT" and parts[2] == "SIG"
            and _verify_client_proof(fp_hex, parts[1], parts[3], nonce)):
        with host_state.pin_lock:
            if host_state.session_active:
                _send_and_close(conn, b"BUSY:ACTIVESESSION")
                return
            reply = _begin_session_locked(peer_ip, encoder_str)
        _send_and_close(conn, reply.encode("utf-8"))
        set_status(f"Client (cert): {host_state.client_ip}")
        set_pin_display(f"Session live: {peer_ip} — PIN paused")
        logging.info(f"[AUTH] Client {peer_ip} authenticated via certificate proof.")
    else:
        logging.warning(f"[AUTH] Certificate proof from {peer_ip} failed verification.")
        _send_and_close(conn, b"FAIL:UNTRUSTEDCERT")

def _handle_pin_handshake(conn, parts, peer_ip, encoder_str, raw=""):
    provided_pin = parts[1] if len(parts) >= 2 else ""

    locked_for = _pin_lockout_remaining(peer_ip)
    if locked_for > 0:
        logging.warning("[AUTH] %s is locked out for another %.0fs after repeated bad PINs.",
                        peer_ip, locked_for)
        _send_and_close(conn, b"FAIL:BADPIN")
        return

    reply = None
    with host_state.pin_lock:
        expected = str(host_state.pin_code or "")
        guess_ok = (bool(expected) and isinstance(provided_pin, str)
                    and provided_pin.isascii() and provided_pin.isdigit()
                    and len(provided_pin) == PIN_LENGTH
                    and hmac.compare_digest(provided_pin, expected))
        if (not host_state.session_active
                and guess_ok
                and time.time() < host_state.pin_expiry):
            reply = _begin_session_locked(peer_ip, encoder_str)

    if reply is not None:
        _pin_clear_failures(peer_ip)
        logging.info(f"[AUTH] Client {peer_ip} authenticated — PIN invalidated and rotation paused.")

        cert_line = ""
        keyreq_b64 = ""
        for line in (raw or "").splitlines()[1:]:
            if line.startswith("KEYREQ "):
                keyreq_b64 = line.split(None, 1)[1].strip()
                break

        # Signing a client key mints a long-lived credential, so the local user
        # confirms it when a GUI is up (headless hosts auto-approve). A client
        # that already holds a certificate never reaches this path.
        if keyreq_b64 and not request_pair_approval(peer_ip):
            logging.warning(f"[AUTH] Pairing declined for {peer_ip}; session stays PIN-only.")
            _send_and_close(conn, reply.encode("utf-8"))
            set_status(f"Client: {host_state.client_ip} (pairing declined)")
            set_pin_display(f"Session live: {peer_ip} — PIN paused")
            return
        if keyreq_b64:
            issued = _issue_client_cert(client_name="linuxplay-client",
                                        export_hint_ip=peer_ip, public_key_pem=keyreq_b64)
            if issued:
                cert_line = "\nCERT " + base64.b64encode(issued["cert_pem"]).decode("ascii")
                logging.info(f"[AUTH] Issued in-place certificate to {peer_ip} (client-held key).")
            else:
                logging.warning(f"[AUTH] KEYREQ issuance failed for {peer_ip}; exporting bundle instead.")
                _issue_client_cert(client_name="linuxplay-client", export_hint_ip=peer_ip)
        else:
            _issue_client_cert(client_name="linuxplay-client", export_hint_ip=peer_ip)
        threading.Thread(target=lambda: pin_rotate_if_needed(force=True), daemon=True).start()
        _send_and_close(conn, (reply + cert_line).encode("utf-8"))
        set_status(f"Client: {host_state.client_ip}")
        set_pin_display(f"Session live: {peer_ip} — PIN paused")
        logging.info(f"Client {peer_ip} handshake complete (PIN locked).")
    else:
        if host_state.session_active:
            logging.warning(f"[AUTH] {peer_ip} attempted reuse of consumed PIN (session active).")
            _send_and_close(conn, b"BUSY:ACTIVESESSION")
        else:
            logging.warning(f"[AUTH] Rejected {peer_ip}: invalid or expired PIN.")
            _send_and_close(conn, b"FAIL:BADPIN")
            _pin_note_failure(peer_ip)

def _handle_handshake_conn(conn, peer_ip, encoder_str, args):
    conn.settimeout(5.0)
    try:
        try:
            raw = conn.recv(4096).decode("utf-8", errors="replace").strip()
        except (socket.timeout, OSError):
            return
        parts = (raw or "").split()
        cmd = parts[0] if parts else ""

        if host_state.session_active and host_state.authed_client_ip and peer_ip != host_state.authed_client_ip:
            logging.warning(f"Rejected handshake from {peer_ip}: active session with {host_state.authed_client_ip}")
            _send_and_close(conn, b"BUSY:ACTIVESESSION")
            return

        if cmd == "HELLO" and len(parts) >= 2 and parts[1].startswith("CERTFP:"):
            fp_hex = parts[1][len("CERTFP:"):].strip().upper()
            _handle_certfp_handshake(conn, peer_ip, fp_hex, encoder_str)
            return

        if cmd == "HELLO":
            _handle_pin_handshake(conn, parts, peer_ip, encoder_str, raw=raw)
            return

        _send_and_close(conn, b"FAIL")
    except Exception as e:
        # One malformed or hostile connection must never stop the host.
        logging.error("Handshake handler error: %s", e)
        try:
            _send_and_close(conn, b"FAIL")
        except Exception:
            pass

def tcp_handshake_server(sock, encoder_str, args):
    logging.info("TCP handshake server on %d", TCP_HANDSHAKE_PORT)
    set_status("Waiting for client handshake…")

    _ensure_ca()
    _harden_secret_files()

    while not host_state.should_terminate:
        try:
            conn, addr = sock.accept()
        except OSError:
            break
        peer_ip = addr[0]
        logging.info(f"Handshake from {peer_ip}")
        # Each connection gets its own thread + timeout, so one stalled
        # client can never block the listener for everyone else.
        threading.Thread(
            target=_handle_handshake_conn, args=(conn, peer_ip, encoder_str, args),
            daemon=True, name=f"Handshake-{peer_ip}",
        ).start()

def start_streams_for_current_client(args):
    ip = getattr(host_state, "client_ip", None)
    if not ip:
        logging.warning("start_streams_for_current_client: no valid client IP — waiting for handshake.")
        return

    with host_state.video_thread_lock:
        if host_state.starting_streams:
            logging.debug("start_streams_for_current_client: already starting; skipping duplicate call.")
            return
        if host_state.video_threads:
            logging.debug("start_streams_for_current_client: video threads already active; skipping.")
            return

        if getattr(host_state, "last_disconnect_ts", 0) > 0:
            elapsed = time.time() - host_state.last_disconnect_ts
            if elapsed < 2.0:
                logging.debug(f"start_streams_for_current_client: cooldown {elapsed:.2f}s — skipping restart.")
                return

        host_state.starting_streams = True
        try:
            host_state.video_threads = {}
            portal = None
            if getattr(host_state, "capture_mode", "x11grab") == "portal":
                portal = getattr(host_state, "portal", None)
                if portal is None:
                    portal = portal_capture.PortalCapture()
                    host_state.portal = portal
                try:
                    portal.ensure(multiple=True)
                except Exception as e:
                    logging.error("Portal screen capture could not start: %s", e)
                    set_status("Portal capture failed or declined")
                    return
            for i, mon in enumerate(host_state.monitors):
                ps = portal.match_stream(i, mon) if portal else None
                cmd = build_video_cmd(args, host_state.current_bitrate, mon, UDP_VIDEO_PORT + i,
                                      portal_stream=ps)
                if not cmd:
                    logging.error(f"Failed to build video cmd for monitor {i}; skipping.")
                    continue
                feeder = portal_capture.build_feeder_cmd(ps) if (portal and ps) else None
                t = StreamThread(cmd, f"Video {i}", feeder_cmd=feeder)
                t.start()
                host_state.video_threads[i] = t

            if args.audio == "enable" and not host_state.audio_thread:
                ac = build_audio_cmd()
                if ac:
                    host_state.audio_thread = StreamThread(ac, "Audio")
                    host_state.audio_thread.start()
        except Exception as e:
            logging.error(f"start_streams_for_current_client: exception while starting — {e}")
        finally:
            host_state.starting_streams = False

def control_listener(sock):
    logging.info("Control listener UDP %d", UDP_CONTROL_PORT)
    error_streak = 0
    while not host_state.should_terminate:
        try:
            data, addr = sock.recvfrom(2048)
            error_streak = 0
            if not addr:
                # The socket was closed under us during shutdown: a blocked
                # recvfrom then returns (b"", None) instead of raising.
                if host_state.should_terminate:
                    break
                continue
            peer_ip = addr[0]

            if not host_state.session_active:
                logging.debug(f"Ignoring control packet from {peer_ip} (no active session)")
                continue

            if host_state.authed_client_ip:
                if peer_ip != host_state.authed_client_ip:
                    logging.warning(f"Rejected control packet from {peer_ip} — active client: {host_state.authed_client_ip}")
                    continue
            else:
                logging.debug(f"Ignoring early control packet from {peer_ip} (auth IP not yet set)")
                continue

            msg = data.decode("utf-8", errors="ignore").strip()
            if not msg:
                continue

            authed = _extract_authed_cmd(msg)
            if authed is None:
                logging.debug(f"Dropped control packet from {peer_ip} (bad/missing AUTH token)")
                continue

            tokens = authed.split()
            cmd = tokens[0].upper() if tokens else ""

            if cmd == "NET" and len(tokens) >= 2:
                mode = tokens[1].strip().lower()
                if mode in ("wifi", "lan", "vpn"):
                    old = getattr(host_state, "net_mode", "lan")
                    if mode != old:
                        logging.info(f"Network mode switch requested: {old} → {mode}")
                        host_state.net_mode = mode
                        try:
                            stop_streams_only()
                            # stop_streams_only() arms the reconnect cooldown, but
                            # this restart is deliberate: clear it so the switch
                            # does not black out the screen for two seconds.
                            host_state.last_disconnect_ts = 0.0
                            if HOST_ARGS:
                                start_streams_for_current_client(HOST_ARGS)
                        except Exception as e:
                            logging.error(f"Restart after NET failed: {e}")
                continue

            elif cmd == "GOODBYE":
                logging.info(f"Client at {peer_ip} disconnected cleanly.")
                try:
                    stop_streams_only()
                    host_state.client_ip = None
                    host_state.starting_streams = False
                    host_state.session_active = False
                    host_state.authed_client_ip = None
                    host_state.session_token = None
                    host_state.session_epoch += 1
                    set_status("Client disconnected — waiting for connection…")
                    logging.debug("All streams stopped after GOODBYE.")

                    pin_rotate_if_needed(force=True)
                    logging.info("[AUTH] Client disconnected — PIN rotation resumed.")

                    time.sleep(RECONNECT_COOLDOWN)
                except Exception as e:
                    logging.error(f"Error handling GOODBYE cleanup: {e}")
                continue
            elif cmd == "WINDOW_CLOSE":
                try:
                    idx = int(tokens[1]) if len(tokens) >= 2 else -1
                except Exception:
                    idx = -1
                if isinstance(host_state.video_threads, dict) and idx in host_state.video_threads:
                    try:
                        t = host_state.video_threads.pop(idx)
                        t.stop()
                        t.join(timeout=2)
                        logging.info(f"Stopped video stream for monitor {idx} on client request.")
                    except Exception as e:
                        logging.debug(f"Error stopping video stream {idx}: {e}")
                else:
                    logging.debug(f"Ignored WINDOW_CLOSE for unknown monitor {idx}.")

            elif cmd == "MOUSE_PKT" and len(tokens) == 5:
                try:
                    pkt_type = int(tokens[1])
                    bmask = int(tokens[2])
                    x = int(tokens[3])
                    y = int(tokens[4])
                except ValueError:
                    continue

                _inject_mouse_move(x, y)

                if pkt_type == 1:
                    if bmask & 1: _inject_mouse_down("1")
                    if bmask & 2: _inject_mouse_down("2")
                    if bmask & 4: _inject_mouse_down("3")
                elif pkt_type == 3:
                    if bmask & 1: _inject_mouse_up("1")
                    if bmask & 2: _inject_mouse_up("2")
                    if bmask & 4: _inject_mouse_up("3")

            elif cmd == "MOUSE_SCROLL" and len(tokens) == 2:
                _inject_scroll(tokens[1])

            elif cmd == "KEY_PRESS" and len(tokens) == 2:
                _inject_key("down", tokens[1])
            elif cmd == "KEY_RELEASE" and len(tokens) == 2:
                _inject_key("up", tokens[1])

        except OSError:
            break
        except Exception as e:
            error_streak += 1
            _warn_throttled("control-listener", f"Control listener error: {e}", exc_info=True)
            if error_streak > 50:
                logging.critical("Control listener stopped after %d consecutive errors: %s",
                                 error_streak, e)
                break
            time.sleep(0.05)

def clipboard_monitor_host():
    if not HAVE_PYPERCLIP:
        logging.info("pyperclip not available; host clipboard sync disabled.")
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while not host_state.should_terminate:
        current = ""
        try:
            current = (pyperclip.paste() or "").strip()
        except Exception:
            pass
        with host_state.clipboard_lock:
            if (not host_state.ignore_clipboard_update and current and current != host_state.last_clipboard_content and host_state.client_ip):
                host_state.last_clipboard_content = current
                msg = f"AUTH {host_state.session_token} CLIPBOARD_UPDATE HOST {current}".encode("utf-8")
                try:
                    sock.sendto(msg, (host_state.client_ip, UDP_CLIPBOARD_PORT))
                except Exception as e:
                    # A route that vanished (client moved between LAN and VPN, or
                    # Wi-Fi dropped) must not take the host down; retry next tick.
                    _warn_throttled("clipboard-send",
                                    f"Clipboard send to {host_state.client_ip} failed: {e}")
        time.sleep(1)
    sock.close()

def clipboard_listener_host(sock):
    if not HAVE_PYPERCLIP:
        return
    while not host_state.should_terminate:
        try:
            data, addr = sock.recvfrom(65535)
            if not addr:
                if host_state.should_terminate:
                    break
                continue
            if not host_state.session_active or addr[0] != host_state.authed_client_ip:
                continue
            msg = data.decode("utf-8", errors="ignore")
            payload = _extract_authed_cmd(msg)
            if payload is None:
                logging.debug(f"Dropped clipboard packet from {addr[0]} (bad/missing AUTH token)")
                continue
            tokens = payload.split(maxsplit=2)
            if len(tokens) >= 3 and tokens[0] == "CLIPBOARD_UPDATE" and tokens[1] == "CLIENT":
                new_content = tokens[2]
                with host_state.clipboard_lock:
                    host_state.ignore_clipboard_update = True
                    try:
                        if (pyperclip.paste() or "") != new_content:
                            pyperclip.copy(new_content)
                    except Exception as e:
                        _warn_throttled("clipboard-apply", f"Clipboard apply failed: {e}")
                    finally:
                        host_state.ignore_clipboard_update = False
        except OSError:
            break
        except Exception as e:
            _warn_throttled("clipboard-listener", f"Clipboard listener error: {e}")
            time.sleep(0.2)

def recvall(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk: return None
        data += chunk
    return data

def _read_line(sock, limit: int = 128, deadline: float = 2.0):
    """Read one newline-terminated line, bounded in bytes AND time.

    Used for the upload AUTH header. A peer that dribbles one byte per socket
    timeout must not be able to hold the (single-threaded) upload listener.
    """
    buf = b""
    started = time.monotonic()
    while len(buf) < limit:
        if time.monotonic() - started > deadline:
            return None
        try:
            ch = sock.recv(1)
        except Exception:
            return None
        if not ch:
            return None
        if ch == b"\n":
            return buf.decode("utf-8", errors="ignore").strip()
        buf += ch
    return None

def file_upload_listener():
    import re
    from pathlib import Path
    import tempfile

    SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._ -]{1,255}$")
    MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024

    def _safe_filename(name: str) -> str:
        base = os.path.basename(name).strip().replace("\\", "_").replace("/", "_")
        base = re.sub(r"\s+", " ", base)
        if base in (".", "..") or not base:
            return ""
        if not SAFE_NAME_RE.match(base):
            base = re.sub(r"[^A-Za-z0-9._ -]", "_", base)
        return base[:255]

    def _unique_path(dir_path: Path, fname: str) -> Path:
        p = dir_path / fname
        if not p.exists():
            return p
        stem = p.stem
        suffix = p.suffix
        i = 1
        while True:
            cand = dir_path / f"{stem} ({i}){suffix}"
            if not cand.exists():
                return cand
            i += 1

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    host_state.file_upload_sock = s
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", FILE_UPLOAD_PORT))
        s.listen(5)
        logging.info("File upload listener TCP %d", FILE_UPLOAD_PORT)
    except Exception as e:
        trigger_shutdown(f"File upload listener bind/listen failed: {e}")
        try:
            s.close()
        finally:
            host_state.file_upload_sock = None
        return

    while not host_state.should_terminate:
        conn = None
        try:
            conn, addr = s.accept()
            conn.settimeout(10.0)
            peer_ip = addr[0]

            if not host_state.session_active or peer_ip != host_state.authed_client_ip:
                logging.warning(f"[UPLOAD] Rejected unauthorized upload from {peer_ip}")
                conn.close()
                continue

            # Same session token as every other channel: a spoofed source IP
            # alone must not be enough to write files into the home directory.
            try:
                conn.settimeout(2.0)
                auth_line = _read_line(conn)
            finally:
                conn.settimeout(10.0)
            token = auth_line[5:].strip() if (auth_line or "").startswith("AUTH ") else ""
            if not token or not _token_ok(token):
                logging.warning(f"[UPLOAD] Rejected upload from {peer_ip} "
                                f"(missing or invalid session token)")
                conn.close()
                continue

            h = recvall(conn, 4)
            if not h:
                conn.close()
                continue
            name_len = int.from_bytes(h, "big")
            if name_len <= 0 or name_len > 4096:
                logging.warning(f"[UPLOAD] Invalid name length from {peer_ip}: {name_len}")
                conn.close()
                continue

            raw_name = recvall(conn, name_len)
            if not raw_name:
                conn.close()
                continue
            filename_in = raw_name.decode("utf-8", errors="ignore")
            filename = _safe_filename(filename_in)
            if not filename:
                logging.warning(f"[UPLOAD] Bad filename from {peer_ip!r}: {filename_in!r}")
                conn.close()
                continue

            sz_bytes = recvall(conn, 8)
            if not sz_bytes:
                conn.close()
                continue
            file_size = int.from_bytes(sz_bytes, "big", signed=False)
            if file_size < 0 or file_size > MAX_FILE_BYTES:
                logging.warning(f"[UPLOAD] Invalid/oversized file from {peer_ip}: {file_size} bytes")
                conn.close()
                continue

            dest_dir = Path(os.path.expanduser("~")) / "LinuxPlayDrop"
            dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(str(dest_dir), 0o700)
            except Exception:
                pass

            final_path = _unique_path(dest_dir, filename)
            real_dest_dir = dest_dir.resolve()
            real_final_parent = final_path.parent.resolve()
            if real_final_parent != real_dest_dir:
                logging.warning(f"[UPLOAD] Traversal blocked from {peer_ip}: {filename_in!r}")
                conn.close()
                continue

            bytes_left = file_size
            try:
                with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(dest_dir)) as tf:
                    tmp_path = Path(tf.name)
                    while bytes_left > 0:
                        chunk = conn.recv(min(65536, bytes_left))
                        if not chunk:
                            break
                        tf.write(chunk)
                        bytes_left -= len(chunk)
                    tf.flush()
                    os.fsync(tf.fileno())
            except Exception as e:
                try:
                    if 'tmp_path' in locals() and tmp_path.exists():
                        tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                conn.close()
                logging.error(f"[UPLOAD] Write failed from {peer_ip}: {e}")
                continue

            try:
                actual = tmp_path.stat().st_size
                if actual != file_size:
                    tmp_path.unlink(missing_ok=True)
                    conn.close()
                    logging.warning(f"[UPLOAD] Size mismatch from {peer_ip}: expected {file_size}, got {actual}")
                    continue
            except Exception:
                conn.close()
                logging.warning(f"[UPLOAD] Temp file missing after write for {peer_ip}")
                continue

            try:
                os.replace(str(tmp_path), str(final_path))
            except Exception as e:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                conn.close()
                logging.error(f"[UPLOAD] Could not finalize {final_path.name}: {e}")
                continue
            try:
                os.chmod(str(final_path), 0o600)
            except Exception:
                pass

            conn.close()
            logging.info("Received file from %s -> %s (%d bytes)", peer_ip, str(final_path), file_size)

        except socket.timeout as e:
            # A client that vanished mid-upload must not kill the listener.
            logging.warning("[UPLOAD] Connection timed out: %s", e)
            if conn:
                try: conn.close()
                except Exception: pass
            continue
        except OSError as e:
            if conn:
                try: conn.close()
                except Exception: pass
            if host_state.should_terminate:
                break
            logging.warning("[UPLOAD] Socket error: %s — listener continues.", e)
            time.sleep(0.2)
            continue
        except Exception as e:
            if conn:
                try: conn.close()
                except Exception: pass
            logging.error("[UPLOAD] Unexpected error: %s — listener continues.", e)
            time.sleep(0.2)
            continue

    try:
        s.close()
    finally:
        host_state.file_upload_sock = None

def heartbeat_manager(args):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", UDP_HEARTBEAT_PORT))
        host_state.heartbeat_sock = s
        logging.info("Heartbeat manager running on UDP %d", UDP_HEARTBEAT_PORT)
    except Exception as e:
        trigger_shutdown(f"Heartbeat socket error: {e}")
        return

    last_ping = 0.0
    host_state.last_pong_ts = time.time()

    while not host_state.should_terminate:
        now = time.time()

        if host_state.client_ip:
            if now - last_ping >= HEARTBEAT_INTERVAL:
                try:
                    # The timestamp comes back in the PONG, which is how the
                    # client's overlay gets a real round-trip time.
                    s.sendto(f"PING {time.time():.6f}".encode("ascii"),
                             (host_state.client_ip, UDP_HEARTBEAT_PORT))
                    last_ping = now
                except Exception as e:
                    logging.warning("Heartbeat send error: %s", e)

            s.settimeout(0.5)
            try:
                data, addr = s.recvfrom(1024)
                msg = data.decode("utf-8", errors="ignore").strip()
                _handle_pong(msg, addr[0], now)
                _handle_pong(msg, addr[0], now)
            except socket.timeout:
                pass
            except Exception as e:
                logging.debug("Heartbeat recv error: %s", e)

            if (now - host_state.last_pong_ts) > HEARTBEAT_TIMEOUT and (now - host_state.last_disconnect_ts) > 10:
                if host_state.client_ip:
                    logging.warning(
                        "Heartbeat timeout from %s — no PONG or GOODBYE, stopping streams.",
                        host_state.client_ip
                    )
                    try:
                        stop_streams_only()
                    except Exception as e:
                        logging.error("Error stopping streams after timeout: %s", e)

                    host_state.client_ip = None
                    host_state.starting_streams = False
                    set_status("Client disconnected — waiting for connection…")
                    host_state.session_active = False
                    host_state.authed_client_ip = None
                    host_state.session_token = None
                    host_state.session_epoch += 1
                    pin_rotate_if_needed(force=True)

                    time.sleep(RECONNECT_COOLDOWN)

                host_state.last_pong_ts = now
        else:
            time.sleep(0.5)

def resource_monitor():
    p = psutil.Process(os.getpid())

    def get_host_memory_mb():
        total = 0
        try:
            total += p.memory_info().rss
            for child in p.children(recursive=True):
                try:
                    cname = child.name().lower()
                    if "ffmpeg" in cname:
                        total += child.memory_info().rss
                except Exception:
                    pass
        except Exception:
            pass
        return total / (1024 * 1024)

    def read_gpu_usage():
        try:
            import pynvml
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            return f"GPU: {util.gpu}% VRAM: {util.memory}% (NVENC)"
        except Exception:
            pass

        try:
            for card in os.listdir("/sys/class/drm"):
                busy_path = f"/sys/class/drm/{card}/device/gpu_busy_percent"
                if os.path.exists(busy_path):
                    with open(busy_path, "r") as f:
                        val = f.read().strip()
                        return f"GPU: {val}% (VAAPI)"
        except Exception:
            pass

        try:
            cmd = ["timeout", "0.5", "intel_gpu_top", "-J"]
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode()
            if '"Busy"' in out:
                import json
                j = json.loads(out)
                busy = j["engines"]["Render/3D/0"]["busy"]
                return f"GPU: {busy}% (iGPU)"
        except Exception:
            pass

        return ""

    while not host_state.should_terminate:
        cpu = p.cpu_percent(interval=1)
        mem = get_host_memory_mb()
        gpu_info = read_gpu_usage()
        logging.info(f"[MONITOR] CPU: {cpu:.1f}% | RAM: {mem:.1f} MB" + (f" | {gpu_info}" if gpu_info else ""))
        time.sleep(5)

def stats_broadcast():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    p = psutil.Process(os.getpid())

    def get_host_memory_mb():
        total = 0
        try:
            total += p.memory_info().rss
            for child in p.children(recursive=True):
                try:
                    cname = child.name().lower()
                    if "ffmpeg" in cname:
                        total += child.memory_info().rss
                except Exception:
                    pass
        except Exception:
            pass
        return total / (1024 * 1024)

    while not host_state.should_terminate:
        if host_state.client_ip:
            try:
                cpu = psutil.cpu_percent(interval=None)
                mem = get_host_memory_mb()

                gpu = 0.0
                try:
                    import pynvml
                    pynvml.nvmlInit()
                    h = pynvml.nvmlDeviceGetHandleByIndex(0)
                    gpu = float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)
                except Exception:
                    try:
                        for card in os.listdir("/sys/class/drm"):
                            busy_path = f"/sys/class/drm/{card}/device/gpu_busy_percent"
                            if os.path.exists(busy_path):
                                with open(busy_path) as f:
                                    gpu = float(f.read().strip())
                                break
                    except Exception:
                        gpu = 0.0

                msg = _stats_payload(cpu, gpu, mem)
                sock.sendto(msg.encode("utf-8"), (host_state.client_ip, UDP_HEARTBEAT_PORT))
            except Exception:
                pass
        time.sleep(1)

class GamepadServer(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._running = True
        self.sock = None
        self.ui = None
        self._authed_addr = None
        self._dpad = {"left": False, "right": False, "up": False, "down": False}
        self._hatx = 0
        self._haty = 0

    def _open_socket(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVLOWAT, 1)
        s.bind(("", UDP_GAMEPAD_PORT))
        s.setblocking(False)
        return s

    def _open_uinput(self):
        if not (IS_LINUX and HAVE_UINPUT):
            return None
        caps = {
            ecodes.EV_KEY: [
                ecodes.BTN_SOUTH, ecodes.BTN_EAST, ecodes.BTN_NORTH, ecodes.BTN_WEST,
                ecodes.BTN_TL, ecodes.BTN_TR, ecodes.BTN_TL2, ecodes.BTN_TR2,
                ecodes.BTN_SELECT, ecodes.BTN_START,
                ecodes.BTN_THUMBL, ecodes.BTN_THUMBR,
                getattr(ecodes, "BTN_MODE", 0x13c),
            ],
            ecodes.EV_ABS: [
                (ecodes.ABS_X,   AbsInfo(0, -32768, 32767, 16, 0, 0)),
                (ecodes.ABS_Y,   AbsInfo(0, -32768, 32767, 16, 0, 0)),
                (ecodes.ABS_RX,  AbsInfo(0, -32768, 32767, 16, 0, 0)),
                (ecodes.ABS_RY,  AbsInfo(0, -32768, 32767, 16, 0, 0)),
                (ecodes.ABS_Z,   AbsInfo(0, 0, 255, 0, 0, 0)),
                (ecodes.ABS_RZ,  AbsInfo(0, 0, 255, 0, 0, 0)),
                (ecodes.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)),
                (ecodes.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0)),
            ],
        }
        ui = UInput(
            caps,
            name="LinuxPlay Virtual Gamepad",
            bustype=0x03,
            vendor=0x045e,
            product=0x028e,
            version=0x0110,
        )
        ui.write(ecodes.EV_ABS, ecodes.ABS_Z, 0)
        ui.write(ecodes.EV_ABS, ecodes.ABS_RZ, 0)
        ui.syn()
        return ui

    def run(self):
        try:
            import psutil
            psutil.Process(os.getpid()).nice(-10)
        except Exception:
            pass

        try:
            self.sock = self._open_socket()
            self.ui = self._open_uinput()
            if not self.ui:
                logging.info("Gamepad server active (pass-through), but uinput unavailable.")
            else:
                logging.info("Gamepad server active on UDP %d with virtual device.", UDP_GAMEPAD_PORT)
        except Exception as e:
            logging.error("Gamepad server init failed: %s", e)
            return

        buf = bytearray(64)
        pending = []
        unpack_event = struct.Struct("!Bhh").unpack_from

        while self._running and not host_state.should_terminate:
            try:
                n, addr = self.sock.recvfrom_into(buf)
            except BlockingIOError:
                time.sleep(0.0005)
                continue
            except OSError:
                break
            if n < 5:
                continue

            if not host_state.session_active:
                self._authed_addr = None
                continue
            if buf[:5] == b"GAUTH":
                try:
                    tok = bytes(buf[:n]).decode("utf-8", "ignore").split(None, 1)[1].strip()
                except Exception:
                    tok = ""
                if _token_ok(tok):
                    self._authed_addr = addr
                    logging.info("Gamepad channel authorized for %s.", addr[0])
                else:
                    logging.debug("Gamepad GAUTH from %s rejected (bad token).", addr[0])
                continue
            if not (self._authed_addr and addr == self._authed_addr):
                continue

            try:
                for i in range(0, n - 4, 5):
                    try:
                        t, c, v = unpack_event(buf, i)
                    except Exception:
                        continue
                    if not self.ui:
                        continue

                    if t == ecodes.EV_KEY and c in (
                        ecodes.KEY_LEFT, ecodes.KEY_RIGHT, ecodes.KEY_UP, ecodes.KEY_DOWN
                    ):
                        if c == ecodes.KEY_LEFT:
                            self._dpad["left"] = (v != 0)
                        elif c == ecodes.KEY_RIGHT:
                            self._dpad["right"] = (v != 0)
                        elif c == ecodes.KEY_UP:
                            self._dpad["up"] = (v != 0)
                        elif c == ecodes.KEY_DOWN:
                            self._dpad["down"] = (v != 0)

                        new_hatx = (
                            -1 if self._dpad["left"] and not self._dpad["right"]
                            else (1 if self._dpad["right"] and not self._dpad["left"] else 0)
                        )
                        new_haty = (
                            -1 if self._dpad["up"] and not self._dpad["down"]
                            else (1 if self._dpad["down"] and not self._dpad["up"] else 0)
                        )

                        if new_hatx != self._hatx:
                            self._hatx = new_hatx
                            pending.append((ecodes.EV_ABS, ecodes.ABS_HAT0X, self._hatx))
                        if new_haty != self._haty:
                            self._haty = new_haty
                            pending.append((ecodes.EV_ABS, ecodes.ABS_HAT0Y, self._haty))
                    else:
                        pending.append((t, c, v))

                if pending:
                    for et, ec, ev in pending:
                        self.ui.write(et, ec, ev)
                    self.ui.syn()
                    pending.clear()

            except Exception as e:
                logging.debug("Gamepad parse/write error: %s", e)

        try:
            if self.ui:
                self.ui.close()
        except Exception:
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass

    def stop(self):
        self._running = False
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass

def session_manager(args):
    consecutive_failures = 0
    next_attempt_ts = 0.0
    seen_epoch = host_state.session_epoch

    while not host_state.should_terminate:
        # A new session (or a disconnect) clears accumulated backoff: the next
        # client must not stare at a black screen because a previous one hit a
        # bad patch.
        if host_state.session_epoch != seen_epoch:
            seen_epoch = host_state.session_epoch
            consecutive_failures = 0
            next_attempt_ts = 0.0

        if time.time() - host_state.last_disconnect_ts < RECONNECT_COOLDOWN:
            time.sleep(0.5)
            continue

        if host_state.client_ip and not host_state.video_threads:
            if time.time() < next_attempt_ts:
                time.sleep(0.5)
                continue
            set_status(f"Client: {host_state.client_ip}")
            start_streams_for_current_client(args)
            if host_state.video_threads:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                delay = min(2.0 * (2 ** (consecutive_failures - 1)), 30.0)
                next_attempt_ts = time.time() + delay
                logging.error("Streams did not start (attempt %d) — retrying in %.0fs.",
                              consecutive_failures, delay)
                set_status(f"Streams failed to start — retrying in {int(delay)}s…")
        elif (host_state.client_ip and host_state.video_threads
              and args.audio == "enable" and not host_state.audio_thread):
            # Video is healthy but the audio stream gave up: bring back audio only.
            ac = build_audio_cmd()
            if ac:
                logging.info("Restarting the audio stream.")
                host_state.audio_thread = StreamThread(ac, "Audio")
                host_state.audio_thread.start()
        time.sleep(0.5)

def _signal_handler(signum, frame):
    logging.info("Signal %s received, shutting down…", signum)
    trigger_shutdown(f"Signal {signum}")
    stop_all()
    try:
        sys.exit(0)
    except SystemExit:
        pass

def core_main(args, use_signals=True) -> int:
    if use_signals:
        try:
            for _sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(_sig, _signal_handler)
        except Exception:
            pass

    logging.debug("FFmpeg marker in use: %s", _marker_value())

    host_state.current_bitrate = args.bitrate
    host_state.monitors = detect_monitors() or [(1920,1080,0,0)]
    logging.info("Session type: %s; monitors: %s", _session_type(), host_state.monitors)
    host_state.capture_mode = _resolve_capture_mode()
    logging.info("Capture mode: %s", host_state.capture_mode)

    global HOST_ARGS
    HOST_ARGS = args

    try:
        host_state.handshake_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        host_state.handshake_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        host_state.handshake_sock.bind(("", TCP_HANDSHAKE_PORT))
        host_state.handshake_sock.listen(5)
    except Exception as e:
        trigger_shutdown(f"Handshake socket error: {e}")
        stop_all(); return 1

    try:
        host_state.control_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        host_state.control_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        host_state.control_sock.bind(("", UDP_CONTROL_PORT))
    except Exception as e:
        trigger_shutdown(f"Control socket error: {e}")
        stop_all(); return 1

    try:
        host_state.clipboard_listener_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        host_state.clipboard_listener_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        host_state.clipboard_listener_sock.bind(("", UDP_CLIPBOARD_PORT))
    except Exception as e:
        trigger_shutdown(f"Clipboard socket error: {e}")
        stop_all(); return 1

    threading.Thread(target=tcp_handshake_server, args=(host_state.handshake_sock, args.encoder, args), daemon=True).start()
    threading.Thread(target=clipboard_monitor_host, daemon=True).start()
    threading.Thread(target=clipboard_listener_host, args=(host_state.clipboard_listener_sock,), daemon=True).start()
    threading.Thread(target=file_upload_listener, daemon=True).start()

    pin_rotate_if_needed(force=True)
    logging.info("Waiting for client handshake…")

    threading.Thread(target=heartbeat_manager, args=(args,), daemon=True).start()
    threading.Thread(target=session_manager, args=(args,), daemon=True).start()
    threading.Thread(target=control_listener, args=(host_state.control_sock,), daemon=True).start()
    threading.Thread(target=resource_monitor, daemon=True).start()
    threading.Thread(target=stats_broadcast, daemon=True).start()
    threading.Thread(target=pin_manager_thread, daemon=True).start()
    if IS_LINUX:
        try:
            host_state.gamepad_thread = GamepadServer()
            host_state.gamepad_thread.start()
        except Exception as e:
            logging.error("Failed to start gamepad server: %s", e)
    logging.info("Host running. Close window or Ctrl+C to quit.")
    try:
        while not host_state.should_terminate:
            time.sleep(0.2)
    except KeyboardInterrupt:
        trigger_shutdown("KeyboardInterrupt")

    reason = host_state.shutdown_reason
    stop_all()
    if reason:
        logging.critical("Stopped due to error: %s", reason)
        return 1
    logging.info("Shutdown complete.")
    return 0

class LogEmitter(QObject):
    log = pyqtSignal(str)
    status = pyqtSignal(str)
    pin = pyqtSignal(str)
    pair_request = pyqtSignal(object)

log_emitter = LogEmitter()

def set_pin_display(text: str):
    try:
        log_emitter.pin.emit(text)
    except Exception:
        pass

def set_status(text: str):
    try:
        log_emitter.status.emit(text)
    except Exception:
        pass

class PairRequest:
    """A pending 'may this new device pair?' question for the local user."""

    def __init__(self, peer_ip: str, request_id: str):
        self.peer_ip = peer_ip
        self.request_id = request_id
        self.event = threading.Event()
        self.approved = False


def request_pair_approval(peer_ip: str) -> bool:
    """Ask the local user before a certificate is issued to a new device.

    Headless hosts keep the auto-pair behaviour (there is nobody to ask, and
    the PIN was already required). With a GUI up, the request is queued to the
    window and the handshake waits PAIR_APPROVAL_TIMEOUT for the answer.
    """
    request_id = base64.b16encode(secrets.token_bytes(4)).decode("ascii")
    if getattr(host_state, "gui_window", None) is None:
        logging.info("[AUTH] Headless host — auto-approving pairing for %s.", peer_ip)
        return True

    req = PairRequest(peer_ip, request_id)
    try:
        log_emitter.pair_request.emit(req)
    except Exception as e:
        logging.error("[AUTH] Could not ask for pairing approval (%s) — denying %s.", e, peer_ip)
        return False
    if req.event.wait(PAIR_APPROVAL_TIMEOUT):
        if not req.approved:
            logging.warning("[AUTH] Pairing request %s from %s denied by the local user.",
                            request_id, peer_ip)
        return req.approved
    logging.warning("[AUTH] Pairing request %s from %s timed out after %.0fs — denied.",
                    request_id, peer_ip, PAIR_APPROVAL_TIMEOUT)
    return False


def _state_dir() -> str:
    """Per-user state directory (XDG) used for logs; created 0700."""
    base = os.environ.get("LINUXPLAY_STATE_DIR")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".local", "state", "linuxplay")
    os.makedirs(base, mode=0o700, exist_ok=True)
    return base


def _setup_file_logging(debug: bool):
    """Attach a rotating log file so a host that dies later leaves evidence.

    Returns the log path (or None). The GUI launcher also captures stderr, but
    a file survives a crash, a logout, or a host started without a GUI.
    """
    try:
        path = os.path.join(_state_dir(), "host.log")
    except Exception:
        try:
            path = os.path.join(os.getcwd(), "linuxplay_host.log")
        except Exception:
            return None
    try:
        from logging.handlers import RotatingFileHandler
        handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        handler.setLevel(logging.DEBUG if debug else logging.INFO)
        logging.getLogger().addHandler(handler)
        logging.info("Host log file: %s", path)
        return path
    except Exception as e:
        logging.warning("Could not open log file %s: %s", path, e)
        return None
class QtLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        try:
            log_emitter.log.emit(msg)
        except Exception:
            pass

def _apply_dark_palette(app: QApplication):
    app.setStyle("Fusion")
    palette = app.palette()
    palette.setColor(QPalette.Window, QColor(53,53,53))
    palette.setColor(QPalette.WindowText, Qt.white)
    palette.setColor(QPalette.Base, QColor(35,35,35))
    palette.setColor(QPalette.AlternateBase, QColor(53,53,53))
    palette.setColor(QPalette.ToolTipBase, Qt.white)
    palette.setColor(QPalette.ToolTipText, Qt.white)
    palette.setColor(QPalette.Text, Qt.white)
    palette.setColor(QPalette.Button, QColor(53,53,53))
    palette.setColor(QPalette.ButtonText, Qt.white)
    palette.setColor(QPalette.BrightText, Qt.red)
    palette.setColor(QPalette.Link, QColor(42,130,218))
    palette.setColor(QPalette.Highlight, QColor(42,130,218))
    palette.setColor(QPalette.HighlightedText, Qt.black)
    app.setPalette(palette)

class HostWindow(QWidget):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.core_thread = None
        self.setWindowTitle("LinuxPlay Host")
        self.resize(840, 520)

        layout = QVBoxLayout(self)

        self.statusLabel = QLabel("Idle")
        self.statusLabel.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self.pinLabel = QLabel("PIN:  ––––––")
        pin_font = QFont("monospace")
        pin_font.setStyleHint(QFont.TypeWriter)
        pin_font.setPointSize(30)
        pin_font.setBold(True)
        self.pinLabel.setFont(pin_font)
        self.pinLabel.setAlignment(Qt.AlignCenter)
        self.pinLabel.setStyleSheet(
            "color:#7CFC00; background:#0d0d0d; border:2px solid #2e7d32;"
            "border-radius:8px; padding:8px;"
        )
        log_emitter.pin.connect(self.pinLabel.setText)

        self.logView = QTextEdit()
        self.logView.setReadOnly(True)
        font = QFont("monospace"); font.setStyleHint(QFont.TypeWriter)
        self.logView.setFont(font)

        buttons = QHBoxLayout()
        self.stopBtn = QPushButton("Stop")
        self.stopBtn.clicked.connect(self._on_stop)

        self.stopBtn.setAutoDefault(False)
        self.stopBtn.setDefault(False)
        self.stopBtn.setFocusPolicy(Qt.NoFocus)
        self.stopBtn.setShortcut(QKeySequence())
        self.stopBtn.setEnabled(False)
        QTimer.singleShot(1200, lambda: self.stopBtn.setEnabled(True))
        self.logView.setFocus()

        buttons.addStretch(1)
        buttons.addWidget(self.stopBtn)

        layout.addWidget(self.statusLabel)
        layout.addWidget(self.pinLabel)
        layout.addWidget(self.logView)
        layout.addLayout(buttons)

        self._log_handler = QtLogHandler()
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
        self._log_handler.setFormatter(fmt)
        logging.getLogger().addHandler(self._log_handler)
        logging.getLogger().setLevel(logging.getLogger().level)

        log_emitter.log.connect(self.append_log)
        log_emitter.status.connect(self.set_status_text)
        log_emitter.pair_request.connect(self._on_pair_request)
        host_state.gui_window = self

        self._start_core()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_core_done)
        self._poll_timer.start(300)

    def _start_core(self):
        self.set_status_text("Starting…")
        self.append_log("Launching host core…")
        if getattr(host_state, "log_path", None):
            self.append_log(f"Log file: {host_state.log_path}")
        self.core_rc = None

        def _run():
            rc = core_main(self.args, use_signals=False)
            self.core_rc = rc
            QTimer.singleShot(0, lambda: QApplication.instance().exit(rc))

        self.core_thread = threading.Thread(target=_run, name="HostCore", daemon=True)
        self.core_thread.start()

    def _poll_core_done(self):
        if host_state.should_terminate:
            self.stopBtn.setEnabled(False)

    def _on_stop(self):
        if not self.stopBtn.isEnabled():
            return
        self.stopBtn.setEnabled(False)
        self.append_log("Stop requested by user.")
        trigger_shutdown("User pressed Stop")

    def append_log(self, text: str):
        self.logView.append(text)
        self.logView.moveCursor(self.logView.textCursor().End)

    def set_status_text(self, text: str):
        self.statusLabel.setText(text)

    def _on_pair_request(self, req):
        """Ask the local user before a new device is given a certificate."""
        try:
            answer = QMessageBox.question(
                self,
                "Approve new device?",
                f"A device wants to pair with this host:\n\n"
                f"    {req.peer_ip}\n\n"
                f"Allow it to receive a client certificate?\n\n"
                f"(request {req.request_id} — approve only if you just started "
                f"the connection from that device)",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            req.approved = (answer == QMessageBox.Yes)
            self.append_log(f"Pairing request {req.request_id} from {req.peer_ip}: "
                            f"{'approved' if req.approved else 'denied'}")
        except Exception as e:
            logging.error("Pairing prompt failed: %s", e)
            req.approved = False
        finally:
            req.event.set()

    def closeEvent(self, event):
        host_state.gui_window = None
        if not host_state.should_terminate:
            trigger_shutdown("Window closed")
        event.accept()

def parse_args():
    p = argparse.ArgumentParser(description="LinuxPlay Host (Linux only)")
    p.add_argument("--gui", action="store_true", help="Show host GUI window.")
    p.add_argument("--encoder", choices=["none","h.264","h.265"], default="none")
    p.add_argument("--hwenc", choices=["auto","cpu","nvenc","qsv","vaapi"], default="auto",
                   help="Manual encoder backend selection (auto=heuristic).")
    p.add_argument("--framerate", default=DEFAULT_FPS)
    p.add_argument("--bitrate", default=LEGACY_BITRATE)
    p.add_argument("--audio", choices=["enable","disable"], default="disable")
    p.add_argument("--adaptive", action="store_true")
    p.add_argument("--display", default=":0")
    p.add_argument("--preset", default="")
    p.add_argument("--gop", default="30")
    p.add_argument("--qp", default="")
    p.add_argument("--tune", default="")
    p.add_argument("--pix_fmt", default="yuv420p")
    p.add_argument("--slices", type=int, default=4,
                   help="Slices per frame for VAAPI encodes (1 disables; 4 confines loss to a band).")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    return args

def main():
    args = parse_args()

    logging.basicConfig(level=(logging.DEBUG if args.debug else logging.INFO),
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    host_state.log_path = _setup_file_logging(args.debug)

    if not IS_LINUX:
        logging.critical("Hosting is Linux-only. Run this on a Linux machine.")
        return 2

    if args.gui:
        app = QApplication(sys.argv)
        _apply_dark_palette(app)
        w = HostWindow(args)
        w.show()
        rc = app.exec_()
        sys.exit(rc)
    else:
        rc = core_main(args, use_signals=True)
        sys.exit(rc)

if __name__ == "__main__":
    main()
