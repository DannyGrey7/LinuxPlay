#!/usr/bin/env python3
import os
import sys
import struct
import socket
import base64
import psutil
import time
import json
import math
import threading
import statistics
import subprocess
import platform as py_platform
import numpy as np
import av
import logging
import argparse
import re
import select

from queue import Queue
from PyQt5.QtWidgets import (QApplication, QMainWindow, QMessageBox, QOpenGLWidget,
                             QInputDialog, QWidget, QLabel, QAction)
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QTimer, QRectF
from PyQt5.QtGui import (QSurfaceFormat, QPainter, QPen, QColor, QFont, QPainterPath,
                         QKeySequence)

from OpenGL.GL import *

import seqrelay

DEFAULT_UDP_PORT = 5000
# The reorder relay (fix-plan 27) receives each video port and hands the
# decoder plain MPEG-TS on 127.0.0.1 at port + this.
VIDEO_RELAY_OFFSET = 10000
# How long the relay waits for a datagram that arrived out of order. On
# 2026-10-20 late ones came 3 ms (median) to 17 ms (p99) late; a missing
# datagram costs at most this before the keyframe gate takes over.
VIDEO_REORDER_WAIT = 0.05
CONTROL_PORT = 7000
TCP_HANDSHAKE_PORT = 7001
UDP_CLIPBOARD_PORT = 7002
UDP_FILE_PORT = 7003
UDP_HEARTBEAT_PORT = 7004
UDP_GAMEPAD_PORT = 7005
UDP_AUDIO_PORT = 6001

DEFAULT_RESOLUTION = "1920x1080"

# Mirrors host.PAIR_APPROVAL_TIMEOUT: the host may ask its own user to approve
# a first-time pairing, so the PIN handshake must wait longer than 8s.
PAIR_APPROVAL_TIMEOUT = 45.0
# A host that took the connection but did not answer the certificate within
# CERT_TIMEOUT gets one more, longer try before the client gives up on it.
CERT_TIMEOUT = 8.0
CERT_RETRY_TIMEOUT = 30.0

IS_WINDOWS = py_platform.system() == "Windows"
IS_LINUX   = py_platform.system() == "Linux"
IS_MAC     = py_platform.system() == "Darwin"

CLIPBOARD_INBOX = Queue()
audio_proc = None

class _SessionManager:
    def __init__(self):
        self.count = 0
        self.next_id = 0
        self._lock = threading.Lock()

    def register(self):
        with self._lock:
            wid = self.next_id
            self.next_id += 1
            self.count += 1
            return wid

    def unregister(self):
        with self._lock:
            if self.count > 0:
                self.count -= 1
            return self.count

SESSION = _SessionManager()

CLIENT_STATE = {
    "connected": False,
    "last_heartbeat": 0.0,
    "net_mode": "lan",
    "reconnecting": False,
    "token": None,
    "last_reauth_attempt": 0.0,
}

try:
    HERE = os.path.dirname(os.path.abspath(__file__))
    ffbin = os.path.join(HERE, "ffmpeg", "bin")

    if os.name == "nt":
        ffmpeg_exe = os.path.join(ffbin, "ffmpeg.exe")
        if os.path.exists(ffmpeg_exe):
            os.environ["PATH"] = ffbin + os.pathsep + os.environ.get("PATH", "")
    else:
        ffmpeg_bin = os.path.join(ffbin, "ffmpeg")
        if os.path.exists(ffmpeg_bin):
            os.environ["PATH"] = ffbin + os.pathsep + os.environ.get("PATH", "")
except Exception as e:
    logging.debug(f"FFmpeg path init failed: {e}")

def _probe_hardware_capabilities():
    try:
        import importlib.util
        vk_spec = importlib.util.find_spec("vulkan")
        vk_available = vk_spec is not None
    except Exception:
        vk_available = False

    gbm_exists = any(os.path.exists(p) for p in ("/dev/dri/renderD128", "/dev/dri/renderD129"))
    kms_exists = any(os.path.exists(p) for p in ("/dev/dri/card0", "/dev/dri/card1"))
    logging.info(f"Hardware paths: GBM={gbm_exists}, KMS={kms_exists}, Vulkan={vk_available}")

_probe_hardware_capabilities()

def ffmpeg_hwaccels():
    try:
        out = subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-hwaccels"],
            stderr=subprocess.STDOUT, universal_newlines=True
        )
        accels = set()
        for line in out.splitlines():
            name = line.strip()
            if name and not name.lower().startswith("hardware acceleration methods"):
                accels.add(name)
        return accels
    except Exception:
        return set()

def choose_auto_hwaccel():
    accels = ffmpeg_hwaccels()
    if IS_WINDOWS:
        for cand in ("d3d11va", "cuda", "dxva2", "qsv"):
            if cand in accels:
                return cand
        return "cpu"
    if IS_MAC:
        for cand in ("videotoolbox",):
            if cand in accels:
                return cand
        return "cpu"
    for cand in ("vaapi", "qsv", "cuda"):
        if cand in accels:
            return cand
    return "cpu"


# How long ffplay may go without advancing its playback clock before we assume
# the demuxer lost sync and replace it (it can otherwise stay silent forever).
AUDIO_STALL_SECS = 8.0

_FFPLAY_CLOCK_RES = (
    re.compile(r"^\s*(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)\s+[MA]-[AV]\s*:"),
    re.compile(r"^\s*(\d+(?:\.\d+)?)\s+[MA]-[AV]\s*:"),
)


def _parse_ffplay_clock(line: str):
    """Playback position (seconds) from an ffplay status line, else None.

    ffplay rewrites its status line with a carriage return: "  12.34 M-A: ..."
    or "1:02:03.45 M-A: ...". Seeing it move is our proof that audio is flowing.
    """
    for rx in _FFPLAY_CLOCK_RES:
        m = rx.match(line)
        if not m:
            continue
        try:
            groups = m.groups()
            if len(groups) == 3:
                hours = float(groups[0] or 0)
                return hours * 3600 + float(groups[1]) * 60 + float(groups[2])
            return float(groups[0])
        except Exception:
            return None
    return None


def _make_hwaccel(hw_type, dev=None):
    """Build a PyAV HWAccel for this device type, or None if unsupported.

    PyAV >= 12 exposes HWAccel, whose default is_hw_owned=False downloads
    decoded frames to system memory — exactly what the ndarray render path
    needs, and unlike a raw hw_device_ctx (whose frames to_ndarray() rejects).
    Software fallback stays enabled so an unsupported stream still plays.
    """
    try:
        from av.codec.hwaccel import HWAccel, hwdevices_available
    except Exception:
        return None
    try:
        if hw_type not in set(hwdevices_available() or ()):
            logging.info("Hardware decode %s not offered by this FFmpeg build.", hw_type)
            return None
        return HWAccel(device_type=hw_type, device=dev, allow_software_fallback=True)
    except Exception as e:
        logging.warning("Could not create a %s hardware decoder: %s", hw_type, e)
        return None

