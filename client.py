#!/usr/bin/env python3
import os
import sys
import mmap
import ctypes
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
                             QInputDialog, QWidget)
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QTimer, QRectF
from PyQt5.QtGui import QSurfaceFormat, QPainter, QPen, QColor, QFont, QPainterPath

from OpenGL.GL import *

DEFAULT_UDP_PORT = 5000
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

def _parse_ok_response(resp):
    """Parse 'OK:<encoder>:<monitors>\\nTOKEN <hex>[\\nCERT <b64>]' responses.

    Returns ((encoder, monitors), token, cert_b64).
    """
    lines = [l.strip() for l in resp.splitlines() if l.strip()]
    parts = lines[0].split(":", 2)
    host_encoder = parts[1].strip() if len(parts) > 1 else "none"
    monitor_info = parts[2].strip() if len(parts) > 2 else DEFAULT_RESOLUTION
    token, cert_b64 = "", ""
    for ln in lines[1:]:
        if ln.startswith("TOKEN "):
            token = ln.split(None, 1)[1].strip()
        elif ln.startswith("CERT "):
            cert_b64 = ln.split(None, 1)[1].strip()
    return (host_encoder, monitor_info), token, cert_b64

def _install_issued_cert(cert_b64, private_key):
    """Save a host-issued certificate (from a KEYREQ handshake) next to this script."""
    try:
        from cryptography.hazmat.primitives import serialization
        here = os.path.dirname(os.path.abspath(__file__))
        cert_path = os.path.join(here, "client_cert.pem")
        key_path = os.path.join(here, "client_key.pem")
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

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(8)
    try:
        logging.info("Handshake to %s:%s", host_ip, TCP_HANDSHAKE_PORT)
        sock.connect((host_ip, TCP_HANDSHAKE_PORT))
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            cert_path = os.path.join(here, 'client_cert.pem')
            key_path  = os.path.join(here, 'client_key.pem')
        except Exception:
            cert_path = 'client_cert.pem'; key_path = 'client_key.pem'
        if os.path.exists(cert_path) and os.path.exists(key_path):
            fp_hex = _read_pem_cert_fingerprint(cert_path)
            if fp_hex:
                try:
                    sock.sendall(f'HELLO CERTFP:{fp_hex}'.encode('utf-8'))
                    resp = sock.recv(4096).decode('utf-8', errors='replace').strip()
                    if resp.startswith('CHALLENGE '):
                        nonce_hex = resp.split(None, 1)[1].strip()
                        proof = _build_client_proof(cert_path, key_path, nonce_hex)
                        if proof is None:
                            logging.warning('Host requires certificate proof but signing failed — falling back to PIN.')
                        else:
                            sock.sendall(proof.encode('utf-8'))
                            resp = sock.recv(8192).decode('utf-8', errors='replace').strip()
                    logging.debug('Cert handshake response: %s', resp.splitlines()[0] if resp else '(empty)')
                    if resp.startswith('OK:'):
                        info, token, _cert = _parse_ok_response(resp)
                        if token:
                            CLIENT_STATE['token'] = token
                        sock.close()
                        CLIENT_STATE['connected'] = True
                        CLIENT_STATE['last_heartbeat'] = time.time()
                        logging.info('Authenticated via client certificate (FP %s…) — PIN skipped', fp_hex[:12])
                        return (True, info)
                    elif resp.startswith('FAIL:UNTRUSTEDCERT'):
                        logging.warning('Client cert not yet trusted by host — falling back to PIN.')
                    else:
                        logging.warning('Unexpected response to CERTFP auth (%s) — falling back to PIN', resp)
                except Exception as _e:
                    logging.debug('CERTFP path failed: %s — falling back to PIN', _e)

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

        # First-time pairing: offer the host our public key so it can issue a
        # certificate we store locally — the private key never leaves this machine.
        generated_key = None
        keyreq_line = ""
        if not (os.path.exists(cert_path) and os.path.exists(key_path)):
            keyreq_b64, generated_key = _make_keyreq()
            if keyreq_b64:
                keyreq_line = "\nKEYREQ " + keyreq_b64

        # The host asks its own user to approve a first-time pairing, so this
        # wait can be much longer than the 8s connect/handshake timeout.
        sock.settimeout(PAIR_APPROVAL_TIMEOUT + 15)
        sock.sendall(f"HELLO {code}{keyreq_line}".encode("utf-8"))
        resp = sock.recv(8192).decode("utf-8", errors="replace").strip()
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

def heartbeat_responder(host_ip):
    def loop():
        first_ping = True
        first_stats = True
        last_stats = 0.0
        last_stats_warn = 0.0
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

def clipboard_listener(app_clipboard):
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
                            app_clipboard.blockSignals(True)
                            app_clipboard.setText(text)
                            app_clipboard.blockSignals(False)
                except Exception:
                    time.sleep(0.2)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t

audio_stop = threading.Event()
_audio_listener_started = False

def audio_listener(host_ip, enabled=True):
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

    def loop():
        global audio_proc
        url = f"udp://@0.0.0.0:{UDP_AUDIO_PORT}?overrun_nonfatal=1&buffer_size=1048576&rw_timeout=5000000"
        restarts = 0
        while not audio_stop.is_set():
            cmd = [
                "ffplay",
                "-hide_banner", "-loglevel", "info",
                "-nodisp", "-autoexit",
                "-fflags", "nobuffer+discardcorrupt",
                "-af", afilter,
                "-f", "mpegts",
                url,
            ]
            try:
                logging.debug("Audio listener command: %s", " ".join(cmd))
                audio_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0            # binary: the reader below parses bytes
                )
                last_clock = None
                last_progress = time.time()
                buf = b""
                while True:
                    ready, _, _ = select.select([audio_proc.stdout], [], [], 1.0)
                    if not ready:
                        if (last_clock is not None
                                and time.time() - last_progress > AUDIO_STALL_SECS):
                            logging.warning(
                                "Audio stalled for %.0fs (clock stuck at %.1fs) — restarting the player.",
                                AUDIO_STALL_SECS, last_clock)
                            try:
                                # Release UDP 6001 before the restart, or the
                                # replacement player would receive nothing.
                                audio_proc.terminate()
                            except Exception:
                                pass
                            break
                        if audio_proc.poll() is not None:
                            break
                        continue
                    chunk = audio_proc.stdout.read(4096)
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
                        elif "Audio:" in line:
                            logging.info(line)
                    if buf and len(buf) > 8192:      # runaway line: drop it
                        buf = b""
                try:
                    audio_proc.wait(timeout=2)
                except Exception:
                    pass
            except Exception as e:
                logging.error("Audio listener failed: %s", e)
            finally:
                audio_proc = None
            if audio_stop.is_set():
                break
            restarts += 1
            log = logging.warning if restarts <= 2 else logging.debug
            log("Audio player exited — restarting in 1s (glitch recovery, restart #%d).",
                restarts)
            audio_stop.wait(1.0)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t

class StreamCounters:
    """Byte/frame counters filled by the decode loop, read by the stats overlay."""

    def __init__(self):
        self.bytes = 0
        self.packets = 0
        self.keyframes = 0
        self.frames = 0
        self.errors = 0

    def snapshot(self):
        return (self.bytes, self.packets, self.keyframes, self.frames)