def _best_ts_pkt_size(mtu_guess: int, ipv6: bool) -> int:
    if mtu_guess <= 0:
        mtu_guess = 1500
    overhead = 48 if ipv6 else 28
    max_payload = max(512, mtu_guess - overhead)
    return max(188, (max_payload // 188) * 188)

def _is_tunnel_iface(iface: str) -> bool:
    """Overlay/VPN links (Tailscale, WireGuard, generic tun) are not 'LAN'.

    Tailscale gives a 1280-byte path and can silently move between a direct
    route and a DERP relay, so the LAN low-latency profile is wrong for it.
    """
    name = (iface or "").lower()
    return name.startswith(("tailscale", "wg", "tun", "tap", "utun", "ppp",
                            "nebula", "zerotier", "nordlynx", "proton"))


def _ip_in_cgnat(ip: str) -> bool:
    """100.64.0.0/10 is the CGNAT range Tailscale (and carrier NAT) uses."""
    try:
        parts = [int(p) for p in str(ip).split(".")]
    except Exception:
        return False
    return len(parts) == 4 and parts[0] == 100 and 64 <= parts[1] <= 127


def _route_mtu(ip: str) -> int:
    """Path MTU to ip from the kernel routing table (Linux); 0 when unknown."""
    if not IS_LINUX:
        return 0
    try:
        import re
        out = subprocess.check_output(["ip", "route", "get", str(ip)],
                                      stderr=subprocess.DEVNULL,
                                      universal_newlines=True, timeout=1.0)
        m = re.search(r"\bdev (\S+)", out)
        if not m:
            return 0
        out = subprocess.check_output(["ip", "-o", "link", "show", "dev", m.group(1)],
                                      stderr=subprocess.DEVNULL,
                                      universal_newlines=True, timeout=1.0)
        m = re.search(r"\bmtu (\d+)", out)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def detect_network_mode(host_ip: str) -> str:
    """Classify the link to the host: 'lan', 'wifi' or 'vpn' (overlay tunnel)."""
    if _ip_in_cgnat(host_ip):
        return "vpn"
    try:
        if IS_LINUX:
            import subprocess, re, os
            out = subprocess.check_output(["ip", "route", "get", host_ip],
                                          universal_newlines=True,
                                          stderr=subprocess.STDOUT)
            m = re.search(r"\bdev\s+(\S+)", out)
            iface = m.group(1) if m else ""
            if iface and _is_tunnel_iface(iface):
                return "vpn"
            if iface and os.path.exists(f"/sys/class/net/{iface}/wireless"):
                return "wifi"
            if iface.startswith("wl"):
                return "wifi"
            return "lan"
        elif IS_MAC:
            import subprocess, re
            out = subprocess.check_output(["route", "-n", "get", host_ip],
                                          universal_newlines=True,
                                          stderr=subprocess.DEVNULL)
            m = re.search(r"interface:\s*(\S+)", out)
            dev = m.group(1) if m else ""
            if dev:
                ports = subprocess.check_output(["networksetup", "-listallhardwareports"],
                                                universal_newlines=True,
                                                stderr=subprocess.DEVNULL)
                cur_dev = None
                for line in ports.splitlines():
                    if line.startswith("Device:"):
                        cur_dev = line.split(":", 1)[1].strip()
                    elif line.startswith("Hardware Port:") and cur_dev == dev:
                        if "wi-fi" in line.lower() or "airdrop" in line.lower():
                            return "wifi"
                        return "lan"
            return "lan"
        elif IS_WINDOWS:
            import subprocess
            ps = ["powershell", "-NoProfile", "-Command",
                  f"(Get-NetRoute -DestinationPrefix {host_ip}/32 | Sort-Object RouteMetric | Select-Object -First 1).InterfaceAlias"]
            alias = subprocess.check_output(ps, universal_newlines=True,
                                            stderr=subprocess.DEVNULL).strip()
            if alias:
                ps2 = ["powershell", "-NoProfile", "-Command",
                       f"($a = Get-NetAdapter -Name '{alias.replace('\"','')}') | Select-Object -Expand NdisPhysicalMedium"]
                medium = subprocess.check_output(ps2, universal_newlines=True,
                                                 stderr=subprocess.DEVNULL).strip().lower()
                if "wireless" in medium or "802.11" in medium:
                    return "wifi"
            return "lan"
    except Exception:
        return "lan"

def _read_pem_cert_fingerprint(pem_path: str) -> str:
    import re, base64, hashlib
    from pathlib import Path as _P
    try:
        data = _P(pem_path).read_text(encoding="utf-8")
        m = re.search(r"-----BEGIN CERTIFICATE-----\s+([A-Za-z0-9+/=\s]+?)\s+-----END CERTIFICATE-----", data, re.S)
        if not m:
            return ""
        der = base64.b64decode("".join(m.group(1).split()))
        return hashlib.sha256(der).hexdigest().upper()
    except Exception:
        return ""

def _build_client_proof(cert_path, key_path, nonce_hex):
    """Sign the host's challenge nonce with the client key.

    Returns 'CERT <b64> SIG <b64>' for the host to verify, or None when the
    cryptography package is missing / signing fails (caller falls back to PIN).
    """
    try:
        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        with open(key_path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
        with open(cert_path, "rb") as f:
            cert_b64 = base64.b64encode(f.read()).decode("ascii")
        nonce = bytes.fromhex(nonce_hex)
        sig = key.sign(
            nonce,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        return f"CERT {cert_b64} SIG {base64.b64encode(sig).decode('ascii')}"
    except Exception as e:
        logging.debug("Could not build certificate proof: %s", e)
        return None

def _size_from_text(text, default=None):
    """'1920x1080' -> (1920, 1080); anything else -> default."""
    try:
        w, h = str(text).strip().lower().split("x", 1)
        w, h = int(w), int(h)
        if w > 0 and h > 0:
            return (w, h)
    except Exception:
        pass
    return default

def _parse_ok_response(resp):
    """Parse 'OK:<encoder>:<monitors>\\nTOKEN <hex>[\\nSTREAM <WxH>][\\nCERT <b64>]'.

    Returns ((encoder, monitors, stream), token, cert_b64). `stream` is the size
    the video arrives at when the host scales the capture ("" = one pixel per
    desktop pixel); the click mapping needs it to tell video pixels from desktop
    coordinates.
    """
    lines = [l.strip() for l in resp.splitlines() if l.strip()]
    parts = lines[0].split(":", 2)
    host_encoder = parts[1].strip() if len(parts) > 1 else "none"
    monitor_info = parts[2].strip() if len(parts) > 2 else DEFAULT_RESOLUTION
    token, cert_b64, stream = "", "", ""
    for ln in lines[1:]:
        if ln.startswith("TOKEN "):
            token = ln.split(None, 1)[1].strip()
        elif ln.startswith("STREAM "):
            stream = ln.split(None, 1)[1].strip()
        elif ln.startswith("CERT "):
            cert_b64 = ln.split(None, 1)[1].strip()
    return (host_encoder, monitor_info, stream), token, cert_b64

def _install_issued_cert(cert_b64, private_key):
    """Save a host-issued certificate (from a KEYREQ handshake) next to this script."""
    try:
        from cryptography.hazmat.primitives import serialization
        here = os.path.dirname(os.path.abspath(__file__))
        cert_path = os.path.join(here, "client_cert.pem")
        key_path = os.path.join(here, "client_key.pem")
        if os.path.exists(cert_path) or os.path.exists(key_path):
            # Replacing credentials that stopped working — keep the old pair so
            # a re-pair can be undone by hand.
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for old in (cert_path, key_path):
                if os.path.exists(old):
                    try:
                        os.replace(old, f"{old}.replaced-{stamp}")
                    except OSError:
                        pass
        with open(cert_path, "wb") as f:
            f.write(base64.b64decode(cert_b64))
        with open(key_path, "wb") as f:
            f.write(private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        os.chmod(cert_path, 0o600)
        os.chmod(key_path, 0o600)
        logging.info("Host issued a client certificate — future connections will skip the PIN.")
    except Exception as e:
        logging.warning("Could not save issued certificate: %s", e)

def _credential_problem(cert_path, key_path):
    """Why the stored certificate/key pair cannot authenticate ("" if it can).

    A pair that does not belong together still signs happily, so the failure
    only shows up as the host rejecting every proof — worth catching here.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        with open(key_path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
    except Exception as e:
        return f"stored certificate or key is unreadable ({e.__class__.__name__}, {e})"
    if cert.public_key().public_numbers() != key.public_key().public_numbers():
        return "client_key.pem does not match client_cert.pem"
    return ""

def _recv_handshake_reply(sock, want_tokens=0, idle=0.4, max_bytes=65536):
    """Read a handshake reply that may arrive in several TCP segments.

    Stops at EOF, at a newline, once `want_tokens` fields have arrived, or after
    `idle` seconds of silence. A lone recv() can return half a reply — the host's
    answer carries a certificate (~1.5 KB) when it issues one — and half a reply
    parses as a failure.
    """
    buf = b""
    while len(buf) < max_bytes:
        if b"\n" in buf or (want_tokens and len(buf.split()) >= want_tokens):
            break
        if buf and not select.select([sock], [], [], idle)[0]:
            break
        try:
            chunk = sock.recv(min(4096, max_bytes - len(buf)))
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
    return buf.decode("utf-8", errors="replace").strip()

def _try_cert_handshake(host_ip, cert_path, key_path, timeout=None):
    """Authenticate with the stored certificate on a connection of its own.

    Returns (ok, info, status). `status` is "" on success or a network error,
    "rejected" when the host turned down the credential itself (pairing should
    ask for a replacement), "busy" when it is in a session it will not give
    up, and "noreply" when it took the connection but said nothing in
    `timeout` seconds. The socket is always closed — a host that rejects a
    proof closes its end, and the PIN handshake that follows needs a live
    connection.
    """
    timeout = CERT_TIMEOUT if timeout is None else timeout
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        fp_hex = _read_pem_cert_fingerprint(cert_path)
        if not fp_hex:
            return (False, None, "")
        sock.connect((host_ip, TCP_HANDSHAKE_PORT))
        # RTP: this client puts the host's numbered video datagrams back in
        # order (seqrelay). Hosts that don't know it read only CERTFP.
        sock.sendall(f'HELLO CERTFP:{fp_hex} RTP\n'.encode('utf-8'))
        resp = _recv_handshake_reply(sock, want_tokens=2)
        if resp.startswith('CHALLENGE '):
            nonce_hex = resp.split(None, 1)[1].strip()
            proof = _build_client_proof(cert_path, key_path, nonce_hex)
            if proof is None:
                logging.warning('Stored certificate found but signing failed — falling back to PIN.')
                return (False, None, "")
            sock.sendall((proof + "\n").encode('utf-8'))
            resp = _recv_handshake_reply(sock)
        logging.debug('Cert handshake response: %s', resp.splitlines()[0] if resp else '(empty)')
        if resp.startswith('OK:'):
            info, token, _cert = _parse_ok_response(resp)
            if token:
                CLIENT_STATE['token'] = token
            CLIENT_STATE['connected'] = True
            CLIENT_STATE['last_heartbeat'] = time.time()
            logging.info('Authenticated via client certificate (FP %s…) — PIN skipped', fp_hex[:12])
            return (True, info, "")
        if resp.startswith('FAIL:UNTRUSTEDCERT'):
            logging.warning('Host does not accept this certificate — falling back to PIN.')
            return (False, None, "rejected")
        if resp.startswith('BUSY'):
            logging.warning('Host is busy with a session (%s).', resp)
            return (False, None, "busy")
        if not resp:
            logging.warning('Host took the connection but did not answer within %ss.', timeout)
            return (False, None, "noreply")
        logging.warning('Unexpected response to certificate auth (%s) — falling back to PIN', resp)
        return (False, None, "")
    except Exception as e:
        logging.debug('Certificate auth failed: %s — falling back to PIN', e)
        return (False, None, "")
    finally:
        try:
            sock.close()
        except Exception:
            pass

def _make_keyreq():
    """Generate a fresh client keypair; returns (keyreq_b64, private_key) or (None, None)."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        spki = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return base64.b64encode(spki).decode("ascii"), key
    except Exception as e:
        logging.debug("KEYREQ not available: %s", e)
        return None, None

def tcp_handshake_client(host_ip, pin=None, interactive=True):
    from PyQt5.QtWidgets import QLineEdit

    try:
        here = os.path.dirname(os.path.abspath(__file__))
        cert_path = os.path.join(here, 'client_cert.pem')
        key_path  = os.path.join(here, 'client_key.pem')
    except Exception:
        cert_path = 'client_cert.pem'; key_path = 'client_key.pem'

    # Try the stored certificate first, on a connection of its own: the host
    # closes its end when it rejects a proof, and reusing that socket is what
    # turned a stale certificate into "broken pipe" instead of a PIN prompt.
    need_fresh_credentials = not (os.path.exists(cert_path) and os.path.exists(key_path))
    if not need_fresh_credentials:
        problem = _credential_problem(cert_path, key_path)
        if problem:
            logging.warning('%s — asking the host for a new one during pairing.', problem)
            need_fresh_credentials = True
        else:
            ok, info, status = _try_cert_handshake(host_ip, cert_path, key_path)
            if status == "noreply":
                logging.info('Retrying the certificate once with a %ss wait.', CERT_RETRY_TIMEOUT)
                ok, info, status = _try_cert_handshake(host_ip, cert_path, key_path,
                                                       timeout=CERT_RETRY_TIMEOUT)
            if ok:
                return (True, info)
            # A PIN cannot help with either of these, and asking for one sent
            # the user to the host's screen for a code it had no use for
            # (2026-10-19: a stalled host answered the challenge 30 s late).
            if status in ("busy", "noreply"):
                title, text = {
                    "busy": ("Host Busy",
                             "The host is in a session with another device (or still closing "
                             "a previous one). Try again in a few seconds."),
                    "noreply": ("Host Not Responding",
                                "The host accepted the connection but did not answer. "
                                "It may be overloaded or stuck — check it and try again."),
                }[status]
                logging.error("%s: %s", title, text)
                if interactive:
                    QMessageBox.critical(None, title, text)
                return (False, None)
            need_fresh_credentials = (status == "rejected")
            logging.info('Continuing with the PIN handshake on a fresh connection.')

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(8)
    try:
        logging.info("Handshake to %s:%s", host_ip, TCP_HANDSHAKE_PORT)
        sock.connect((host_ip, TCP_HANDSHAKE_PORT))
        code = (pin or "").strip()
        if not code or len(code) != 6 or not code.isdigit():
            if not interactive:
                sock.close()
                logging.info("No usable PIN for non-interactive handshake.")
                return (False, None)
            dlg = QInputDialog()
            dlg.setWindowTitle("Enter Host PIN")
            dlg.setLabelText("6-digit PIN (rotates every 30s):")
            dlg.setInputMode(QInputDialog.TextInput)
            dlg.resize(360, 150)

            le = dlg.findChild(QLineEdit)
            if le is None:
                dlg.setTextValue("")
                le = dlg.findChild(QLineEdit)
            if le is not None:
                le.setEchoMode(QLineEdit.Password)
                le.setMaxLength(6)
                le.setPlaceholderText("••••••")

            ok = dlg.exec_()
            code = dlg.textValue().strip()
            if not ok or not code or not code.isdigit() or len(code) != 6:
                sock.close()
                logging.error("PIN entry cancelled or invalid.")
                if interactive:
                    QMessageBox.critical(None, "Invalid PIN", "PIN entry was cancelled or invalid.")
                return (False, None)

        # First-time pairing, or replacing credentials the host stopped
        # accepting: offer the host our public key so it can issue a certificate
        # we store locally — the private key never leaves this machine.
        generated_key = None
        keyreq_line = ""
        if need_fresh_credentials:
            keyreq_b64, generated_key = _make_keyreq()
            if keyreq_b64:
                keyreq_line = "\nKEYREQ " + keyreq_b64

        # The host asks its own user to approve a first-time pairing, so this
        # wait can be much longer than the 8s connect/handshake timeout.
        sock.settimeout(PAIR_APPROVAL_TIMEOUT + 15)
        sock.sendall(f"HELLO {code} RTP{keyreq_line}\n".encode("utf-8"))
        resp = _recv_handshake_reply(sock)
        logging.debug("Handshake response: %s", resp.splitlines()[0] if resp else '(empty)')

        if resp.startswith("OK:"):
            info, token, cert_b64 = _parse_ok_response(resp)
            if token:
                CLIENT_STATE["token"] = token
            sock.close()
            CLIENT_STATE["connected"] = True
            CLIENT_STATE["last_heartbeat"] = time.time()
            if cert_b64 and generated_key is not None:
                _install_issued_cert(cert_b64, generated_key)
            return (True, info)

        elif resp.startswith("BUSY"):
            logging.error("Host is already in a session with another client.")
            if interactive:
                QMessageBox.critical(None, "Host Busy", "The host is already connected to another client.")
            sock.close()
            return (False, None)

        elif resp.startswith("FAIL:BADPIN"):
            logging.error("Incorrect or expired PIN.")
            if interactive:
                QMessageBox.critical(None, "Authentication Failed", "The PIN is incorrect or expired. Please try again.")
            sock.close()
            return (False, None)

        else:
            logging.error("Unexpected handshake response: %s", resp)
            if interactive:
                QMessageBox.critical(None, "Handshake Error", f"Unexpected response from host:\n{resp}")
            sock.close()
            return (False, None)

    except Exception as e:
        logging.error("Handshake failed: %s", e)
        if interactive:
            QMessageBox.critical(None, "Connection Error", f"Handshake failed:\n{e}")
        try:
            sock.close()
        except Exception:
            pass
        return (False, None)

_rehandshake_lock = threading.Lock()
_reauth_no_credentials_warned = False

def attempt_rehandshake(host_ip, pin=None):
    """Re-run the TCP handshake after a session drop (cert bundle or saved PIN)."""
    global _reauth_no_credentials_warned
    if not _rehandshake_lock.acquire(blocking=False):
        return
    try:
        if CLIENT_STATE.get("connected"):
            return
        here = os.path.dirname(os.path.abspath(__file__))
        has_cert = (os.path.exists(os.path.join(here, "client_cert.pem"))
                    and os.path.exists(os.path.join(here, "client_key.pem")))
        code = (pin or "").strip()
        usable_pin = code if (len(code) == 6 and code.isdigit()) else None
        if not has_cert and not usable_pin:
            if not _reauth_no_credentials_warned:
                _reauth_no_credentials_warned = True
                logging.warning(
                    "Session lost — no certificate bundle or saved PIN for automatic "
                    "re-auth; restart the client to reconnect."
                )
            return
        logging.info("Session lost — re-authenticating with host…")
        ok, _info = tcp_handshake_client(host_ip, usable_pin, interactive=False)
        if ok:
            CLIENT_STATE["connected"] = True
            CLIENT_STATE["reconnecting"] = False
            CLIENT_STATE["last_heartbeat"] = time.time()
            logging.info("Re-authenticated — stream should resume shortly.")
        else:
            logging.debug("Re-authentication failed — will retry.")
    finally:
        _rehandshake_lock.release()

def _host_source_ips(host_ip):
    """Every address the negotiated host may legitimately send from.

    The heartbeat socket answers with the session token and treats any PING
    as proof of host liveness, so an off-path peer must never get through it:
    resolve the host name the user gave once, up front.
    """
    ips = set()
    try:
        for info in socket.getaddrinfo(host_ip, None, proto=socket.IPPROTO_UDP):
            ips.add(info[4][0])
    except Exception as e:
        logging.warning("Could not resolve host %r for source checks: %s", host_ip, e)
    literal = str(host_ip or "").strip().strip("[]")
    if literal:
        ips.add(literal)
    return ips

def heartbeat_responder(host_ip):
    host_ips = _host_source_ips(host_ip)

    def loop():
        first_ping = True
        first_stats = True
        # Seeded with the wall clock: starting at 0.0 made the "no STATS"
        # warning below fire on every startup, reporting an age of ~1.8e9 s
        # before the host's first datagram had a chance to arrive.
        last_stats = time.time()
        last_stats_warn = time.time()
        last_reject_warn = 0.0
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("", UDP_HEARTBEAT_PORT))
            except OSError as e:
                logging.error(f"Heartbeat bind failed: {e}")
                return
            sock.settimeout(2)
            logging.info("Heartbeat responder active on UDP %s", UDP_HEARTBEAT_PORT)
            while True:
                try:
                    data, addr = sock.recvfrom(256)
                    if addr[0] not in host_ips:
                        # PONG carries the session token: never hand it to a
                        # peer that is not the host we negotiated with, and
                        # never let one hold the client in "connected".
                        now = time.time()
                        if now - last_reject_warn > 30.0:
                            last_reject_warn = now
                            logging.warning(
                                "Ignoring heartbeat/STATS from %s (host is %s).",
                                addr[0], host_ip)
                        continue
                    if data.startswith(b"PING"):
                        if first_ping:
                            first_ping = False
                            logging.info("Heartbeat PING received from %s — control link is live.", addr[0])
                        token = CLIENT_STATE.get("token") or ""
                        # Echo the host's timestamp so it can measure the RTT.
                        parts = data.split(maxsplit=1)
                        stamp = parts[1].decode("ascii", errors="ignore") if len(parts) > 1 else ""
                        pong = f"PONG {token} {stamp}" if stamp else f"PONG {token}"
                        sock.sendto(pong.encode("utf-8"), addr)
                        CLIENT_STATE["last_heartbeat"] = time.time()
                        if not CLIENT_STATE["connected"]:
                            CLIENT_STATE["connected"] = True
                            CLIENT_STATE["reconnecting"] = False
                    elif data.startswith(b"NOTE"):
                        tag = data[4:].decode("ascii", errors="ignore").strip()[:40]
                        if tag:
                            if (CLIENT_STATE.get("host_note") or ("",))[0] != tag:
                                logging.info("Host says: %s", {
                                    "portal-wait": "waiting for someone to approve screen "
                                                   "sharing on the host"}.get(tag, tag))
                            CLIENT_STATE["host_note"] = (tag, time.time())
                    elif data.startswith(b"STATS"):
                        stats = _parse_host_stats(data.decode("utf-8", errors="ignore"))
                        if stats:
                            CLIENT_STATE["host_stats"] = stats
                            last_stats = time.time()
                            if first_stats:
                                first_stats = False
                                logging.info("Host STATS telemetry active (%d fields).", len(stats))
                except socket.timeout:
                    pass
                except Exception:
                    time.sleep(0.2)
                # Heartbeats arriving while STATS never shows up is the exact
                # signature of the overlay reading zeros with a healthy link,
                # so make it impossible to miss in the log.
                now = time.time()
                if (CLIENT_STATE.get("connected") and now - last_stats > 15.0
                        and now - last_stats_warn > 15.0):
                    logging.warning(
                        "Heartbeats arrive but no STATS telemetry in %.0fs — "
                        "host-side overlay graphs will read zero.",
                        now - last_stats)
                    last_stats_warn = now
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t

def clipboard_listener():
    def loop():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("", UDP_CLIPBOARD_PORT))
            except OSError as e:
                logging.error(f"Clipboard listener bind failed: {e}")
                return
            logging.info("Listening for clipboard updates on UDP %s", UDP_CLIPBOARD_PORT)
            while True:
                try:
                    data, _ = sock.recvfrom(65535)
                    msg = data.decode("utf-8", errors="replace").strip()
                    tok = CLIENT_STATE.get("token") or ""
                    prefix = f"AUTH {tok} "
                    if not tok or not msg.startswith(prefix):
                        continue
                    msg = msg[len(prefix):]
                    if msg.startswith("CLIPBOARD_UPDATE HOST"):
                        text = msg.split("HOST", 1)[1].strip()
                        if text:
                            # Queue it: QClipboard is a GUI-thread object, so
                            # setting it from here would emit dataChanged from
                            # this thread and race the GUI's own clipboard
                            # access. _drain_clipboard_inbox applies it.
                            CLIPBOARD_INBOX.put(text)
                except Exception:
                    time.sleep(0.2)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t

audio_stop = threading.Event()
_audio_listener_started = False
# Set by set_audio_muted(): the player is being restarted on purpose (new
# volume), which is not a glitch and must not be counted as one.
_audio_mute_restart = threading.Event()

AUDIO_SILENCE_DB = -60          # quieter than this for …
AUDIO_SILENCE_SECS = 10         # … this long is "silent" (the clock keeps running)

AUDIO_STATE = {
    "started": None, "restarts": 0, "stalls": 0, "last_glitch": 0.0,
    "muted": False, "muted_since": None, "muted_total": 0.0,
    "silent_since": None, "silence_total": 0.0, "silence_longest": 0.0,
}


def _fmt_secs(secs):
    secs = int(max(0, secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


def _audio_silence_line(line, now=None):
    """Fold one silencedetect line from the player into AUDIO_STATE."""
    now = time.time() if now is None else now
    st = AUDIO_STATE
    if "silence_start:" in line:
        if st["silent_since"] is None:
            st["silent_since"] = now - AUDIO_SILENCE_SECS
            logging.info("Audio: stream silent for %ds+ while the playout clock runs "
                         "(nothing playing on the host, or it captures an idle source).",
                         AUDIO_SILENCE_SECS)
        return True
    if "silence_end:" in line:
        m = re.search(r"silence_duration:\s*([\d.]+)", line)
        dur = float(m.group(1)) if m else (now - (st["silent_since"] or now))
        st["silent_since"] = None
        st["silence_total"] += dur
        st["silence_longest"] = max(st["silence_longest"], dur)
        logging.info("Audio: sound again after %s of silence.", _fmt_secs(dur))
        return True
    return False


def _audio_end_silence(now=None):
    """A player ending mid-silence: bank the silence so far."""
    now = time.time() if now is None else now
    st = AUDIO_STATE
    if st["silent_since"] is not None:
        dur = now - st["silent_since"]
        st["silence_total"] += dur
        st["silence_longest"] = max(st["silence_longest"], dur)
        st["silent_since"] = None


def audio_report(now=None):
    """One line on what this session's audio did (logged when the client closes)."""
    now = time.time() if now is None else now
    st = AUDIO_STATE
    if not st["started"]:
        return None
    span = max(1.0, now - st["started"])
    silent, longest = st["silence_total"], st["silence_longest"]
    if st["silent_since"] is not None:
        cur = now - st["silent_since"]
        silent, longest = silent + cur, max(longest, cur)
    muted = st["muted_total"] + ((now - st["muted_since"]) if st["muted_since"] else 0.0)
    return (f"Audio report: {_fmt_secs(span)}, {st['restarts']} player restarts "
            f"({st['stalls']} stalls), silent {100.0 * silent / span:.0f}% "
            f"(longest {_fmt_secs(longest)}), muted {_fmt_secs(muted)}.")


def set_audio_muted(muted):
    """Mute or unmute the host's audio here; the stream keeps flowing and is
    still watched (silence, stalls) — only the playback volume changes."""
    global audio_proc
    st = AUDIO_STATE
    muted = bool(muted)
    if st["muted"] == muted:
        return
    now = time.time()
    st["muted"] = muted
    if muted:
        st["muted_since"] = now
    elif st["muted_since"]:
        st["muted_total"] += now - st["muted_since"]
        st["muted_since"] = None
    logging.info("Audio %s.", "muted (the stream keeps playing silently)" if muted else "unmuted")
    proc = audio_proc
    if proc is not None:
        _audio_mute_restart.set()
        try:
            proc.terminate()
        except Exception:
            pass


def audio_listener(host_ip, enabled=True, muted=False):
    """Decode the host's Opus stream with ffplay, restarting it whenever it dies.

    After heavy packet loss the mpegts demuxer can lose sync and stay silent
    for the rest of the session; rw_timeout makes ffplay give up after 5 s
    without data so the restart loop swaps in a fresh demuxer.
    """
    global _audio_listener_started
    if not enabled:
        logging.info("Audio disabled — ffplay listener not started.")
        return None
    if _audio_listener_started:
        return None   # one listener process for all windows (port 6001)
    _audio_listener_started = True
    AUDIO_STATE["started"] = time.time()
    if muted:
        set_audio_muted(True)

    max_channels = 2
    try:
        info = subprocess.check_output(
            ["pactl", "list", "sinks"], text=True, stderr=subprocess.DEVNULL
        )
        if "channels: 8" in info or "channel_map: front-left,front-right,rear-left,rear-right,front-center,lfe,side-left,side-right" in info:
            max_channels = 8
        elif "channels: 6" in info or "channel_map: front-left,front-right,rear-left,rear-right,front-center,lfe" in info:
            max_channels = 6
    except Exception:
        pass

    # No aresample=async/first_pts in either filter: measured to add ~6 s of
    # playout delay. The stall watchdog in loop() already recovers dead audio.
    if max_channels > 2:
        afilter = (f"aresample=matrix_encoding=none,"
                   f"aformat=channel_layouts={'5.1' if max_channels==6 else '7.1'}")
        logging.info(f"Detected {max_channels}-channel output device — enabling surround audio.")
    else:
        afilter = ("aresample=matrix_encoding=none,"
                   "pan=stereo|FL<0.5*FL+0.5*FC|FR<0.5*FR+0.5*FC")
        logging.info("Stereo-only output detected — downmixing surround audio.")
    # The stall watchdog only sees a stopped clock; a stream of digital
    # silence (the host capturing an idle sink) keeps the clock running.
    afilter += f",silencedetect=noise={AUDIO_SILENCE_DB}dB:duration={AUDIO_SILENCE_SECS}"

    def loop():
        global audio_proc
        url = f"udp://@0.0.0.0:{UDP_AUDIO_PORT}?overrun_nonfatal=1&buffer_size=1048576&rw_timeout=5000000"
        restarts = 0
        while not audio_stop.is_set():
            cmd = [
                "ffplay",
                "-hide_banner", "-loglevel", "info",
                "-nodisp", "-autoexit",
                "-volume", "0" if AUDIO_STATE["muted"] else "100",
                # ffplay's default 5 s stream analysis meant ~4.8 s of nothing
                # after every (re)start — each stall recovery, glitch restart
                # and mute toggle. The TS descriptors already carry what the
                # Opus decoder needs: measured 0.2 s to first sound.
                "-probesize", "32", "-analyzeduration", "0",
                "-fflags", "nobuffer+discardcorrupt",
                "-af", afilter,
                "-f", "mpegts",
                url,
            ]
            tail = []                    # last few non-clock lines, for the exit report
            proc = None
            try:
                logging.debug("Audio listener command: %s", " ".join(cmd))
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0            # binary: the reader below parses bytes
                )
                audio_proc = proc
                if audio_stop.is_set():
                    # Shutdown sampled audio_proc before this spawn assigned
                    # it, so nothing else will ever stop this player: an
                    # orphan would keep UDP 6001 and play on after exit.
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break
                last_clock = None
                last_progress = time.time()
                buf = b""
                while True:
                    if audio_stop.is_set():
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                        break
                    ready, _, _ = select.select([proc.stdout], [], [], 1.0)
                    if not ready:
                        if (last_clock is not None
                                and time.time() - last_progress > AUDIO_STALL_SECS):
                            logging.warning(
                                "Audio stalled for %.0fs (clock stuck at %.1fs) — restarting the player.",
                                AUDIO_STALL_SECS, last_clock)
                            AUDIO_STATE["stalls"] += 1
                            try:
                                # Release UDP 6001 before the restart, or the
                                # replacement player would receive nothing.
                                proc.terminate()
                            except Exception:
                                pass
                            break
                        if proc.poll() is not None:
                            break
                        continue
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        m = re.search(rb"[\r\n]", buf)
                        if not m:
                            break
                        line = buf[:m.start()].decode("utf-8", errors="replace").strip()
                        buf = buf[m.end():]
                        if not line:
                            continue
                        clock = _parse_ffplay_clock(line)
                        if clock is not None:
                            if last_clock is None:
                                logging.info("Audio stream detected — playout clock running.")
                            last_clock, last_progress = clock, time.time()
                        elif _audio_silence_line(line):
                            pass
                        else:
                            if "Audio:" in line:
                                logging.info(line)
                            tail.append(line[:160])
                            del tail[:-4]
                    if buf and len(buf) > 8192:      # runaway line: drop it
                        buf = b""
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
            except Exception as e:
                logging.error("Audio listener failed: %s", e)
            finally:
                if audio_proc is proc:
                    audio_proc = None
                _audio_end_silence()
            if audio_stop.is_set():
                break
            if _audio_mute_restart.is_set():
                _audio_mute_restart.clear()      # volume change, not a glitch
                continue
            restarts += 1
            AUDIO_STATE["restarts"] = restarts
            AUDIO_STATE["last_glitch"] = time.time()
            # Audio deaths must leave a trail: report the exit code and stay
            # visible at the default log level. (This used to drop to debug
            # after the second restart, hiding long-running trouble.)
            try:
                rc = proc.poll() if proc else None
            except Exception:
                rc = None
            lvl = logging.warning if restarts <= 5 else logging.info
            lvl("Audio player exited (rc=%s) — restarting in 1s (glitch recovery, restart #%d).%s",
                rc, restarts, f" Last output: {' | '.join(tail)}" if tail else "")
            audio_stop.wait(1.0)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t

class StreamCounters:
    """Counters filled by the decode loop, read by the overlay and the pacing report.

    What each one means (all cumulative; readers take deltas):
      damaged  frames that arrived with a hole — the demuxer saw a gap in the
               transport stream (one or more datagrams lost in flight)
      skipped  frames not decoded because they were damaged or predicted from
               a damaged one (held back until the next keyframe)
      refused  frames the decoder was given but returned nothing for (a loss
               the transport could not see, e.g. a whole frame vanished)
      corrupt  frames the decoder returned but flagged as corrupt
      freezes / freeze_ms  how often, and for how long, the picture stood
               still waiting for a keyframe after a loss
    """

    def __init__(self):
        self.bytes = 0
        self.packets = 0
        self.keyframes = 0
        self.frames = 0
        self.errors = 0
        self.damaged = 0
        self.skipped = 0
        self.refused = 0
        self.corrupt = 0
        self.freezes = 0
        self.freeze_ms = 0.0
        self.freeze_max_ms = 0.0          # longest since the last take_freeze_max()

    def snapshot(self):
        return (self.bytes, self.packets, self.keyframes, self.frames)

    def note_freeze(self, ms):
        self.freezes += 1
        self.freeze_ms += ms
        self.freeze_max_ms = max(self.freeze_max_ms, ms)

    def take_freeze_max(self):
        ms, self.freeze_max_ms = self.freeze_max_ms, 0.0
        return ms


# FFmpeg's mpegts demuxer cuts a PES of unknown length into pieces of this
# size (MAX_PES_PAYLOAD). Without its parser behind it, a keyframe bigger
# than 200 KB — common at 2194x1234 — reaches us in pieces, and every piece
# after the first starts mid-NAL ("No start code is found").
_MAX_PES_PAYLOAD = 200 * 1024
_SPLIT_PIECE = _MAX_PES_PAYLOAD - 188          # a piece this big may be continued


def _packet_head(packet, n):
    head = getattr(packet, "head", None)
    if head is not None:
        return head(n)
    return bytes(memoryview(packet)[:n])


def _starts_access_unit(packet):
    try:
        head = _packet_head(packet, 4)
    except Exception:
        return True
    return head[:3] == b"\x00\x00\x01" or head == b"\x00\x00\x00\x01"


class _AccessUnit:
    """A frame the demuxer handed over in pieces, put back together.

    Built from a single piece with orphan=True, it is a fragment whose head
    never arrived (lost in flight): undecodable, so it reports as damaged.
    """

    def __init__(self, pieces, orphan=False):
        first = pieces[0]
        self._stream = first.stream
        self._data = b"".join(bytes(p) for p in pieces)
        self.size = len(self._data)
        self.pts, self.dts, self.time_base = first.pts, first.dts, first.time_base
        self.is_corrupt = orphan or any(p.is_corrupt for p in pieces)
        self.is_keyframe = bool(first.is_keyframe) and not orphan
        self.pieces = len(pieces)

    def head(self, n):
        return self._data[:n]

    def decode(self):
        pkt = av.Packet(self._data)
        pkt.pts, pkt.dts, pkt.time_base = self.pts, self.dts, self.time_base
        return self._stream.decode(pkt)


def _access_units(packets, counters):
    """Demuxed packets in, whole access units out (counting bytes on the way)."""
    pending = []
    for packet in packets:
        size = int(packet.size or 0)
        counters.bytes += size
        if pending:
            if size and not _starts_access_unit(packet):
                pending.append(packet)
                if size >= _SPLIT_PIECE:
                    continue
                yield _AccessUnit(pending)
                pending = []
                continue
            yield _AccessUnit(pending) if len(pending) > 1 else pending[0]
            pending = []
        if size >= _SPLIT_PIECE:
            pending = [packet]
            continue
        if size and not _starts_access_unit(packet):
            yield _AccessUnit([packet], orphan=True)
            continue
        yield packet
    if pending:
        yield _AccessUnit(pending) if len(pending) > 1 else pending[0]


def _is_keyframe(packet, codec=None):
    """Does this access unit start a GOP? The flag first, then the NAL units.

    The client demuxes without FFmpeg's parser (see DecoderThread), and the
    parser is what sets the keyframe flag on transport-stream video. So look
    at the first NAL units ourselves: an IRAP slice (HEVC 16-23) or IDR slice
    (H.264 5), or parameter sets ahead of the first slice — encoders only
    send those with a keyframe. The scan window covers x265's ~2 KB SEI.
    """
    if getattr(packet, "is_keyframe", False):
        return True
    if codec not in ("hevc", "h264"):
        return False
    try:
        head = _packet_head(packet, 4096)
    except Exception:
        return False
    i = head.find(b"\x00\x00\x01")
    while 0 <= i < len(head) - 3:
        b = head[i + 3]
        if codec == "hevc":
            t = (b >> 1) & 0x3F
            if t < 32:
                return 16 <= t <= 23
            if t in (32, 33):                   # VPS, SPS
                return True
        else:
            t = b & 0x1F
            if 1 <= t <= 5:
                return t == 5
            if t == 7:                          # SPS
                return True
        i = head.find(b"\x00\x00\x01", i + 3)
    return False


class _KeyframeGate:
    """Hold the decoder back from a damaged stretch until the next keyframe.

    A frame that arrived with a hole — or anything predicted from it — can only
    decode to garbage. FFmpeg's HEVC decoder refuses every P-frame after a lost
    reference (logging ~9 lines for each), and VideoToolbox fails outright on
    broken slice data, after which the codec is wedged until it is reopened.
    So damaged packets stop here and decoding resumes at the next keyframe;
    the wait is reported as a freeze, which is what the viewer actually saw.
    A fresh stream starts closed as well: it joins mid-GOP, and the slices
    before the first keyframe are undecodable noise.
    """

    # Upstream encoders that never flag keyframes must not mean a black screen.
    NO_KEYFRAME_GIVEUP_SECS = 10.0

    def __init__(self, counters, codec=None):
        self.counters = counters
        self.codec = codec
        self.waiting = True
        self.reason = "join"
        self.since = time.time()
        self.held = 0
        self.keyframes_seen = 0
        self.disabled = False

    def is_key(self, packet):
        return _is_keyframe(packet, self.codec)

    def close(self, reason="loss"):
        """Stop decoding until the next keyframe (a loss the demuxer missed)."""
        if not self.waiting:
            self.waiting, self.reason, self.since, self.held = True, reason, time.time(), 0

    def admit(self, packet, key=None):
        """True if this packet should go to the decoder."""
        if self.disabled:
            return True
        now = time.time()
        if getattr(packet, "is_corrupt", False):
            self.counters.damaged += 1
            if not self.waiting or self.reason == "join":
                self.waiting, self.reason, self.since, self.held = True, "loss", now, 0
            self._hold()
            return False
        if not self.waiting:
            return True
        if key is None:
            key = self.is_key(packet)
        if key:
            self.keyframes_seen += 1
            waited_ms = (now - self.since) * 1000.0
            if self.reason == "loss":
                self.counters.note_freeze(waited_ms)
                logging.debug("Resumed at a keyframe after %.0f ms (%d frames held back).",
                              waited_ms, self.held)
            else:
                logging.info("Joined the stream at its first keyframe (%.0f ms, %d "
                             "frames before it skipped).", waited_ms, self.held)
            self.waiting = False
            return True
        if not self.keyframes_seen and now - self.since > self.NO_KEYFRAME_GIVEUP_SECS:
            logging.warning("No keyframe flagged in %.0fs — decoding without the "
                            "keyframe gate (expect artefacts after losses).",
                            self.NO_KEYFRAME_GIVEUP_SECS)
            self.disabled = True
            return True
        self._hold()
        return False

    def _hold(self):
        self.held += 1
        if self.reason == "loss":
            self.counters.skipped += 1


def _counted_decode(container, counters, gate=None, **kwargs):
    """container.decode(), but counting what arrived and what became a frame.

    PyAV's decode() is literally demux+decode, so this yields the same frames
    while giving the overlay real receive-rate numbers. With a gate, damaged
    stretches never reach the decoder (see _KeyframeGate).
    """
    packets = container.demux(**kwargs)
    if gate is not None:
        # The client demuxes without FFmpeg's parser, which used to glue
        # oversized frames back together; do it here.
        packets = _access_units(packets, counters)
    for packet in packets:
        size, key = 0, False
        try:
            size = int(packet.size or 0)
            if gate is None:
                counters.bytes += size
            counters.packets += 1
            key = gate.is_key(packet) if gate is not None else bool(packet.is_keyframe)
            if key:
                counters.keyframes += 1
        except Exception:
            counters.errors += 1
        if gate is not None and size and not gate.admit(packet, key):
            continue
        produced = 0
        for frame in packet.decode():
            produced += 1
            counters.frames += 1
            yield frame
        if size and not produced and gate is not None and not gate.waiting:
            # Slice threading returns a frame per packet, so nothing back
            # means the decoder refused it — a whole frame lost without a
            # transport gap to show for it. Everything predicted from it
            # would be refused too: hold until the next keyframe.
            counters.refused += 1
            gate.close("loss")


# ── YUV plane payload: lets the GPU do the YUV→RGB conversion ─────────
# The old path ran swscale (to_ndarray("rgb24")) per frame — the single
# biggest CPU cost left after hardware decode. These helpers hand the
# decoder's planes to the renderer as zero-copy views instead; the GL
# shader (and the CPU fallback below) do the colour maths.

_YUV_PLANAR_FORMATS = ("yuv420p", "yuv444p", "nv12")


def _frame_planes(frame):
    """Zero-copy plane views for the formats the shader path understands.

    Returns ("yuv", [(view, plane_w, plane_h, stride), ...], w, h, cs709,
    frame) or None for anything else (odd layouts, 10-bit, …) — the caller
    then falls back to to_ndarray("rgb24") exactly like before. The frame
    itself travels in the payload because the views alias its buffers.
    """
    name = getattr(getattr(frame, "format", None), "name", "")
    if name not in _YUV_PLANAR_FORMATS:
        return None
    try:
        if int(getattr(frame.format, "bits_per_raw", 8) or 8) > 8:
            return None
    except Exception:
        pass
    w, h = frame.width, frame.height
    cw, ch = (w + 1) // 2, (h + 1) // 2
    dims = {
        "yuv444p": ((w, h), (w, h), (w, h)),
        "yuv420p": ((w, h), (cw, ch), (cw, ch)),
        "nv12":    ((w, h), (cw, ch)),
    }[name]
    planes = []
    for i, plane in enumerate(frame.planes):
        try:
            view = np.frombuffer(memoryview(plane), dtype=np.uint8)
        except Exception:
            view = np.frombuffer(plane.to_bytes(), dtype=np.uint8)
        stride = plane.line_size
        pw, ph = dims[i]
        if view.size < stride * ph or stride <= 0:
            return None            # unexpected layout; let the RGB path handle it
        planes.append((view[: stride * ph], pw, ph, stride))
    cs = str(getattr(frame, "colorspace", "") or "")
    return ("yuv", tuple(planes), w, h, "709" in cs, frame)


# BT.601/709 limited-range coefficients, shared with the fragment shader.
_YUV_MATRIX_601 = ((1.164383, 0.0, 1.596027),
                   (1.164383, -0.391762, -0.812968),
                   (1.164383, 2.017232, 0.0))
_YUV_MATRIX_709 = ((1.164383, 0.0, 1.792741),
                   (1.164383, -0.213249, -0.532909),
                   (1.164383, 2.112402, 0.0))


def _yuv_planes_to_rgb(payload):
    """CPU YUV→RGB with the same maths as the shader.

    Only used when the GLSL program failed to build, so the shader path
    degrades to the legacy RGB upload instead of showing nothing.
    """
    _, planes, w, h, cs709, _frame = payload

    def rows(p):
        view, pw, ph, stride = p
        return view.reshape(ph, stride)[:, :pw].astype(np.float32)

    y = rows(planes[0])
    if len(planes) == 2:                        # nv12: U,V byte-interleaved rows
        _v, _pw, ph, stride = planes[1]
        uv = _v.reshape(ph, stride).astype(np.float32)
        u, v = uv[:, 0::2], uv[:, 1::2]
    else:
        u, v = rows(planes[1]), rows(planes[2])
    # Chroma is subsampled by 2 (or not at all), but odd frame dimensions make
    # the ratio inexact — 1079 rows against 540 chroma rows floors to 1 and
    # used to leave u/v smaller than y, so np.stack raised inside paintEvent.
    # Ceiling-divide, repeat, then clip back to the luma size.
    fy = max(1, -(-y.shape[0] // max(1, u.shape[0])))
    fx = max(1, -(-y.shape[1] // max(1, u.shape[1])))
    if (u.shape[0], u.shape[1]) != (y.shape[0], y.shape[1]):
        u = np.repeat(np.repeat(u, fy, axis=0), fx, axis=1)[:y.shape[0], :y.shape[1]]
        v = np.repeat(np.repeat(v, fy, axis=0), fx, axis=1)[:y.shape[0], :y.shape[1]]
    yuv = np.stack([(y - 16.0) / 255.0, (u - 128.0) / 255.0,
                    (v - 128.0) / 255.0], axis=-1)
    rgb = np.clip(yuv @ np.array(_YUV_MATRIX_709 if cs709 else _YUV_MATRIX_601).T,
                  0.0, 1.0)
    return np.ascontiguousarray((rgb * 255.0 + 0.5).astype(np.uint8))


def _client_cpu_percent(proc):
    """Process CPU % plus its children — ffplay (audio) is a separate
    process, and without it the overlay under-reports the real client cost."""
    total = 0.0
    try:
        total += proc.cpu_percent(interval=None)
        for child in proc.children(recursive=True):
            try:
                total += child.cpu_percent(interval=None)
            except Exception:
                pass
    except Exception:
        pass
    return total


# GLSL 1.20 so it runs on the same 2.1-style compatibility context the
# immediate-mode draw already uses (macOS included). The quad still comes
# from glBegin/glTexCoord2f; the shaders only replace the colour maths.
_YUV_VS = """
#version 120
void main() {
    gl_TexCoord[0] = gl_MultiTexCoord0;
    gl_Position = gl_Vertex;
}
"""
_YUV_FS = """
#version 120
uniform sampler2D texY;
uniform sampler2D texU;
uniform sampler2D texV;
uniform int twoPlane;
uniform int cs709;
void main() {
    vec2 st = gl_TexCoord[0].xy;
    float y = texture2D(texY, st).r - 0.0627451;      // 16/255
    float u;
    float v;
    if (twoPlane == 1) {
        vec2 uv = texture2D(texU, st).ra;             // LUMINANCE_ALPHA pair
        u = uv.x - 0.5019608;                         // 128/255
        v = uv.y - 0.5019608;
    } else {
        u = texture2D(texU, st).r - 0.5019608;
        v = texture2D(texV, st).r - 0.5019608;
    }
    vec3 rgb;
    if (cs709 == 1) {
        rgb = vec3(1.164383 * y + 1.792741 * v,
                   1.164383 * y - 0.213249 * u - 0.532909 * v,
                   1.164383 * y + 2.112402 * u);
    } else {
        rgb = vec3(1.164383 * y + 1.596027 * v,
                   1.164383 * y - 0.391762 * u - 0.812968 * v,
                   1.164383 * y + 2.017232 * u);
    }
    gl_FragColor = vec4(clamp(rgb, 0.0, 1.0), 1.0);
}
"""


def _parse_host_stats(msg):
    """Parse the host's STATS datagram into a dict.

    STATS <cpu%> <gpu%> <mem MB> <encode fps> <rtt ms> <jitter ms>
          <encoder kbit/s> <dropped frames> <encoded frames> <window s>
    window > 0: fps and kbit/s cover that many recent seconds; absent or 0
    (older hosts) they are averages over the encoder's whole life.
    Older hosts send fewer fields; missing ones are simply absent.
    """
    parts = msg.split()
    if len(parts) < 3 or parts[0] != "STATS":
        return None
    names = ("cpu", "gpu", "mem", "fps", "rtt", "jitter",
             "enc_kbps", "drops", "frame", "window")
    out = {}
    for name, raw in zip(names, parts[1:]):
        try:
            value = float(raw)
        except Exception:
            continue
        # "inf"/"nan"/"1e999" all parse as floats but would reach the
        # graph maths, where log10(inf) raises inside paintEvent — which
        # PyQt turns into qFatal(). Never let a datagram carry one in.
        if not math.isfinite(value):
            continue
        out[name] = value
    return out or None


class DecoderThread(QThread):
    frame_ready = pyqtSignal(object)

    def __init__(self, input_url, decoder_opts, ultra=False, url_provider=None):
        super().__init__()
        self.input_url = input_url
        # Called before every (re)connect so the path MTU is re-probed when the
        # client moves between Wi-Fi, Ethernet and Tailscale mid-session.
        self._url_provider = url_provider
        self.decoder_opts = dict(decoder_opts or {})
        self.decoder_opts.setdefault("probesize", "32")
        self.decoder_opts.setdefault("analyzeduration", "0")
        self.decoder_opts.setdefault("scan_all_pmts", "1")
        # noparse: FFmpeg's HEVC/H.264 parser releases an access unit only when
        # it sees the start of the next one, and the transport-stream demuxer
        # adds another frame when the PES length is not written — measured at
        # two whole frame intervals between arrival and decode, which on a
        # damage-driven desktop stream is unbounded (a keystroke on a quiet
        # screen waited for two more screen changes). The host writes one
        # access unit per PES, so the parser has nothing to do; it also moved
        # the corrupt flag onto the frame *before* the damaged one.
        self.decoder_opts.setdefault("fflags", "nobuffer+noparse+nofillin")
        self.decoder_opts.setdefault("flags", "low_delay")
        self.decoder_opts.setdefault("reorder_queue_size", "0")
        self.decoder_opts.setdefault("rtbufsize", "2M")
        self.decoder_opts.setdefault("fpsprobesize", "1")

        self._running = True
        self._sw_fallback_done = False
        self.ultra = ultra
        self._frame_count = 0
        self._avg_decode_time = 0.0
        self._restart_delay = 0.5
        self._last_error = ""
        self._has_first_frame = False
        self._hw_name = None
        self.counters = StreamCounters()
        self._hwaccel = None
        self.reconnects = 0
        # Hardware decode bookkeeping. A hardware error after the hardware has
        # been decoding this stream means bad input (a frame damaged in
        # flight), not an unsupported stream: reopen with hardware again, and
        # fall back to the CPU only when it keeps failing — then retry later
        # rather than never. One damaged packet used to cost hardware decode
        # for the rest of a 51-hour session.
        self._hw_wanted = self.decoder_opts.get("hwaccel")
        self._hw_frames = 0
        self._hw_failures = []            # timestamps of recent hardware failures
        self._hw_retry_at = 0.0
        self.hw_resets = 0
        self.hw_fallback_at = 0.0         # when decoding last fell back to the CPU
        self.gate = None                  # the current container's keyframe gate

    def _open_container(self):
        logging.debug("Opening stream with opts: %s", self.decoder_opts)
        if self._hwaccel is not None:
            return av.open(self.input_url, format="mpegts",
                           options=self.decoder_opts, hwaccel=self._hwaccel)
        return av.open(self.input_url, format="mpegts", options=self.decoder_opts)

    HW_RESET_WINDOW_SECS = 60.0      # this many failures inside the window …
    HW_RESET_LIMIT = 3               # … send decoding to the CPU
    HW_RETRY_SECS = 600.0            # how long the CPU fallback lasts

    def _maybe_restore_hw(self):
        if (self._sw_fallback_done and self._hw_wanted and self._hw_retry_at
                and time.time() >= self._hw_retry_at):
            logging.info("Retrying %s hardware decode.", self._hw_wanted)
            self.decoder_opts["hwaccel"] = self._hw_wanted
            self._sw_fallback_done = False
            self._hw_retry_at = 0.0
            self._hw_failures = []

    def _hw_failed(self, err):
        """A decode error with hardware active: True = reopen with hardware."""
        now = time.time()
        self._hw_failures = [t for t in self._hw_failures
                             if now - t < self.HW_RESET_WINDOW_SECS] + [now]
        if self._hw_frames and len(self._hw_failures) < self.HW_RESET_LIMIT:
            self.hw_resets += 1
            logging.warning("Hardware decoder rejected the stream (%s) — reopening it "
                            "on %s (reset #%d; it had decoded %d frames).",
                            err, self._hw_name or self._hw_wanted, self.hw_resets,
                            self._hw_frames)
            return True
        self.hw_fallback_at = now
        if not self._hw_frames:
            # Never decoded a frame of this stream: a real incompatibility,
            # so retrying later would only flap.
            logging.warning("HW decode failed (%s) — switching to CPU for this "
                            "session (the hardware never produced a frame).", err)
            self._hw_retry_at = 0.0
            return False
        logging.warning("HW decode failed (%s) — switching to CPU (%d failures in "
                        "%.0fs); retrying hardware in %.0f min.", err,
                        len(self._hw_failures), self.HW_RESET_WINDOW_SECS,
                        self.HW_RETRY_SECS / 60)
        self._hw_retry_at = now + self.HW_RETRY_SECS
        return False

    def run(self):
        first_attempt = True
        while self._running:
            container = None
            if not first_attempt:
                self.reconnects += 1
            first_attempt = False
            self._maybe_restore_hw()
            try:
                if self._url_provider is not None:
                    self.input_url = self._url_provider()
                container = self._open_container()
                vstream = next((s for s in container.streams if s.type == "video"), None)
                if not vstream:
                    logging.warning("No video stream detected, retrying...")
                    time.sleep(0.5)
                    continue
                    
                cc = vstream.codec_context

                for attr, value in (
                    ("low_delay", True),
                    ("skip_frame", "NONREF"),
                    ("has_b_frames", False),
                    ("strict_std_compliance", "experimental"),
                    ("framerate", None),
                    ("delay", 0),
                ):
                    try:
                        setattr(cc, attr, value)
                    except Exception:
                        pass

                try:
                    cc.flags2 = "+fast"
                except Exception:
                    pass

                # PyAV 18 does not expose codec_context.hw_device_ctx at all, so
                # probing it reported "no hardware device" even when the HWAccel
                # handed to av.open() had attached — the next pass then tore the
                # accel down and labelled a working hardware decoder as CPU. Ask
                # the codec context itself instead.
                hw_attached = bool(getattr(cc, "is_hwaccel", False))
                if hw_attached and self._hw_name != self.decoder_opts.get("hwaccel"):
                    self._hw_name = self.decoder_opts.get("hwaccel") or self._hw_name
                    logging.info("DecoderThread: %s hardware decode attached.",
                                 self._hw_name)

                hw_device = getattr(cc, "hw_device_ctx", None)
                if (not hw_attached and hw_device is None
                        and "hwaccel" in self.decoder_opts):
                    hw_type = self.decoder_opts["hwaccel"]
                    dev = self.decoder_opts.get("hwaccel_device", None)

                    hw_type_map = {
                        "vaapi": "vaapi",
                        "nvdec": "cuda",
                        "cuda": "cuda",
                        "qsv": "qsv",
                        "d3d11va": "d3d11va",
                        "dxva2": "dxva2",
                        "videotoolbox": "videotoolbox",
                    }
                    hw_type_norm = hw_type_map.get(hw_type, hw_type)

                    try:
                        if hasattr(av, "HwDeviceContext"):
                            if not dev:
                                if hw_type_norm == "vaapi":
                                    dev = "/dev/dri/renderD128"
                                elif hw_type_norm in ("cuda", "nvdec"):
                                    dev = "cuda"
                                else:
                                    dev = None
                            hw_ctx = av.HwDeviceContext.create(hw_type_norm, device=dev)
                            cc.hw_device_ctx = hw_ctx
                            self._hw_name = hw_type_norm
                            logging.info(f"DecoderThread: Using hardware decode via {hw_type_norm} ({dev or 'auto'})")
                        else:
                            raise RuntimeError("PyAV build has no HwDeviceContext")
                    except Exception as e:
                        # Modern PyAV (>=12) route: HWAccel downloads frames to
                        # system memory unless is_hw_owned=True, so the ndarray
                        # render path keeps working. Needs the container to be
                        # reopened with hwaccel=..., hence the continue.
                        accel = _make_hwaccel(hw_type_norm, dev)
                        if accel is not None and self._hwaccel is None:
                            self._hwaccel = accel
                            self._hw_name = hw_type_norm
                            logging.info("DecoderThread: using %s hardware decode via "
                                         "PyAV HWAccel (%s).", hw_type_norm, e)
                            try:
                                container.close()
                            except Exception:
                                pass
                            continue
                        logging.warning(f"Hardware decode init failed for {hw_type_norm}: {e}")
                        self._hw_name = "CPU"
                        # A reopen already showed this accel does not attach, so
                        # stop handing it to av.open() on every reconnect.
                        self._hwaccel = None
                        self.decoder_opts.pop("hwaccel", None)
                        self.decoder_opts.pop("hwaccel_device", None)

                # Software HEVC 4:4:4 at 2.5 MP does not reach the host's ~50 fps
                # on the tuned two threads — the M1 client logged a ~35 fps
                # ceiling and 1-3 s gaps — so let FFmpeg size the pool whenever
                # the decoder is software. With a hardware decoder attached the
                # count costs nothing, and --ultra keeps its deliberate 1.
                if self.ultra or hw_attached:
                    cc.thread_count = 1 if self.ultra else 2
                else:
                    cc.thread_count = 0
                    logging.debug("DecoderThread: software decode, thread count auto.")

                t_decode = []

                gate = self.gate = _KeyframeGate(self.counters, codec=cc.name)
                for frame in _counted_decode(container, self.counters, gate=gate, video=0):
                    if not self._running:
                        break
                    if not frame or frame.is_corrupt:
                        if frame is not None:
                            # Counted for the renderer's pacing report: a corrupt
                            # frame is dropped silently, which otherwise looks
                            # exactly like "the host sent nothing".
                            self.counters.corrupt += 1
                        continue
                    if hw_attached:
                        self._hw_frames += 1
                    elif self._hw_retry_at and time.time() >= self._hw_retry_at:
                        # Time to give the hardware another go; the reopened
                        # decoder picks up at the next keyframe.
                        self._retry_hw_now = True
                        break

                    t0 = time.perf_counter()

                    # No dmabuf payload: paintGL cannot draw one (and the
                    # ValueError it used to raise inside paintEvent aborts the
                    # process through qFatal). Planes are read through
                    # _frame_planes when possible, else converted to RGB.
                    payload = _frame_planes(frame)
                    if payload is not None:
                        self._has_first_frame = True
                        self.frame_ready.emit(payload)
                    else:
                        arr = frame.to_ndarray(format="rgb24")
                        if not arr.flags["C_CONTIGUOUS"]:
                            arr = np.ascontiguousarray(arr, dtype=np.uint8)
                        self._has_first_frame = True
                        self.frame_ready.emit((arr, frame.width, frame.height))

                    t1 = time.perf_counter()
                    self._frame_count += 1
                    decode_time = (t1 - t0) * 1000
                    self._avg_decode_time = (
                        0.9 * self._avg_decode_time + 0.1 * decode_time
                        if self._frame_count > 1 else decode_time
                    )

                    if len(t_decode) < 120:
                        t_decode.append(decode_time)
                    else:
                        avg = statistics.mean(t_decode)
                        logging.debug(f"Avg decode time: {avg:.2f} ms ({self._hw_name or 'CPU'})")
                        t_decode.clear()


                if getattr(self, "_retry_hw_now", False):
                    self._retry_hw_now = False
                    continue
                if self._running:
                    if not self._has_first_frame:
                        logging.info("Still waiting for video data...")
                    else:
                        logging.warning("Stream ended — reconnecting in %.1fs...", self._restart_delay)
                    time.sleep(self._restart_delay)

            except Exception as e:
                err = str(e)
                if err != self._last_error:
                    logging.error(f"Decode error: {err}")
                    self._last_error = err

                if not self._sw_fallback_done and "hwaccel" in self.decoder_opts:
                    if self._hw_failed(err):
                        continue                      # same accel, fresh decoder
                    self._hwaccel = None
                    self.decoder_opts.pop("hwaccel", None)
                    self.decoder_opts.pop("hwaccel_device", None)
                    self._sw_fallback_done = True
                    continue

                if self._running:
                    time.sleep(self._restart_delay)

            finally:
                try:
                    if container:
                        container.close()
                except Exception:
                    pass

    def stop(self):
        self._running = False
        time.sleep(0.05)

# How often the renderer reports its frame-arrival pacing (see updateFrame).
PACING_WINDOW_SECS = 5.0

class _LibavLogFilter(logging.Handler):
    """Sort FFmpeg's messages: count the expected ones, pass the rest on.

    Three families are expected and repeat for as long as a cause lasts, so
    they are tallied instead of printed (one lost datagram used to cost up to
    ~130 log lines):
      - transport damage ("Packet corrupt" …): already counted per packet by
        the decode loop, from the packet's own corrupt flag;
      - decoder complaints about missing references or slices — the echo of a
        loss the transport could not see; first one of each kind is printed,
        then a per-minute summary;
      - stream-probe chatter at connect ("not enough frames to estimate rate",
        "Could not find codec parameters"), inherent to probesize=32: debug.
    Anything else FFmpeg says reaches the log unchanged.
    """

    _TRANSPORT = ("Continuity check failed", "Packet corrupt",
                  "corrupt input packet", "Invalid data found",
                  "PES packet size mismatch")
    _DECODER = (
        ("First slice in a frame missing", "slice without frame"),
        ("Skipping invalid undecodable NALU", "undecodable NALU"),
        ("Could not find ref with POC", "missing reference"),
        ("Error constructing the frame RPS", "missing reference"),
        ("PPS id out of range", "no parameter set"),
        ("Ignoring POC change between slices", "mixed frames"),
        ("Non-matching NAL types of the VCL NALUs", "mixed frames"),
        ("CABAC_MAX_BIN", "damaged slice data"),
        ("cu_qp_delta", "damaged slice data"),
    )
    _PROBE = ("not enough frames to estimate rate", "Could not find codec parameters",
              "Consider increasing the value", "start time for stream",
              "no TS found at start of file")
    SUMMARY_SECS = 60.0

    def __init__(self):
        super().__init__()
        self.decoder_msgs = 0                 # cumulative, for the pacing report
        self._seen = set()
        self._pending = {}
        self._last_summary = time.time()

    def emit(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return
        if any(p in message for p in self._TRANSPORT):
            return
        if any(p in message for p in self._PROBE):
            if record.levelno > logging.DEBUG:
                logging.debug("FFmpeg (stream probe): %s", message.splitlines()[0])
            return
        for pattern, family in self._DECODER:
            if pattern in message:
                self._decoder(record, family)
                return
        self._forward(record)

    def _decoder(self, record, family):
        self.decoder_msgs += 1
        if family not in self._seen:
            self._seen.add(family)
            self._forward(record)
            return
        self._pending[family] = self._pending.get(family, 0) + 1
        now = time.time()
        if now - self._last_summary >= self.SUMMARY_SECS:
            self._last_summary = now
            parts = ", ".join(f"{n} {f}" for f, n in sorted(
                self._pending.items(), key=lambda kv: -kv[1]))
            self._pending = {}
            logging.warning("Decoder: %s in the last minute (after stream losses).", parts)

    @staticmethod
    def _forward(record):
        # Hand it to the root handlers directly. Going through a logger here
        # would come straight back to this handler.
        for handler in logging.getLogger().handlers:
            if record.levelno >= handler.level:
                handler.handle(record)


_LIBAV_LOG = _LibavLogFilter()


def _enable_libav_logging():
    """Let FFmpeg's own warnings reach Python (PyAV drops them by default)."""
    try:
        import av.logging
        av.logging.set_level(av.logging.WARNING)
        libav_logger = logging.getLogger("libav")
        # Handlers see child-logger records ("libav.mpegts"); a filter on this
        # logger would not. Propagation off so the tally is the only copy.
        libav_logger.propagate = False
        if not any(isinstance(h, _LibavLogFilter) for h in libav_logger.handlers):
            libav_logger.addHandler(_LIBAV_LOG)
        return True
    except Exception as e:
        logging.debug("Could not enable FFmpeg logging: %s", e)
        return False


class VideoWidgetGL(QOpenGLWidget):
    def __init__(self, control_callback, rwidth, rheight, offset_x, offset_y, host_ip,
                 parent=None, toggle_overlay=None, desktop_size=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)

        self.host_ip = host_ip
        self.control_callback = control_callback
        self.toggle_overlay = toggle_overlay
        self.texture_width = rwidth
        self.texture_height = rheight
        # Clicks travel in desktop coordinates, which only match video pixels
        # when the host streams at native size (--resolution scales the capture).
        self.desktop_width, self.desktop_height = desktop_size or (rwidth, rheight)
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.frame_data = None
        self._pending_resize = None

        self.clipboard = QApplication.clipboard()
        self.clipboard.dataChanged.connect(self.on_clipboard_change)
        self.last_clipboard = self.clipboard.text()
        self.ignore_clipboard = False

        self.texture_id = None
        self.pbo_ids = []
        self.current_pbo = 0
        self._yuv_prog = None
        self._yuv_locs = {}
        self._yuv_textures = []
        self._yuv_alloc = None            # (w, h, nplanes) the textures were sized for
        self._last_frame_recv = time.time()
        self._last_mouse_ts = 0.0
        self._mouse_throttle = 0.0025
        # Frame-arrival pacing window, reported once per PACING_WINDOW_SECS:
        # a hitch the user sees is either a gap (the link or the decoder
        # stalled) or a burst of near-zero deltas (a stale backlog draining).
        self._pacing_start = time.time()
        self._pacing_last = 0.0
        self._pacing_frames = 0
        self._pacing_max_gap = 0.0
        self._pacing_bursts = 0
        # Set by MainWindow to the decoder's StreamCounters; the report shows
        # deltas over its own window, so every figure on the line shares it.
        self.stream_counters = None
        self._pacing_seen = {}

        if not logging.getLogger().hasHandlers():
            logging.basicConfig(level=logging.DEBUG,
                                format="%(asctime)s [%(levelname)s] %(message)s",
                                datefmt="%H:%M:%S")

        logging.info("────────────────────────────────────────────")
        logging.info("Renderer Initialization Summary")
        logging.info(f"Session type: {os.environ.get('XDG_SESSION_TYPE', 'unknown')}")
        logging.info(f"Desktop: {os.environ.get('XDG_CURRENT_DESKTOP', 'unknown')}")
        logging.info(f"Display server: {os.environ.get('WAYLAND_DISPLAY') or os.environ.get('DISPLAY', 'n/a')}")
        for node in ("/dev/dri/renderD128", "/dev/dri/renderD129", "/dev/dri/card0", "/dev/dri/card1"):
            exists = "✅" if os.path.exists(node) else "❌"
            access = "🟢" if os.access(node, os.W_OK) else "🔴"
            logging.info(f"  {node:<20} exists={exists} access={access}")
        # All drawing happens here in paintGL (QOpenGLWidget); the earlier
        # KMSDRM/Vulkan/OpenGL renderer classes were never called by anything.
        self.render_backend = "OpenGL"
        logging.info("Using render backend: %s (QOpenGLWidget paintGL)", self.render_backend)
        logging.info("────────────────────────────────────────────")

    def on_clipboard_change(self):
        new_text = self.clipboard.text()
        if self.ignore_clipboard or not new_text or new_text == self.last_clipboard:
            return
        self.last_clipboard = new_text
        token = CLIENT_STATE.get("token") or ""
        msg = f"AUTH {token} CLIPBOARD_UPDATE CLIENT {new_text}".encode("utf-8")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(msg, (self.host_ip, UDP_CLIPBOARD_PORT))
        except Exception:
            pass

    def initializeGL(self):
        glDisable(GL_DEPTH_TEST)
        glDisable(GL_DITHER)
        glClearColor(0.0, 0.0, 0.0, 1.0)
        self.texture_id = glGenTextures(1)
        self._initialize_texture(self.texture_width, self.texture_height)
        self._init_yuv_program()

    def _init_yuv_program(self):
        """Compile the YUV shader; on failure the widget keeps working through
        the legacy rgb24 upload (fed by _yuv_planes_to_rgb), just slower."""
        def compile_stage(stype, source):
            sh = glCreateShader(stype)
            glShaderSource(sh, source)
            glCompileShader(sh)
            if glGetShaderiv(sh, GL_COMPILE_STATUS) != GL_TRUE:
                log = glGetShaderInfoLog(sh)
                raise RuntimeError(f"shader compile failed: {log}")
            return sh
        try:
            prog = glCreateProgram()
            glAttachShader(prog, compile_stage(GL_VERTEX_SHADER, _YUV_VS))
            glAttachShader(prog, compile_stage(GL_FRAGMENT_SHADER, _YUV_FS))
            glLinkProgram(prog)
            if glGetProgramiv(prog, GL_LINK_STATUS) != GL_TRUE:
                raise RuntimeError(f"program link failed: {glGetProgramInfoLog(prog)}")
            self._yuv_prog = prog
            self._yuv_locs = {name: glGetUniformLocation(prog, name)
                              for name in ("texY", "texU", "texV", "twoPlane", "cs709")}
            logging.info("YUV→RGB shader program built; GPU colour conversion active.")
        except Exception as e:
            self._yuv_prog = None
            logging.warning("YUV shader unavailable (%s) — falling back to CPU "
                            "colour conversion through the legacy upload.", e)

    def _initialize_texture(self, w, h):
        glBindTexture(GL_TEXTURE_2D, self.texture_id)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB, w, h, 0, GL_RGB, GL_UNSIGNED_BYTE, None)
        glBindTexture(GL_TEXTURE_2D, 0)

        if self.pbo_ids:
            glDeleteBuffers(len(self.pbo_ids), self.pbo_ids)

        buf_size = w * h * 3
        self.pbo_ids = list(glGenBuffers(3))
        for pbo in self.pbo_ids:
            glBindBuffer(GL_PIXEL_UNPACK_BUFFER, pbo)
            glBufferData(GL_PIXEL_UNPACK_BUFFER, buf_size, None, GL_STREAM_DRAW)
        glBindBuffer(GL_PIXEL_UNPACK_BUFFER, 0)

        self.texture_width, self.texture_height = w, h
        self.current_pbo = 0
        glFlush()

    def resizeTexture(self, w, h):
        if (w, h) != (self.texture_width, self.texture_height):
            logging.info(f"Resize texture {self.texture_width}x{self.texture_height} → {w}x{h}")
            self._pending_resize = (w, h)

    def _quad_scale(self, fw, fh):
        aspect_tex = fw / float(fh)
        aspect_win = self.width() / float(self.height())
        if aspect_win > aspect_tex:
            return (aspect_tex / aspect_win), 1.0
        return 1.0, (aspect_win / aspect_tex)

    def _ensure_yuv_textures(self, w, h, plane_dims):
        # The plane dims (not just w/h/nplanes) must be part of the key:
        # yuv420p and yuv444p share all three but need different chroma
        # texture sizes, and a stale smaller texture makes the upload go
        # out of bounds.
        if self._yuv_alloc == (w, h, plane_dims) and self._yuv_textures:
            return
        if self._yuv_textures:
            glDeleteTextures(len(self._yuv_textures), self._yuv_textures)
        self._yuv_textures = list(glGenTextures(len(plane_dims)))
        for i, (tex, (tw, th)) in enumerate(zip(self._yuv_textures, plane_dims)):
            glBindTexture(GL_TEXTURE_2D, tex)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
            fmt = GL_LUMINANCE_ALPHA if (len(plane_dims) == 2 and i == 1) else GL_LUMINANCE
            glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
            glTexImage2D(GL_TEXTURE_2D, 0, fmt, tw, th, 0, fmt, GL_UNSIGNED_BYTE, None)
        glBindTexture(GL_TEXTURE_2D, 0)
        self._yuv_alloc = (w, h, plane_dims)

    def _paint_yuv(self, payload):
        """Upload the planes as-is (no conversion, no staging copy) and let
        the fragment shader produce RGB — the fast path."""
        _, planes, w, h, cs709, _frame = payload
        n = len(planes)
        self._ensure_yuv_textures(w, h, tuple((pw, ph) for _v, pw, ph, _s in planes))
        for i, (view, pw, ph, stride) in enumerate(planes):
            interleaved = (n == 2 and i == 1)     # nv12: U,V as LUMINANCE_ALPHA pairs
            # ROW_LENGTH makes the driver skip row padding; for interleaved
            # planes a texel is 2 bytes, so the length is counted in pairs.
            glPixelStorei(GL_UNPACK_ROW_LENGTH, stride // 2 if interleaved else stride)
            glBindTexture(GL_TEXTURE_2D, self._yuv_textures[i])
            glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, pw, ph,
                            GL_LUMINANCE_ALPHA if interleaved else GL_LUMINANCE,
                            GL_UNSIGNED_BYTE, view)
        glPixelStorei(GL_UNPACK_ROW_LENGTH, 0)
        glBindTexture(GL_TEXTURE_2D, 0)

        glUseProgram(self._yuv_prog)
        try:
            for unit, tex in enumerate(self._yuv_textures):
                glActiveTexture(GL_TEXTURE0 + unit)
                glBindTexture(GL_TEXTURE_2D, tex)
            glUniform1i(self._yuv_locs["texY"], 0)
            glUniform1i(self._yuv_locs["texU"], 1)
            glUniform1i(self._yuv_locs["texV"], 2)
            glUniform1i(self._yuv_locs["twoPlane"], 1 if n == 2 else 0)
            glUniform1i(self._yuv_locs["cs709"], 1 if cs709 else 0)

            sx, sy = self._quad_scale(w, h)
            glClear(GL_COLOR_BUFFER_BIT)
            glBegin(GL_QUADS)
            glTexCoord2f(0.0, 1.0); glVertex2f(-sx, -sy)
            glTexCoord2f(1.0, 1.0); glVertex2f(sx, -sy)
            glTexCoord2f(1.0, 0.0); glVertex2f(sx, sy)
            glTexCoord2f(0.0, 0.0); glVertex2f(-sx, sy)
            glEnd()
        finally:
            glUseProgram(0)
            glActiveTexture(GL_TEXTURE0)

    def paintGL(self):
        if not self.frame_data:
            glClear(GL_COLOR_BUFFER_BIT)
            return

        payload = self.frame_data
        kind = payload[0] if isinstance(payload[0], str) else "rgb"
        if kind == "yuv":
            if self._yuv_prog is not None:
                self._paint_yuv(payload)
                return
            # shader build failed: convert on the CPU and take the legacy upload
            arr = _yuv_planes_to_rgb(payload)
            fw, fh = payload[2], payload[3]
        elif kind == "rgb":
            arr, fw, fh = payload
        else:
            # A payload this renderer cannot draw: skipping the frame beats
            # raising inside a paint handler, which PyQt turns into qFatal().
            logging.debug("Unsupported frame payload %r — skipping paint.", kind)
            glClear(GL_COLOR_BUFFER_BIT)
            return

        if self._pending_resize:
            w, h = self._pending_resize
            self._initialize_texture(w, h)
            self._pending_resize = None

        data = np.ascontiguousarray(arr, dtype=np.uint8)
        size = data.nbytes
        current_pbo = self.pbo_ids[self.current_pbo]

        glBindBuffer(GL_PIXEL_UNPACK_BUFFER, current_pbo)
        # Fill the staging PBO straight from the numpy buffer: the driver does
        # the copy, so the ctypes.memmove of a full frame is gone.
        glBufferData(GL_PIXEL_UNPACK_BUFFER, size, data, GL_STREAM_DRAW)

        glBindTexture(GL_TEXTURE_2D, self.texture_id)
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, fw, fh, GL_RGB, GL_UNSIGNED_BYTE, None)
        glBindBuffer(GL_PIXEL_UNPACK_BUFFER, 0)

        sx, sy = self._quad_scale(fw, fh)
        glClear(GL_COLOR_BUFFER_BIT)
        glEnable(GL_TEXTURE_2D)
        glBegin(GL_QUADS)
        glTexCoord2f(0.0, 1.0); glVertex2f(-sx, -sy)
        glTexCoord2f(1.0, 1.0); glVertex2f(sx, -sy)
        glTexCoord2f(1.0, 0.0); glVertex2f(sx, sy)
        glTexCoord2f(0.0, 0.0); glVertex2f(-sx, sy)
        glEnd()
        glDisable(GL_TEXTURE_2D)
        glBindTexture(GL_TEXTURE_2D, 0)
        glFlush()

        self.current_pbo = (self.current_pbo + 1) % len(self.pbo_ids)

    def updateFrame(self, frame_tuple):
        self.frame_data = frame_tuple
        kind = frame_tuple[0] if isinstance(frame_tuple[0], str) else "rgb"
        if kind == "yuv":
            # plane textures manage their own size; just track it
            _, _planes, fw, fh, _cs, _frame = frame_tuple
            self.texture_width, self.texture_height = fw, fh
        else:
            _arr, fw, fh = frame_tuple
            if (fw, fh) != (self.texture_width, self.texture_height):
                self.resizeTexture(fw, fh)
        self._last_frame_recv = time.time()

        now = time.time()
        gap = now - self._pacing_last if self._pacing_last else 0.0
        self._pacing_last = now
        self._pacing_frames += 1
        if gap > self._pacing_max_gap:
            self._pacing_max_gap = gap
        if 0.0 < gap < 0.005:
            self._pacing_bursts += 1
        window = now - self._pacing_start
        if window >= PACING_WINDOW_SECS:
            logging.info("Frame pacing: %d frames in %.1fs (%.1f fps), max gap %.0f ms, "
                         "%d under 5 ms%s", self._pacing_frames, window,
                         self._pacing_frames / window, self._pacing_max_gap * 1000.0,
                         self._pacing_bursts, self._pacing_details(window))
            self._pacing_start = now
            self._pacing_frames = 0
            self._pacing_max_gap = 0.0
            self._pacing_bursts = 0

        if not hasattr(self, "_frame_times"):
            self._frame_times = []
        self._frame_times.append(now)
        if len(self._frame_times) > 90:
            self._frame_times.pop(0)
        if len(self._frame_times) >= 2:
            diffs = [t2 - t1 for t1, t2 in zip(self._frame_times, self._frame_times[1:])]
            mean_diff = statistics.mean(diffs)
            self._fps = 1.0 / mean_diff if mean_diff > 0 else 0.0

        if self.isVisible():
            t = time.time()
            if not hasattr(self, "_last_draw") or (t - getattr(self, "_last_draw", 0)) > (1/240):
                self._last_draw = t
                self.update()

    def _delta(self, key, value):
        d = value - self._pacing_seen.get(key, value if key == "bytes" else 0)
        self._pacing_seen[key] = value
        return d

    def _pacing_details(self, window):
        """The loss and rate half of the pacing line, all over the same window.

        loss: damaged = frames that arrived with a hole; held = frames not
        decoded until the next keyframe because of one; refused = frames the
        decoder dropped on its own; corrupt = frames it returned flagged;
        frozen = time the picture waited for that keyframe. The host's rate
        next to ours separates a lossy link (host sent it, it never arrived)
        from a slow client.
        """
        out = ""
        c = self.stream_counters
        if c is not None:
            damaged = self._delta("damaged", c.damaged)
            held = self._delta("skipped", c.skipped)
            refused = self._delta("refused", c.refused)
            corrupt = self._delta("corrupt", c.corrupt)
            frozen = self._delta("freeze_ms", c.freeze_ms)
            longest = c.take_freeze_max()
            msgs = self._delta("decoder_msgs", _LIBAV_LOG.decoder_msgs)
            out += (f" | loss: {damaged} damaged, {held} held, {refused} refused, "
                    f"{corrupt} corrupt, frozen {frozen:.0f} ms")
            if longest and longest < frozen:
                out += f" (longest {longest:.0f})"
            if msgs:
                out += f", {msgs} decoder msgs"
        stats = CLIENT_STATE.get("host_stats") or {}
        rates = []
        if stats.get("fps") is not None:
            host = f"host {stats['fps']:.0f} fps"
            if stats.get("enc_kbps"):
                host += f" / {stats['enc_kbps'] / 1000.0:.1f} Mb/s"
            # Hosts before the stats window field report encoder-lifetime
            # averages; say so instead of setting them beside a 5 s figure.
            if not stats.get("window"):
                host += " (session avg)"
            rates.append(host)
        if c is not None:
            got = self._delta("bytes", c.bytes)
            rates.append(f"recv {got * 8.0 / window / 1e6:.1f} Mb/s")
        if rates:
            out += " | " + ", ".join(rates)
        return out

    def _flush_pending_mouse(self):
        if not hasattr(self, "_pending_mouse") or self._pending_mouse is None:
            return
        now = time.time()
        if now - self._last_mouse_ts < self._mouse_throttle:
            return
        rx, ry, buttons = self._pending_mouse
        self.send_mouse_packet(2, buttons, rx, ry)
        self._last_mouse_ts = now
        self._pending_mouse = None

    def send_mouse_packet(self, pkt_type, bmask, x, y):
        msg = f"MOUSE_PKT {pkt_type} {bmask} {x} {y}"
        try:
            self.control_callback(msg)
        except Exception:
            pass

    def _scaled_mouse_coords(self, e):
        ww, wh = self.width(), self.height()
        fw, fh = self.texture_width, self.texture_height
        aspect_tex = fw / float(fh)
        aspect_win = ww / float(wh)

        if aspect_win > aspect_tex:
            view_h = wh
            view_w = aspect_tex / aspect_win * ww
            offset_x = (ww - view_w) / 2.0
            offset_y = 0
        else:
            view_w = ww
            view_h = aspect_win / aspect_tex * wh
            offset_x = 0
            offset_y = (wh - view_h) / 2.0

        nx = (e.x() - offset_x) / view_w
        ny = (e.y() - offset_y) / view_h

        nx = min(max(nx, 0.0), 1.0)
        ny = min(max(ny, 0.0), 1.0)

        # Normalised position of the video maps onto the desktop, whatever size
        # the host chose to stream at.
        rx = self.offset_x + int(nx * self.desktop_width)
        ry = self.offset_y + int(ny * self.desktop_height)
        return rx, ry

    def mousePressEvent(self, e):
        bmap = {Qt.LeftButton: 1, Qt.MiddleButton: 2, Qt.RightButton: 4}
        bmask = bmap.get(e.button(), 0)
        if bmask:
            rx, ry = self._scaled_mouse_coords(e)
            self.send_mouse_packet(1, bmask, rx, ry)
        e.accept()

    def mouseMoveEvent(self, e):
        rx, ry = self._scaled_mouse_coords(e)
        buttons = 0
        if e.buttons() & Qt.LeftButton: buttons |= 1
        if e.buttons() & Qt.MiddleButton: buttons |= 2
        if e.buttons() & Qt.RightButton: buttons |= 4

        if not hasattr(self, "_pending_mouse"):
            self._pending_mouse = None
        self._pending_mouse = (rx, ry, buttons)

        self._flush_pending_mouse()
        e.accept()

    def mouseReleaseEvent(self, e):
        bmap = {Qt.LeftButton: 1, Qt.MiddleButton: 2, Qt.RightButton: 4}
        bmask = bmap.get(e.button(), 0)
        if bmask:
            rx, ry = self._scaled_mouse_coords(e)
            self.send_mouse_packet(3, bmask, rx, ry)
        e.accept()

    def wheelEvent(self, e):
        d = e.angleDelta()
        if d.y() != 0:
            b = "4" if d.y() > 0 else "5"
            self.control_callback(f"MOUSE_SCROLL {b}")
        elif d.x() != 0:
            b = "6" if d.x() < 0 else "7"
            self.control_callback(f"MOUSE_SCROLL {b}")
        e.accept()

    def keyPressEvent(self, e):
        if e.isAutoRepeat():
            return
        if e.key() == Qt.Key_F1:
            # Local overlay toggle: do not forward this one to the host.
            if callable(self.toggle_overlay):
                self.toggle_overlay()
            e.accept()
            return
        key_name = self._get_key_name(e)
        if key_name:
            self.control_callback(f"KEY_PRESS {key_name}")
        e.accept()

    def keyReleaseEvent(self, e):
        if e.isAutoRepeat():
            return
        key_name = self._get_key_name(e)
        if key_name:
            self.control_callback(f"KEY_RELEASE {key_name}")
        e.accept()

    def _get_key_name(self, event):
        text = event.text()
        if text and len(text) == 1 and ord(text) >= 0x20 and ord(text) != 0x7f:
            return "space" if text == " " else text
        key = event.key()
        key_map = {
            Qt.Key_Escape: "Escape", Qt.Key_Tab: "Tab", Qt.Key_Backtab: "Tab",
            Qt.Key_Backspace: "BackSpace", Qt.Key_Return: "Return", Qt.Key_Enter: "Return",
            Qt.Key_Insert: "Insert", Qt.Key_Delete: "Delete", Qt.Key_Pause: "Pause",
            Qt.Key_Print: "Print", Qt.Key_Home: "Home", Qt.Key_End: "End",
            Qt.Key_Left: "Left", Qt.Key_Up: "Up", Qt.Key_Right: "Right", Qt.Key_Down: "Down",
            Qt.Key_PageUp: "Page_Up", Qt.Key_PageDown: "Page_Down",
            Qt.Key_Shift: "Shift_L", Qt.Key_Control: "Control_L",
            Qt.Key_Meta: "Super_L", Qt.Key_Alt: "Alt_L", Qt.Key_AltGr: "Alt_R",
            Qt.Key_CapsLock: "Caps_Lock", Qt.Key_NumLock: "Num_Lock",
            Qt.Key_ScrollLock: "Scroll_Lock",
            **{getattr(Qt, f"Key_F{i}"): f"F{i}" for i in range(1, 13)},
        }
        if IS_MAC:
            # Command is the Mac's primary shortcut modifier; forwarding it as
            # Super would hand every Cmd+X to the desktop shell instead of the
            # remote app, so Mac clients send it as the host's Ctrl instead.
            key_map[Qt.Key_Meta] = "Control_L"
        if key in key_map:
            return key_map[key]
        if (Qt.Key_A <= key <= Qt.Key_Z) or (Qt.Key_0 <= key <= Qt.Key_9):
            try:
                return chr(key).lower()
            except Exception:
                pass
        return text or None

class GamepadThread(threading.Thread):
    def __init__(self, host_ip, port, path_hint=None):
        super().__init__(daemon=True)
        self.host_ip = host_ip
        self.port = port
        self.path_hint = path_hint
        self._running = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _find_device(self):
        try:
            from evdev import InputDevice, list_devices
        except Exception:
            return None
        if self.path_hint:
            try:
                return InputDevice(self.path_hint)
            except Exception:
                return None
        candidates = []
        for p in list_devices():
            try:
                d = InputDevice(p)
                name = (d.name or "").lower()
                if any(k in name for k in ("controller", "gamepad", "xbox", "dualshock", "dual sense", "8bitdo", "ps")):
                    candidates.append(d)
                    continue
                caps = d.capabilities(verbose=True)
                if any(n for (typ, codes) in caps for (code, n) in (codes or []) if n.startswith(("BTN_", "ABS_"))):
                    candidates.append(d)
            except Exception:
                pass
        if not candidates:
            return None

        def score(dev):
            s = 0
            try:
                n = (dev.name or "").lower()
                if "controller" in n or "gamepad" in n: s += 5
                if "xbox" in n or "dual" in n or "8bitdo" in n or "ps" in n: s += 3
                caps = dev.capabilities(verbose=True)
                if any((codes or []) for (_t, codes) in caps): s += 1
            except Exception:
                pass
            return s

        candidates.sort(key=score, reverse=True)
        return candidates[0]

    def run(self):
        if not IS_LINUX:
            if IS_MAC:
                logging.info("Gamepad capture is not supported on macOS yet — controller input disabled.")
            return
        try:
            from evdev import ecodes, InputDevice
        except Exception:
            return

        dev = self._find_device()
        if not dev:
            return

        try:
            dev.grab()
        except Exception:
            pass

        pack_event = struct.Struct("!Bhh").pack
        sendto = self.sock.sendto
        addr = (self.host_ip, self.port)

        token = CLIENT_STATE.get("token") or ""
        try:
            sendto(f"GAUTH {token}".encode("utf-8"), addr)
        except Exception:
            pass

        try:
            for event in dev.read_loop():
                if not self._running:
                    break
                t = int(event.type)
                c = int(event.code)
                v = int(event.value)
                if t in (ecodes.EV_KEY, ecodes.EV_ABS, ecodes.EV_SYN):
                    try:
                        sendto(pack_event(t, c, v), addr)
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            dev.ungrab()
        except Exception:
            pass

    def stop(self):
        self._running = False
        try:
            self.sock.close()
        except Exception:
            pass

STATS_HISTORY = 60          # one sample per second -> a one-minute window


class _Series:
    """One sparkline: a labelled ring buffer of samples."""

    def __init__(self, label, unit, color, fmt="{:.1f}"):
        self.label = label
        self.unit = unit
        self.color = color
        self.fmt = fmt
        self.values = []
        self.peak = 0.0

    def push(self, value):
        try:
            value = float(value)
        except Exception:
            return
        self.values.append(value)
        if len(self.values) > STATS_HISTORY:
            del self.values[0]
        self.peak = max(self.values) if self.values else 0.0

    @property
    def latest(self):
        return self.values[-1] if self.values else 0.0

    def text(self):
        return self.fmt.format(self.latest)


class StatsOverlay(QWidget):
    """Translucent on-screen stats panel with live numbers and sparklines.

    Painted with QPainter onto a child widget that ignores mouse events, so it
    cannot interfere with input forwarding or the OpenGL video path.
    """

    PANEL_BG = QColor(10, 12, 16, 205)
    PANEL_BORDER = QColor(255, 255, 255, 45)
    TEXT = QColor(230, 234, 240)
    DIM = QColor(152, 160, 170)
    GRID = QColor(255, 255, 255, 26)

    def __init__(self, provider, parent=None):
        super().__init__(parent)
        self._provider = provider
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.resize(392, 368)
        self.series = {
            "mbps":   _Series("video in", "Mb/s", QColor(84, 220, 148)),
            "enc":    _Series("encoder out", "Mb/s", QColor(110, 150, 255)),
            "fps":    _Series("decode", "fps", QColor(120, 190, 255), "{:.0f}"),
            "rtt":    _Series("latency", "ms", QColor(255, 190, 90), "{:.0f}"),
            "jitter": _Series("jitter", "ms", QColor(255, 140, 120), "{:.1f}"),
            "dec":    _Series("decode", "ms", QColor(150, 210, 160), "{:.1f}"),
            "hcpu":   _Series("host cpu", "%", QColor(255, 120, 200), "{:.0f}"),
            "hgpu":   _Series("host gpu", "%", QColor(205, 205, 120), "{:.0f}"),
            "ccpu":   _Series("client cpu", "%", QColor(190, 150, 255), "{:.0f}"),
            "cgpu":   _Series("client gpu", "%", QColor(150, 220, 220), "{:.0f}"),
            "drop":   _Series("dropped", "/s", QColor(255, 90, 90), "{:.0f}"),
        }
        self.info = []
        self.headline = ""
        self._provider_failed = False
        self._font = QFont("monospace")
        self._font.setStyleHint(QFont.TypeWriter)
        self._font.setPointSize(8)
        self._sample_timer = QTimer(self)
        self._sample_timer.timeout.connect(self._sample)
        self._paint_timer = QTimer(self)
        self._paint_timer.timeout.connect(self.update)

    def start(self):
        self._sample_timer.start(1000)
        self._paint_timer.start(250)
        self.show()
        self.raise_()

    def stop(self):
        self._sample_timer.stop()
        self._paint_timer.stop()
        self.hide()

    def _sample(self):
        try:
            data = self._provider() or {}
        except Exception as e:
            # A persistently failing provider looks exactly like "the overlay
            # shows zeros" — say so loudly once, then keep the noise down.
            if not self._provider_failed:
                self._provider_failed = True
                logging.warning("Stats provider failed (%s) — the overlay will show zeros.", e)
            else:
                logging.debug("Stats provider failed again: %s", e)
            return
        for key, series in self.series.items():
            if data.get(key) is not None:
                series.push(data[key])
        if data.get("info"):
            self.info = data["info"]
            # The graphs sit below the info lines: grow with them rather
            # than clip the bottom row (the panel was sized for four).
            height = 368 + 13 * max(0, len(self.info) - 4)
            if self.height() != height:
                self.resize(self.width(), height)
        if data.get("headline"):
            self.headline = data["headline"]

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setPen(self.PANEL_BORDER)
        painter.setBrush(self.PANEL_BG)
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 8, 8)

        painter.setFont(self._font)
        y = 20
        painter.setPen(self.TEXT)
        painter.drawText(10, y, self.headline or "LinuxPlay")
        y += 15
        painter.setPen(self.DIM)
        for line in self.info:
            painter.drawText(10, y, line)
            y += 13
        y += 6

        cell_w, cell_h = 190, 56
        for i, key in enumerate(self.series):
            col, row = i % 2, i // 2
            x = 10 + col * (cell_w + 4)
            self._draw_series(painter, self.series[key], x, y + row * cell_h,
                              cell_w - 12, cell_h - 8)
        painter.end()

    def _draw_series(self, painter, series, x, y, w, h):
        label_h = 12
        painter.setPen(self.DIM)
        painter.drawText(x, y + 9, series.label)
        painter.setPen(self.TEXT)
        painter.drawText(QRectF(x, y, w, 12), Qt.AlignRight | Qt.AlignVCenter,
                         f"{series.text()} {series.unit}")
        box = QRectF(x, y + label_h, w, max(6, h - label_h))
        painter.setPen(self.GRID)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(box)
        values = series.values
        if len(values) < 2:
            return
        vmax = max(series.peak, 1e-9)
        if not math.isfinite(vmax):
            return                       # one bad sample must not abort the paint
        step = 10 ** math.floor(math.log10(vmax))
        vmax = max(math.ceil(vmax / step) * step, step)
        path = QPainterPath()
        for i, value in enumerate(values):
            px = box.left() + box.width() * i / (STATS_HISTORY - 1)
            py = box.bottom() - box.height() * min(value, vmax) / vmax
            if i == 0:
                path.moveTo(px, py)
            else:
                path.lineTo(px, py)
        pen = QPen(series.color, 1.4)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(path)


class StatusBanner(QLabel):
    """One line of plain-language status across the top of the video.

    Says what the picture and sound cannot: that the client is reconnecting,
    that the picture is frozen waiting for a keyframe, that the host is
    waiting for someone to approve screen sharing, that audio dropped out or
    is muted. Hidden when there is nothing to say; never takes the mouse.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("QLabel { background: rgba(18, 18, 22, 210); color: #f4f4f4;"
                           " border-radius: 8px; padding: 6px 14px; font-size: 13px; }")
        self.hide()

    def show_notices(self, notices):
        text = "   ·   ".join(notices)
        if not text:
            if not self.isHidden():
                self.hide()
            return
        if text != self.text():
            self.setText(text)
            self.adjustSize()
        self.place()
        if self.isHidden():
            self.show()
        self.raise_()

    def place(self):
        parent = self.parentWidget()
        if parent is not None:
            self.move(max(8, (parent.width() - self.width()) // 2), 10)


class MainWindow(QMainWindow):
    def __init__(self, decoder_opts, rwidth, rheight, host_ip, udp_port,
                 offset_x, offset_y, net_mode='lan', parent=None, ultra=False,
                 gamepad="disable", gamepad_dev=None, pin=None, audio=True,
                 stats_visible=False, desktop_size=None, muted=False):
        super().__init__(parent)

        self.window_id = SESSION.register()
        self.monitor_index = udp_port - DEFAULT_UDP_PORT
        self._pin = pin
        self._audio_enabled = audio
        self._audio_muted = muted
        self.setWindowTitle("LinuxPlay")
        self.texture_width, self.texture_height = rwidth, rheight
        self.offset_x, self.offset_y = offset_x, offset_y
        self.host_ip, self.ultra = host_ip, ultra
        self.udp_port = udp_port
        self._running = True
        self.gamepad_mode = gamepad
        self.gamepad_dev = gamepad_dev

        self.control_addr = (host_ip, CONTROL_PORT)
        self.control_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.control_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.control_sock.setblocking(False)

        self.send_control(f"NET {net_mode}")

        self.video_widget = VideoWidgetGL(self.send_control, rwidth, rheight,
                                          offset_x, offset_y, host_ip,
                                          toggle_overlay=self.toggle_stats,
                                          desktop_size=desktop_size)
        self.setCentralWidget(self.video_widget)
        self.video_widget.setFocus()
        self.setAcceptDrops(True)

        self.video_url = self._video_url_for_path()

        self.decoder_opts = dict(decoder_opts)
        logging.debug("Decoder options: %s", self.decoder_opts)

        self._proc = psutil.Process(os.getpid())

        self._start_decoder_thread()
        self._start_background_threads()
        self._start_timers()

        self._stats_visible = bool(stats_visible)
        self._session_start = time.time()
        self._total_bytes = 0
        self._prev_counters = None
        self._prev_drops = None
        self._prev_drops_t = 0.0
        self.overlay = StatsOverlay(self._collect_metrics, self)
        self.overlay.move(12, 12)
        if self._stats_visible:
            self.overlay.start()
            logging.info("Stats overlay enabled (F1 toggles it off)")
        self.banner = StatusBanner(self)
        self._build_menu()

    def _start_timers(self):
        self.clip_timer = QTimer(self)
        self.clip_timer.timeout.connect(self._drain_clipboard_inbox)
        self.clip_timer.start(10)

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._poll_connection_state)
        self.status_timer.start(1000)

        self.stats_timer = QTimer(self)
        self.stats_timer.timeout.connect(self._update_stats)
        self.stats_timer.start(1000)

    def _start_background_threads(self):
        try:
            self._heartbeat_thread = heartbeat_responder(self.host_ip)
        except Exception as e:
            logging.error(f"Heartbeat responder failed: {e}")
            self._heartbeat_thread = None

        try:
            self._audio_thread = audio_listener(self.host_ip, enabled=self._audio_enabled,
                                                muted=self._audio_muted)
        except Exception as e:
            logging.error(f"Audio listener failed: {e}")
            self._audio_thread = None

        try:
            self._clip_thread = clipboard_listener()
        except Exception as e:
            logging.error(f"Clipboard listener failed: {e}")
            self._clip_thread = None

        self._gp_thread = None
        if self.gamepad_mode == "enable" and IS_LINUX:
            try:
                self._gp_thread = GamepadThread(self.host_ip, UDP_GAMEPAD_PORT, self.gamepad_dev)
                self._gp_thread.start()
                logging.info("Controller forwarding started from %s -> %s",
                             self.gamepad_dev or "/dev/input/event*",
                             f"{self.host_ip}:{UDP_GAMEPAD_PORT}")
            except Exception as e:
                logging.error("Gamepad thread failed: %s", e)
                self._gp_thread = None

    def _video_url_for_path(self):
        """Recompute the video input URL, re-probing the path MTU to the host.

        Called on every decoder (re)start so a client that moved between
        Wi-Fi, Ethernet and Tailscale picks up the new path MTU instead of a
        stale one. rw_timeout matches the audio listener: a blackholed stream
        then fails and is restarted instead of freezing on the last frame.
        """
        mtu_guess = _route_mtu(self.host_ip) or 1500
        try:
            env_mtu = int(os.environ.get("LINUXPLAY_MTU", "1500") or 1500)
            if env_mtu > 0:
                mtu_guess = min(mtu_guess, env_mtu)
        except Exception:
            pass
        pkt = _best_ts_pkt_size(mtu_guess, ":" in str(self.host_ip))
        if mtu_guess < 1500:
            logging.info("Path MTU to %s is %d → TS pkt_size %d (no IP fragmentation).",
                         self.host_ip, mtu_guess, pkt)
        relay = getattr(self, "video_relay", None)
        bind = (f"127.0.0.1:{self.udp_port + VIDEO_RELAY_OFFSET}" if relay is not None
                else f"0.0.0.0:{self.udp_port}")
        return (
            f"udp://@{bind}"
            f"?pkt_size={pkt}"
            f"&reuse=1&buffer_size=4194304&fifo_size=131072"
            f"&overrun_nonfatal=1&max_delay=0&rw_timeout=5000000"
        )

    def _start_video_relay(self):
        """Put the host's numbered (RTP) video datagrams back in order.

        Runs whatever the host answered: an older host's plain MPEG-TS passes
        straight through. LINUXPLAY_VIDEO_RELAY=0 reads the port directly.
        """
        old = getattr(self, "video_relay", None)
        if old is not None and old.is_alive():
            return                      # it owns the port: a second one would fail to bind
        self.video_relay = None
        if os.environ.get("LINUXPLAY_VIDEO_RELAY", "1") == "0":
            return
        try:
            self.video_relay = seqrelay.ReorderRelay(
                self.udp_port, self.udp_port + VIDEO_RELAY_OFFSET,
                wait=VIDEO_REORDER_WAIT, name=f"Video {self.monitor_index}")
        except OSError as e:
            logging.warning("Video reorder relay unavailable on UDP %d (%s) — reading "
                            "the stream directly.", self.udp_port, e)
            self.video_relay = None
            return
        self.video_relay.start()

    def _start_decoder_thread(self):
        self._start_video_relay()
        self.video_url = self._video_url_for_path()
        self.decoder_thread = DecoderThread(self.video_url, self.decoder_opts,
                                            ultra=self.ultra,
                                            url_provider=self._video_url_for_path)
        # Queued, not Direct: updateFrame touches widget state (isVisible,
        # update, texture bookkeeping) that must stay on the GUI thread.
        self.decoder_thread.frame_ready.connect(self.video_widget.updateFrame,
                                                Qt.QueuedConnection)
        self.video_widget.stream_counters = self.decoder_thread.counters
        self.decoder_thread.start()
        logging.info("Decoder thread started")

    def _build_menu(self):
        """macOS menu bar: Audio ▸ Mute Host Audio (⌘⇧M), View ▸ Stats Overlay.

        The menu bar lives in the system bar on macOS, so it costs the video
        nothing; other platforms keep upstream's bare window. A menu shortcut
        is consumed here, never forwarded to the host as a key press.
        """
        if not IS_MAC:
            return
        bar = self.menuBar()
        audio = bar.addMenu("Audio")
        self.mute_action = QAction("Mute Host Audio", self)
        self.mute_action.setCheckable(True)
        self.mute_action.setShortcut(QKeySequence("Ctrl+Shift+M"))
        self.mute_action.setChecked(bool(AUDIO_STATE["muted"] or self._audio_muted))
        self.mute_action.setEnabled(bool(self._audio_enabled))
        self.mute_action.toggled.connect(set_audio_muted)
        audio.addAction(self.mute_action)
        view = bar.addMenu("View")
        stats = QAction("Toggle Stats Overlay (F1)", self)
        stats.triggered.connect(self.toggle_stats)
        view.addAction(stats)

    def _banner_notices(self, now=None):
        """What the status banner should say right now (most urgent first)."""
        now = time.time() if now is None else now
        out = []
        if CLIENT_STATE.get("reconnecting") or not CLIENT_STATE.get("connected"):
            out.append("Reconnecting to the host…")
        note = CLIENT_STATE.get("host_note")
        if note and now - note[1] < 3.0:
            out.append({"portal-wait": "The host is waiting for screen-share approval"}
                       .get(note[0], note[0]))
        dt = getattr(self, "decoder_thread", None)
        if dt is not None:
            gate = getattr(dt, "gate", None)
            if not dt._has_first_frame and now - self._session_start > 3.0:
                out.append("Waiting for video…")
            elif (gate is not None and gate.waiting and gate.reason == "loss"
                  and now - gate.since > 1.0):
                out.append("Picture frozen — waiting for a keyframe")
            if dt.hw_fallback_at and now - dt.hw_fallback_at < 15.0:
                out.append("Video decoding fell back to the CPU")
        if self._audio_enabled:
            if AUDIO_STATE["muted"]:
                out.append("Muted")
            elif now - AUDIO_STATE["last_glitch"] < 5.0:
                out.append("Audio dropped out — restarting")
        return out

    def _poll_connection_state(self):
        now = time.time()
        try:
            self.banner.show_notices(self._banner_notices(now))
        except Exception as e:
            logging.debug("Status banner update failed: %s", e)
        age = now - CLIENT_STATE.get("last_heartbeat", 0)
        if age > 6 and CLIENT_STATE["connected"]:
            CLIENT_STATE["connected"], CLIENT_STATE["reconnecting"] = False, True
            logging.warning("Lost heartbeat from host")
            CLIENT_STATE["last_reauth_attempt"] = now
            threading.Thread(target=attempt_rehandshake, args=(self.host_ip, self._pin), daemon=True).start()
        elif age > 6 and CLIENT_STATE["reconnecting"]:
            if now - CLIENT_STATE.get("last_reauth_attempt", 0.0) > 5:
                CLIENT_STATE["last_reauth_attempt"] = now
                threading.Thread(target=attempt_rehandshake, args=(self.host_ip, self._pin), daemon=True).start()
        elif age <= 6 and CLIENT_STATE["reconnecting"]:
            CLIENT_STATE["connected"], CLIENT_STATE["reconnecting"] = True, False
            logging.info("Heartbeat restored")

    def _read_gpu_percent(self):
        """Client GPU utilisation as a number plus its vendor (None if unknown)."""
        try:
            import pynvml
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            return float(util.gpu), "NVENC"
        except Exception:
            pass

        if IS_LINUX:
            # sysfs and GNU timeout + intel_gpu_top exist only on Linux; on
            # macOS/Windows these two probes can only fail, and they run on the
            # GUI thread once a second (twice with the overlay up).
            try:
                for card in os.listdir("/sys/class/drm"):
                    busy_path = f"/sys/class/drm/{card}/device/gpu_busy_percent"
                    if os.path.exists(busy_path):
                        with open(busy_path, "r") as f:
                            return float(f.read().strip()), "VAAPI"
            except Exception:
                pass

            try:
                cmd = ["timeout", "0.5", "intel_gpu_top", "-J"]
                out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode()
                if '"Busy"' in out:
                    j = json.loads(out)
                    return float(j["engines"]["Render/3D/0"]["busy"]), "iGPU"
            except Exception:
                pass

        return None, ""

    def _read_gpu_usage(self):
        pct, vendor = self._read_gpu_percent()
        if pct is None:
            return "N/A"
        return f"{pct:.0f}% ({vendor})"

    def _collect_metrics(self):
        """Snapshot everything the stats overlay draws (called once per second)."""
        now = time.time()
        host_stats = CLIENT_STATE.get("host_stats") or {}
        data = {"info": []}

        counters = getattr(getattr(self, "decoder_thread", None), "counters", None)
        if counters is not None:
            snap = counters.snapshot()
            prev = self._prev_counters
            if prev:
                dt = max(0.2, now - prev["t"])
                if snap[0] >= prev["bytes"] and snap[3] >= prev["frames"]:
                    data["mbps"] = (snap[0] - prev["bytes"]) * 8.0 / dt / 1e6
                    data["fps"] = (snap[3] - prev["frames"]) / dt
                    keyframes = (snap[2] - prev["keyframes"]) / dt
                    self._total_bytes += snap[0] - prev["bytes"]
                    data["keyframes"] = keyframes
            self._prev_counters = {"t": now, "bytes": snap[0],
                                   "frames": snap[3], "keyframes": snap[2]}

        try:
            data["ccpu"] = _client_cpu_percent(self._proc)
        except Exception:
            pass
        try:
            data["cgpu"] = self._read_gpu_percent()[0]
        except Exception:
            pass
        data["dec"] = getattr(getattr(self, "decoder_thread", None),
                              "_avg_decode_time", None)

        for src, dst in (("cpu", "hcpu"), ("gpu", "hgpu"), ("rtt", "rtt"),
                         ("jitter", "jitter")):
            if host_stats.get(src) is not None:
                data[dst] = host_stats[src]
        enc_kbps = host_stats.get("enc_kbps")
        if enc_kbps:
            data["enc"] = enc_kbps / 1000.0

        drops = host_stats.get("drops")
        if drops is not None:
            if self._prev_drops is not None and drops >= self._prev_drops:
                dt = max(0.2, now - self._prev_drops_t)
                data["drop"] = (drops - self._prev_drops) / dt
            self._prev_drops, self._prev_drops_t = drops, now

        uptime = int(now - self._session_start)
        backend = getattr(self.video_widget, "render_backend", "OpenGL")
        hw = getattr(getattr(self, "decoder_thread", None), "_hw_name", None) or "CPU"
        status = ("connected" if CLIENT_STATE["connected"]
                  else ("reconnecting…" if CLIENT_STATE["reconnecting"] else "idle"))
        enc_txt = f"{enc_kbps / 1000:.1f} Mb/s" if enc_kbps else "—"
        kf_txt = f"{data.get('keyframes', 0):.0f}/s" if "keyframes" in data else "—"
        data["headline"] = (f"LinuxPlay · {self.host_ip}:{self.udp_port} · "
                            f"{self.texture_width}x{self.texture_height}")
        dt = getattr(self, "decoder_thread", None)
        restarts = getattr(dt, "reconnects", 0)
        decode = f"decode {hw}"
        if dt is not None and dt._sw_fallback_done and dt._hw_wanted:
            left = (dt._hw_retry_at - now) / 60.0 if dt._hw_retry_at else None
            decode += (f" (hardware failed; retry in {left:.0f} min)" if left and left > 0
                       else " (hardware failed)")
        c = getattr(dt, "counters", None)
        loss = ""
        if c is not None:
            # Session totals of what the viewer lost to the network: frames
            # that arrived damaged, frames held back until the next keyframe,
            # and how long the picture stood still waiting for it.
            loss = (f"loss {c.damaged} damaged · {c.skipped} held · frozen "
                    f"{c.freeze_ms / 1000.0:.1f}s total, {c.freezes} times")
        data["info"] = [
            f"link {CLIENT_STATE.get('net_mode', 'lan')} · {status} · "
            f"{uptime // 60:02d}:{uptime % 60:02d} up · {restarts} reconnects",
            f"{decode} · render {backend}",
            *([loss] if loss else []),
            f"host encode {enc_txt} @ {host_stats.get('fps', 0):.0f} fps"
        f"{'' if host_stats.get('window') else ' (session avg)'} · "
            f"cpu {host_stats.get('cpu', 0):.0f}% gpu {host_stats.get('gpu', 0):.0f}%",
            f"received {self._total_bytes / 1e6:.0f} MB · keyframes {kf_txt}",
        ]
        return data

    def toggle_stats(self):
        """F1: show or hide the on-screen stats panel."""
        self._stats_visible = not self._stats_visible
        if self._stats_visible:
            self.overlay.start()
        else:
            self.overlay.stop()
        logging.info("Stats overlay %s (F1 toggles)",
                     "shown" if self._stats_visible else "hidden")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        try:
            self.overlay.move(12, 12)
            self.overlay.raise_()
            self.banner.place()
        except Exception:
            pass

    def _update_stats(self):
        try:
            cpu = self._proc.cpu_percent(interval=None)
            mem = self._proc.memory_info().rss / (1024 * 1024)
            gpu = self._read_gpu_usage()
            fps = getattr(self.video_widget, "_fps", 0.0)
            backend = getattr(self.video_widget, "render_backend", "OpenGL")

            base_title = "LinuxPlay"
            status = ""
            if not CLIENT_STATE["connected"]:
                status = " | RECONNECTING…"
            elif CLIENT_STATE["reconnecting"]:
                status = " | Weak Signal"

            new_title = (
                f"{base_title} — {backend} | "
                f"FPS: {fps:.0f} | CPU: {cpu:.0f}% | RAM: {mem:.0f} MB | GPU: {gpu}{status}"
            )
            self.setWindowTitle(new_title)
        except Exception as e:
            logging.debug(f"Stats update failed: {e}")

    def _drain_clipboard_inbox(self):
        changed = False
        while not CLIPBOARD_INBOX.empty():
            text = CLIPBOARD_INBOX.get_nowait()
            cb = QApplication.clipboard()
            current = cb.text()
            if text and text != current:
                self.video_widget.ignore_clipboard = True
                cb.setText(text)
                self.video_widget.ignore_clipboard = False
                changed = True
        if changed:
            self.video_widget.last_clipboard = QApplication.clipboard().text()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls:
            event.ignore()
            return

        files_to_upload = []
        for url in urls:
            path = url.toLocalFile()
            if os.path.isdir(path):
                for root, _, files in os.walk(path):
                    for f in files:
                        files_to_upload.append(os.path.join(root, f))
            elif os.path.isfile(path):
                files_to_upload.append(path)

        for fpath in files_to_upload:
            threading.Thread(target=self.upload_file, args=(fpath,), daemon=True).start()
        event.acceptProposedAction()

    def upload_file(self, file_path):
        try:
            token = CLIENT_STATE.get("token")
            if not token:
                logging.error(f"Upload refused for {file_path}: no session token yet.")
                return
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(20)      # a blackholed host must not wedge the thread
                sock.connect((self.control_addr[0], UDP_FILE_PORT))
                # Every other channel is token-gated; the upload channel is too.
                sock.sendall(f"AUTH {token}\n".encode("utf-8"))
                filename = os.path.basename(file_path).encode("utf-8")
                header = len(filename).to_bytes(4, "big") + filename
                size = os.path.getsize(file_path)
                header += size.to_bytes(8, "big")
                sock.sendall(header)
                with open(file_path, "rb") as f:
                    while True:
                        chunk = f.read(4096)
                        if not chunk:
                            break
                        sock.sendall(chunk)
            logging.info(f"Uploaded: {file_path}")
        except Exception as e:
            logging.error(f"Upload error for {file_path}: {e}")

    def send_control(self, msg):
        token = CLIENT_STATE.get("token")
        payload = f"AUTH {token} {msg}" if token else msg
        try:
            self.control_sock.sendto(payload.encode("utf-8"), self.control_addr)
        except Exception as e:
            logging.error(f"Control send error: {e}")

    def closeEvent(self, event):
        self._running = False
        try:
            self.overlay.stop()
        except Exception:
            pass
        logging.info("Closing client window…")
        try:
            self.send_control(f"WINDOW_CLOSE {self.monitor_index}")
        except Exception as e:
            logging.debug(f"WINDOW_CLOSE send failed: {e}")
        remaining = SESSION.unregister()
        if remaining == 0:
            CLIENT_STATE["connected"] = False
            try:
                self.send_control("GOODBYE")
                logging.info("Sent GOODBYE to host (last window closed)")
            except Exception as e:
                logging.debug(f"GOODBYE send failed: {e}")

        for timer_name in ("clip_timer", "status_timer", "stats_timer"):
            timer = getattr(self, timer_name, None)
            if timer:
                try:
                    timer.stop()
                except Exception:
                    pass

        if hasattr(self, "decoder_thread"):
            try:
                self.decoder_thread.stop()
                self.decoder_thread.wait(2000)
            except Exception as e:
                logging.debug(f"Decoder cleanup error: {e}")
        if getattr(self, "video_relay", None) is not None:
            self.video_relay.stop()

        if getattr(self, "_gp_thread", None):
            try:
                self._gp_thread.stop()
            except Exception:
                pass

        if remaining == 0:
            # One ffplay serves every monitor window, so only the last window
            # may stop it — closing one of several must not kill audio for all.
            global audio_proc
            report = audio_report()
            if report:
                logging.info(report)
            audio_stop.set()
            if audio_proc:
                try:
                    audio_proc.terminate()
                    audio_proc.wait(timeout=2)
                except Exception as e:
                    logging.error(f"ffplay term error: {e}")
                audio_proc = None

        try:
            self.control_sock.close()
        except Exception:
            pass

        event.accept()

def keep_previous_logs(path, keep=3):
    """Shift path → path.1 → … → path.<keep> before a run starts a fresh log.

    Opening the log with mode "w" alone erased the only record of the last
    session the moment the client was launched again — on 2026-10-19 the run
    that had kept the host busy all night was gone by the time anyone looked.
    """
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return
        for i in range(keep - 1, 0, -1):
            if os.path.exists(f"{path}.{i}"):
                os.replace(f"{path}.{i}", f"{path}.{i + 1}")
        os.replace(path, f"{path}.1")
    except OSError:
        pass

def main():
    import os, sys, argparse, psutil, time, logging
    from PyQt5.QtWidgets import QApplication, QMessageBox
    from PyQt5.QtGui import QSurfaceFormat

    p = argparse.ArgumentParser(description="LinuxPlay Client (Linux/Windows/macOS)")
    p.add_argument("--decoder", choices=["none", "h.264", "h.265"], default="none",
                   help="Expected stream codec; a mismatch with the host is logged.")
    p.add_argument("--host_ip", required=True)
    p.add_argument("--pin", default=None, help="6-digit host PIN (optional; will prompt if required)")
    p.add_argument("--audio", choices=["enable", "disable"], default="disable")
    p.add_argument("--mute", action="store_true",
                   help="Start with the host's audio muted (Audio ▸ Mute Host Audio toggles it).")
    p.add_argument("--monitor", default="0", help="Index or 'all'")
    p.add_argument("--hwaccel", choices=["auto", "cpu", "cuda", "qsv", "d3d11va", "dxva2", "vaapi", "videotoolbox"], default="auto")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--stats", action="store_true",
                   help="Show the on-screen stats overlay at startup (F1 toggles it).")
    p.add_argument("--net", choices=["auto", "lan", "wifi", "vpn"], default="auto")
    p.add_argument("--ultra", action="store_true", help="Enable ultra-low-latency (LAN only). Auto-disabled on Wi-Fi/WAN.")
    p.add_argument("--gamepad", choices=["enable", "disable"], default="enable")
    p.add_argument("--gamepad_dev", default=None)
    args = p.parse_args()

    for var in list(os.environ):
        if var.startswith(("MESA_", "LIBGL_", "__GL_", "QT_LOGGING", "vblank_mode")):
            del os.environ[var]

    if IS_WINDOWS:
        os.environ["QT_OPENGL"] = "angle"
        os.environ["QT_ANGLE_PLATFORM"] = "d3d11"
    else:
        os.environ.setdefault("QT_OPENGL", "desktop")
        if IS_LINUX:
            # XCB-only integration hint: it is what drops the compositor's
            # vblank wait on X11. macOS runs the Cocoa platform plugin.
            os.environ.setdefault("QT_XCB_GL_INTEGRATION", "xcb_egl")

    fmt = QSurfaceFormat()
    fmt.setSwapInterval(0)
    # Single buffering skips the swap wait on X11, but Cocoa composites the
    # window through the WindowServer, so there a single-buffered surface is
    # drawn straight to screen and flickers (QSurfaceFormat docs); macOS keeps
    # the platform's double-buffered default and the swap interval of 0.
    if not IS_MAC:
        fmt.setSwapBehavior(QSurfaceFormat.SingleBuffer)
    QSurfaceFormat.setDefaultFormat(fmt)

    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    try:
        ps = psutil.Process(os.getpid())
        ps.nice(-5)
    except Exception:
        pass

    LOG_LEVEL = logging.DEBUG if args.debug else logging.INFO
    LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
    LOG_DATEFMT = "%H:%M:%S"

    root_logger = logging.getLogger()
    root_logger.setLevel(LOG_LEVEL)
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(LOG_LEVEL)
    console.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    root_logger.addHandler(console)

    try:
        keep_previous_logs("linuxplay_client.log")
        file_handler = logging.FileHandler("linuxplay_client.log", mode="w", encoding="utf-8")
        file_handler.setLevel(LOG_LEVEL)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
        root_logger.addHandler(file_handler)
    except Exception:
        pass

    # FFmpeg's own transport-stream complaints are the only direct evidence of
    # packets lost in flight; PyAV hides them unless asked.
    _enable_libav_logging()

    logging.info("────────────────────────────────────────────")
    logging.info("LinuxPlay Client starting up")
    logging.info(f"Python: {sys.version.split()[0]}, Platform: {sys.platform}")
    logging.info("────────────────────────────────────────────")

    app = QApplication(sys.argv)

    ok, host_info = tcp_handshake_client(args.host_ip, args.pin)
    if not ok or not host_info:
        QMessageBox.critical(None, "Handshake Failed", "Could not negotiate with host.")
        sys.exit(1)
    host_encoder, monitor_info_str = host_info[0], host_info[1]
    stream_size = _size_from_text(host_info[2]) if len(host_info) > 2 else None
    # PyAV decodes whatever the stream carries; --decoder only lets the user
    # state what they expect, so surface a mismatch instead of ignoring it.
    if args.decoder not in ("none", host_encoder):
        logging.warning("Host streams %s but --decoder %s was requested — "
                        "decoding the stream as sent.", host_encoder, args.decoder)
    CLIENT_STATE["connected"] = True
    CLIENT_STATE["last_heartbeat"] = time.time()
    logging.info("Host streams %s; desktop %s",
                 f"{stream_size[0]}x{stream_size[1]}" if stream_size else "at native size",
                 monitor_info_str)

    net_mode = args.net
    if net_mode == "auto":
        try:
            net_mode = detect_network_mode(args.host_ip)
        except Exception:
            net_mode = "lan"
    logging.info(f"Network mode: {net_mode}")

    ultra_active = args.ultra and (net_mode == "lan")
    if args.ultra and not ultra_active:
        logging.info("Ultra requested but disabled on %s; using safe buffering.", net_mode)
    elif ultra_active:
        logging.info("Ultra mode enabled (LAN): minimal buffering, no B-frame reordering.")

    try:
        monitors = []
        parts = [p for p in monitor_info_str.split(";") if p]
        for part in parts:
            if "+" in part:
                res, ox, oy = part.split("+")
                w, h = map(int, res.split("x"))
                monitors.append((w, h, int(ox), int(oy)))
            else:
                w, h = map(int, part.split("x"))
                monitors.append((w, h, 0, 0))
        if not monitors:
            raise ValueError
    except Exception:
        logging.error("Monitor parse error, defaulting to %s", DEFAULT_RESOLUTION)
        w, h = map(int, DEFAULT_RESOLUTION.split("x"))
        monitors = [(w, h, 0, 0)]

    chosen = args.hwaccel
    if chosen == "auto":
        chosen = choose_auto_hwaccel()
    logging.info(f"HW accel selected: {chosen}")

    decoder_opts = {}
    if chosen != "cpu":
        decoder_opts["hwaccel"] = chosen
        if chosen == "vaapi":
            decoder_opts["hwaccel_device"] = "/dev/dri/renderD128"

    if ultra_active:
        decoder_opts.update({
            "fflags": "nobuffer+noparse+nofillin",
            "flags": "low_delay",
            "flags2": "+fast",
            "probesize": "32",
            "analyzeduration": "0",
            "rtbufsize": "512k",
            "threads": "1",
            "skip_frame": "noref",
        })

    windows = []
    if args.monitor.lower() == "all":
        for i, (w, h, ox, oy) in enumerate(monitors):
            win = MainWindow(decoder_opts, *(stream_size or (w, h)), args.host_ip,
                             DEFAULT_UDP_PORT + i,
                             ox, oy, net_mode, ultra=ultra_active,
                             gamepad=args.gamepad, gamepad_dev=args.gamepad_dev, pin=args.pin,
                             audio=(args.audio == "enable"), stats_visible=args.stats,
                             desktop_size=(w, h), muted=args.mute)
            win.setWindowTitle(f"LinuxPlay — Monitor {i}")
            win.show()
            windows.append(win)
    else:
        try:
            idx = int(args.monitor)
        except Exception:
            idx = 0
        if idx < 0 or idx >= len(monitors):
            idx = 0
        w, h, ox, oy = monitors[idx]
        win = MainWindow(decoder_opts, *(stream_size or (w, h)), args.host_ip,
                         DEFAULT_UDP_PORT + idx,
                         ox, oy, net_mode, ultra=ultra_active,
                         gamepad=args.gamepad, gamepad_dev=args.gamepad_dev, pin=args.pin,
                         audio=(args.audio == "enable"), stats_visible=args.stats,
                         desktop_size=(w, h), muted=args.mute)
        win.setWindowTitle(f"LinuxPlay — Monitor {idx}")
        win.show()
        windows.append(win)

    ret = app.exec_()

    audio_stop.set()
    try:
        if audio_proc:
            audio_proc.terminate()
    except Exception as e:
        logging.error("ffplay term error: %s", e)

    sys.exit(ret)

if __name__ == "__main__":
    main()