def _counted_decode(container, counters, **kwargs):
    """container.decode(), but counting demuxed bytes/packets/keyframes.

    PyAV's decode() is literally demux+decode, so this yields the same frames
    while giving the overlay real receive-rate numbers.
    """
    for packet in container.demux(**kwargs):
        try:
            counters.bytes += int(packet.size or 0)
            counters.packets += 1
            if packet.is_keyframe:
                counters.keyframes += 1
        except Exception:
            counters.errors += 1
        for frame in packet.decode():
            counters.frames += 1
            yield frame


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
    fy, fx = max(1, y.shape[0] // max(1, u.shape[0])), max(1, y.shape[1] // max(1, u.shape[1]))
    if fx > 1 or fy > 1:
        u = np.repeat(np.repeat(u, fy, axis=0), fx, axis=1)
        v = np.repeat(np.repeat(v, fy, axis=0), fx, axis=1)
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
          <encoder kbit/s> <dropped frames> <encoded frames>
    Older hosts send fewer fields; missing ones are simply absent.
    """
    parts = msg.split()
    if len(parts) < 3 or parts[0] != "STATS":
        return None
    names = ("cpu", "gpu", "mem", "fps", "rtt", "jitter",
             "enc_kbps", "drops", "frame")
    out = {}
    for name, raw in zip(names, parts[1:]):
        try:
            out[name] = float(raw)
        except Exception:
            pass
    return out or None


class DecoderThread(QThread):
    frame_ready = pyqtSignal(object)

    def __init__(self, input_url, decoder_opts, ultra=False):
        super().__init__()
        self.input_url = input_url
        self.decoder_opts = dict(decoder_opts or {})
        self.decoder_opts.setdefault("probesize", "32")
        self.decoder_opts.setdefault("analyzeduration", "0")
        self.decoder_opts.setdefault("scan_all_pmts", "1")
        self.decoder_opts.setdefault("fflags", "nobuffer")
        self.decoder_opts.setdefault("flags", "low_delay")
        self.decoder_opts.setdefault("reorder_queue_size", "0")
        self.decoder_opts.setdefault("rtbufsize", "2M")
        self.decoder_opts.setdefault("fpsprobesize", "1")

        self._running = True
        self._sw_fallback_done = False
        self.ultra = ultra
        self._emit_interval = 0.0
        self._last_emit = 0.0
        self._frame_count = 0
        self._avg_decode_time = 0.0
        self._restart_delay = 0.5
        self._last_error = ""
        self._has_first_frame = False
        self._hw_name = None
        self.counters = StreamCounters()
        self._hwaccel = None

    def _open_container(self):
        logging.debug("Opening stream with opts: %s", self.decoder_opts)
        if self._hwaccel is not None:
            return av.open(self.input_url, format="mpegts",
                           options=self.decoder_opts, hwaccel=self._hwaccel)
        return av.open(self.input_url, format="mpegts", options=self.decoder_opts)

    def run(self):
        while self._running:
            container = None
            try:
                container = self._open_container()
                vstream = next((s for s in container.streams if s.type == "video"), None)
                if not vstream:
                    logging.warning("No video stream detected, retrying...")
                    time.sleep(0.5)
                    continue
                    
                cc = vstream.codec_context
                cc.thread_count = 1 if self.ultra else 2

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

                hw_device = getattr(cc, "hw_device_ctx", None)
                if hw_device is None and "hwaccel" in self.decoder_opts:
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
                        self.decoder_opts.pop("hwaccel", None)
                        self.decoder_opts.pop("hwaccel_device", None)

                hw_frames = None
                t_decode = []

                for frame in _counted_decode(container, self.counters, video=0):
                    if not self._running:
                        break
                    if not frame or frame.is_corrupt:
                        continue

                    t0 = time.perf_counter()
                    dmabuf_fd = None

                    try:
                        if frame.hw_frames_ctx:
                            hw_frames = frame.hw_frames_ctx
                        if hasattr(frame, "planes") and frame.planes:
                            p = frame.planes[0]
                            if hasattr(p, "fd"):
                                dmabuf_fd = p.fd
                            elif hasattr(p, "buffer_ptr") and isinstance(p.buffer_ptr, int):
                                dmabuf_fd = p.buffer_ptr
                    except Exception:
                        dmabuf_fd = None

                    if dmabuf_fd is not None:
                        self._has_first_frame = True
                        self.frame_ready.emit(("dmabuf", dmabuf_fd, frame.width, frame.height))
                    else:
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

                    if self._emit_interval > 0:
                        elapsed = time.time() - self._last_emit
                        if elapsed < self._emit_interval:
                            continue
                    self._last_emit = time.time()

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
                    logging.warning("HW decode failed (%s) — switching to CPU.", err)
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

class RenderBackend:
    def render_frame(self, frame_tuple):
        pass
    def is_valid(self):
        return False
    def name(self):
        return "unknown"

class RenderKMSDRM(RenderBackend):
    def __init__(self):
        self.valid = False
        self.fd = None
        self.gbm = None
        self.bo = None
        self.map = None
        self.stride = 0
        self.width = 0
        self.height = 0
        self.device_path = None

        for node in ("/dev/dri/renderD128", "/dev/dri/renderD129", "/dev/dri/card0", "/dev/dri/card1"):
            if os.path.exists(node) and os.access(node, os.W_OK):
                try:
                    self.fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
                    self.device_path = node
                    self.valid = True
                    break
                except Exception:
                    continue

        if not self.valid:
            logging.debug("KMSDRM: no accessible DRM device found.")
            return

        try:
            self.libgbm = ctypes.CDLL("libgbm.so.1")

            self.libgbm.gbm_create_device.argtypes = [ctypes.c_int]
            self.libgbm.gbm_create_device.restype = ctypes.c_void_p

            self.libgbm.gbm_bo_create.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                                  ctypes.c_uint32, ctypes.c_uint32]
            self.libgbm.gbm_bo_create.restype = ctypes.c_void_p

            self.libgbm.gbm_bo_get_stride.argtypes = [ctypes.c_void_p]
            self.libgbm.gbm_bo_get_stride.restype = ctypes.c_uint32

            self.libgbm.gbm_bo_destroy.argtypes = [ctypes.c_void_p]
            self.libgbm.gbm_device_destroy.argtypes = [ctypes.c_void_p]

            self.gbm = self.libgbm.gbm_create_device(self.fd)
            if not self.gbm:
                raise RuntimeError("gbm_create_device() failed")

            self.valid = True
            logging.info(f"KMSDRM initialized (safe render-node) via {self.device_path}")
        except Exception as e:
            logging.debug(f"KMSDRM init failed: {e}")
            self.valid = False

    def is_valid(self):
        return self.valid

    def name(self):
        return "KMSDRM"

    def _alloc_bo(self, w, h):
        if not self.valid or not self.gbm:
            return
        try:
            if self.bo:
                self.libgbm.gbm_bo_destroy(self.bo)
                self.bo = None

            DRM_FORMAT_ARGB8888 = 0x34325241
            GBM_BO_USE_RENDERING = 1 << 1

            self.bo = self.libgbm.gbm_bo_create(self.gbm, w, h, DRM_FORMAT_ARGB8888, GBM_BO_USE_RENDERING)
            if not self.bo:
                raise RuntimeError("gbm_bo_create() failed")

            self.stride = self.libgbm.gbm_bo_get_stride(self.bo)
            size = self.stride * h

            if self.map:
                self.map.close()
            self.map = mmap.mmap(self.fd, size, mmap.MAP_SHARED,
                                 mmap.PROT_READ | mmap.PROT_WRITE, offset=0)
            self.width, self.height = w, h
            logging.debug(f"KMSDRM GBM buffer {w}x{h} stride={self.stride}")
        except Exception as e:
            logging.debug(f"KMSDRM alloc failed: {e}")
            self.valid = False

    def _import_dmabuf(self, fd, w, h):
        try:
            size = w * h * 4
            with mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ) as buf:
                data = buf.read(size)
            logging.debug(f"KMSDRM: imported dmabuf FD={fd} ({w}x{h})")
            return np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)
        except Exception as e:
            logging.debug(f"KMSDRM: dmabuf import failed: {e}")
            return None

    def render_frame(self, frame_tuple):
        if not self.valid:
            return
        t0 = time.perf_counter()
        try:
            is_dmabuf = (
                isinstance(frame_tuple, tuple)
                and len(frame_tuple) == 4
                and isinstance(frame_tuple[0], str)
                and frame_tuple[0] == "dmabuf"
            )

            if is_dmabuf:
                _, fd, w, h = frame_tuple
                w, h = int(w), int(h)
                arr = self._import_dmabuf(fd, w, h)
                if not isinstance(arr, np.ndarray) or arr.size == 0:
                    return
            else:
                arr, w, h = frame_tuple
                w, h = int(w), int(h)
                if not isinstance(arr, np.ndarray) or arr.size == 0:
                    return

            cur_w = int(getattr(self, "width", 0) or 0)
            cur_h = int(getattr(self, "height", 0) or 0)
            if (w != cur_w) or (h != cur_h):
                self._alloc_bo(w, h)

            data = np.ascontiguousarray(arr, dtype=np.uint8)
            if self.map and hasattr(self.map, "write"):
                self.map.seek(0)
                self.map.write(data.tobytes())

            dt = (time.perf_counter() - t0) * 1000.0
            logging.debug(f"KMSDRM upload {w}x{h} ({data.nbytes/1024/1024:.2f} MB) in {dt:.2f} ms to {self.device_path}")
        except Exception as e:
            logging.debug(f"KMSDRM render error: {e}")

class RenderVulkan(RenderBackend):
    def __init__(self):
        try:
            import vulkan as vk
            self.valid = True
        except Exception:
            self.valid = False

    def is_valid(self):
        return self.valid

    def name(self):
        return "Vulkan"

    def render_frame(self, frame_tuple):
        try:
            if isinstance(frame_tuple, tuple) and len(frame_tuple) == 4 and isinstance(frame_tuple[0], str) and frame_tuple[0] == "dmabuf":
                return
            arr, w, h = frame_tuple
            if not isinstance(arr, np.ndarray) or arr.size == 0:
                return
            t0 = time.perf_counter()
            _ = np.mean(arr)
            dt = (time.perf_counter() - t0) * 1000.0
            logging.debug(f"Vulkan simulated render {int(w)}x{int(h)} in {dt:.2f} ms")
        except Exception as e:
            logging.debug(f"Vulkan render error: {e}")

class RenderOpenGL(RenderBackend):
    def __init__(self):
        self.valid = True

    def is_valid(self):
        return self.valid

    def name(self):
        return "OpenGL"

    def render_frame(self, frame_tuple):
        try:
            if isinstance(frame_tuple, tuple) and len(frame_tuple) == 4 and isinstance(frame_tuple[0], str) and frame_tuple[0] == "dmabuf":
                return
            arr, w, h = frame_tuple
            if not isinstance(arr, np.ndarray) or arr.size == 0:
                return
            t0 = time.perf_counter()
            _ = np.mean(arr)
            dt = (time.perf_counter() - t0) * 1000.0
            logging.debug(f"OpenGL simulated render {int(w)}x{int(h)} in {dt:.2f} ms")
        except Exception as e:
            logging.debug(f"OpenGL render error: {e}")

def pick_best_renderer():
    renderers = (RenderKMSDRM, RenderVulkan, RenderOpenGL)
    selected = None
    for renderer_cls in renderers:
        r = renderer_cls()
        logging.debug(f"Trying renderer: {r.name()} (valid={r.is_valid()})")
        if r.is_valid():
            selected = r
            logging.info(f"Renderer selected: {r.name()}")
            if hasattr(r, "device_path") and r.device_path:
                logging.info(f"Using device path: {r.device_path}")
            break

    if not selected:
        logging.warning("No GPU renderer found, using dummy software renderer.")
        selected = RenderBackend()
    return selected

class VideoWidgetGL(QOpenGLWidget):
    def __init__(self, control_callback, rwidth, rheight, offset_x, offset_y, host_ip,
                 parent=None, toggle_overlay=None):
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
        logging.info("Renderer priority order: KMSDRM → Vulkan → OpenGL")

        self.renderer = pick_best_renderer()
        logging.info(f"Using render backend: {self.renderer.name()}")
        if hasattr(self.renderer, "device_path") and self.renderer.device_path:
            logging.info(f"Bound to device: {self.renderer.device_path}")
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
        else:
            arr, fw, fh = payload

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
        elif kind == "dmabuf":
            _, _fd, fw, fh = frame_tuple
            if (fw, fh) != (self.texture_width, self.texture_height):
                self.resizeTexture(fw, fh)
        else:
            _arr, fw, fh = frame_tuple
            if (fw, fh) != (self.texture_width, self.texture_height):
                self.resizeTexture(fw, fh)
        self._last_frame_recv = time.time()

        now = time.time()
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

        rx = self.offset_x + int(nx * fw)
        ry = self.offset_y + int(ny * fh)
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


class MainWindow(QMainWindow):
    def __init__(self, decoder_opts, rwidth, rheight, host_ip, udp_port,
                 offset_x, offset_y, net_mode='lan', parent=None, ultra=False,
                 gamepad="disable", gamepad_dev=None, pin=None, audio=True,
                 stats_visible=False):
        super().__init__(parent)

        self.window_id = SESSION.register()
        self.monitor_index = udp_port - DEFAULT_UDP_PORT
        self._pin = pin
        self._audio_enabled = audio
        self.setWindowTitle("LinuxPlay")
        self.texture_width, self.texture_height = rwidth, rheight
        self.offset_x, self.offset_y = offset_x, offset_y
        self.host_ip, self.ultra = host_ip, ultra
        self.udp_port = udp_port
        self._running, self._restarts = True, 0
        self.gamepad_mode = gamepad
        self.gamepad_dev = gamepad_dev

        self.control_addr = (host_ip, CONTROL_PORT)
        self.control_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.control_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.control_sock.setblocking(False)

        self.send_control(f"NET {net_mode}")

        self.video_widget = VideoWidgetGL(self.send_control, rwidth, rheight,
                                          offset_x, offset_y, host_ip,
                                          toggle_overlay=self.toggle_stats)
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
            self._audio_thread = audio_listener(self.host_ip, enabled=self._audio_enabled)
        except Exception as e:
            logging.error(f"Audio listener failed: {e}")
            self._audio_thread = None

        try:
            self._clip_thread = clipboard_listener(QApplication.clipboard())
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
        return (
            f"udp://@0.0.0.0:{self.udp_port}"
            f"?pkt_size={pkt}"
            f"&reuse=1&buffer_size=4194304&fifo_size=131072"
            f"&overrun_nonfatal=1&max_delay=0&rw_timeout=5000000"
        )

    def _start_decoder_thread(self):
        self.video_url = self._video_url_for_path()
        self.decoder_thread = DecoderThread(self.video_url, self.decoder_opts, ultra=self.ultra)
        self.decoder_thread.frame_ready.connect(self.video_widget.updateFrame, Qt.DirectConnection)
        self.decoder_thread.finished.connect(self._on_decoder_exit)
        self.decoder_thread.start()
        logging.info("Decoder thread started")

    def _on_decoder_exit(self):
        if not self._running:
            return
        self._restarts += 1
        delay = min(1.0 + (self._restarts * 0.3), 5.0)
        logging.warning(f"Decoder thread exited — attempting restart in {delay:.1f}s")
        QTimer.singleShot(int(delay * 1000), self._restart_decoder_safe)

    def _restart_decoder_safe(self):
        if self._running:
            try:
                self._start_decoder_thread()
            except Exception as e:
                logging.error(f"Decoder restart failed: {e}")

    def _poll_connection_state(self):
        now = time.time()
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
        try:
            backend = self.video_widget.renderer.name()
        except Exception:
            backend = "?"
        hw = getattr(getattr(self, "decoder_thread", None), "_hw_name", None) or "CPU"
        status = ("connected" if CLIENT_STATE["connected"]
                  else ("reconnecting…" if CLIENT_STATE["reconnecting"] else "idle"))
        enc_txt = f"{enc_kbps / 1000:.1f} Mb/s" if enc_kbps else "—"
        kf_txt = f"{data.get('keyframes', 0):.0f}/s" if "keyframes" in data else "—"
        data["headline"] = (f"LinuxPlay · {self.host_ip}:{self.udp_port} · "
                            f"{self.texture_width}x{self.texture_height}")
        data["info"] = [
            f"link {CLIENT_STATE.get('net_mode', 'lan')} · {status} · "
            f"{uptime // 60:02d}:{uptime % 60:02d} up · {self._restarts} restarts",
            f"decode {hw} · render {backend}",
            f"host encode {enc_txt} @ {host_stats.get('fps', 0):.0f} fps · "
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
        except Exception:
            pass

    def _update_stats(self):
        try:
            cpu = self._proc.cpu_percent(interval=None)
            mem = self._proc.memory_info().rss / (1024 * 1024)
            gpu = self._read_gpu_usage()
            fps = getattr(self.video_widget, "_fps", 0.0)
            renderer_name = getattr(self.video_widget.renderer, "name", lambda: "Unknown")()
            device_info = getattr(self.video_widget.renderer, "device_path", None)
            backend = f"{renderer_name} ({os.path.basename(device_info)})" if device_info else renderer_name

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

        if getattr(self, "_gp_thread", None):
            try:
                self._gp_thread.stop()
            except Exception:
                pass

        if remaining == 0:
            # One ffplay serves every monitor window, so only the last window
            # may stop it — closing one of several must not kill audio for all.
            global audio_proc
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

def main():
    import os, sys, argparse, psutil, time, logging
    from PyQt5.QtWidgets import QApplication, QMessageBox
    from PyQt5.QtGui import QSurfaceFormat

    p = argparse.ArgumentParser(description="LinuxPlay Client (Linux/Windows/macOS)")
    p.add_argument("--decoder", choices=["none", "h.264", "h.265"], default="none")
    p.add_argument("--host_ip", required=True)
    p.add_argument("--pin", default=None, help="6-digit host PIN (optional; will prompt if required)")
    p.add_argument("--audio", choices=["enable", "disable"], default="disable")
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
        os.environ.setdefault("QT_XCB_GL_INTEGRATION", "xcb_egl")

    fmt = QSurfaceFormat()
    fmt.setSwapInterval(0)
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
        file_handler = logging.FileHandler("linuxplay_client.log", mode="w", encoding="utf-8")
        file_handler.setLevel(LOG_LEVEL)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
        root_logger.addHandler(file_handler)
    except Exception:
        pass

    logging.info("────────────────────────────────────────────")
    logging.info("LinuxPlay Client starting up")
    logging.info(f"Python: {sys.version.split()[0]}, Platform: {sys.platform}")
    logging.info("────────────────────────────────────────────")

    app = QApplication(sys.argv)

    ok, host_info = tcp_handshake_client(args.host_ip, args.pin)
    if not ok or not host_info:
        QMessageBox.critical(None, "Handshake Failed", "Could not negotiate with host.")
        sys.exit(1)
    host_encoder, monitor_info_str = host_info
    CLIENT_STATE["connected"] = True
    CLIENT_STATE["last_heartbeat"] = time.time()

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
            "fflags": "nobuffer",
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
            win = MainWindow(decoder_opts, w, h, args.host_ip, DEFAULT_UDP_PORT + i,
                             ox, oy, net_mode, ultra=ultra_active,
                             gamepad=args.gamepad, gamepad_dev=args.gamepad_dev, pin=args.pin,
                             audio=(args.audio == "enable"), stats_visible=args.stats)
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
        win = MainWindow(decoder_opts, w, h, args.host_ip, DEFAULT_UDP_PORT + idx,
                         ox, oy, net_mode, ultra=ultra_active,
                         gamepad=args.gamepad, gamepad_dev=args.gamepad_dev, pin=args.pin,
                         audio=(args.audio == "enable"), stats_visible=args.stats)
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
