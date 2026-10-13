#!/usr/bin/env python3
"""Headless measurement client: the real client code, no window.

Imports client.py from --client-dir (so a baseline checkout and the working
tree can be measured with the same harness), builds the same MainWindow the
client builds, but never shows it: decode, pacing reports, heartbeats, audio
and the WINDOW_CLOSE/GOODBYE teardown all run exactly as in a real session,
minus the drawing. Closes itself after --minutes.

    .venv/bin/python tools/probe_client.py --host_ip 100.76.206.96 \\
        --minutes 30 --log /tmp/probe.log [--client-dir DIR] [--audio]

The log has the client's own format, plus the macOS UDP drop counters at
the start and end (dropped for a full socket buffer = the client could not
drain its socket; that is client-side loss, not the network's).
"""
import argparse
import importlib
import logging
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def udp_counters():
    """macOS/Linux kernel UDP counters that separate local drops from loss."""
    out = {}
    try:
        if sys.platform == "darwin":
            txt = subprocess.check_output(["netstat", "-s", "-p", "udp"], text=True)
            for line in txt.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[0].isdigit():
                    if "full socket" in parts[1]:
                        out["rcvbuf_drops"] = int(parts[0])
                    elif "no socket" in parts[1]:
                        out["no_socket"] = int(parts[0])
                    elif parts[1].startswith("datagram received"):
                        out["received"] = int(parts[0])
        else:
            with open("/proc/net/snmp") as f:
                rows = [l.split() for l in f if l.startswith("Udp:")]
            hdr, val = rows[0], rows[1]
            d = dict(zip(hdr[1:], map(int, val[1:])))
            out = {"rcvbuf_drops": d.get("RcvbufErrors"), "no_socket": d.get("NoPorts"),
                   "received": d.get("InDatagrams")}
    except Exception:
        pass
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host_ip", required=True)
    ap.add_argument("--client-dir", default=os.path.dirname(HERE))
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--log", default="probe_client.log")
    ap.add_argument("--audio", action="store_true", help="also run the audio player")
    ap.add_argument("--hwaccel", default="auto")
    ap.add_argument("--net", default="auto")
    a = ap.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, os.path.abspath(a.client_dir))
    client = importlib.import_module("client")

    from PyQt5.QtCore import QTimer
    from PyQt5.QtWidgets import QApplication

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.FileHandler(a.log, mode="w", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    root.addHandler(fh)
    client._enable_libav_logging()

    logging.info("Probe: client %s (%s), %.1f min, audio %s", os.path.abspath(client.__file__),
                 subprocess.run(["git", "-C", a.client_dir, "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True).stdout.strip() or "?",
                 a.minutes, "on" if a.audio else "off")
    c0 = udp_counters()
    logging.info("Probe: UDP counters at start: %s", c0)

    app = QApplication([sys.argv[0]])
    ok, host_info = client.tcp_handshake_client(a.host_ip, None, interactive=False)
    if not ok or not host_info:
        logging.error("Probe: handshake failed.")
        return 2
    client.CLIENT_STATE["connected"] = True
    client.CLIENT_STATE["last_heartbeat"] = time.time()
    host_encoder, monitor_info_str = host_info[0], host_info[1]
    stream_size = client._size_from_text(host_info[2]) if len(host_info) > 2 else None
    net_mode = a.net if a.net != "auto" else client.detect_network_mode(a.host_ip)
    part = [p for p in monitor_info_str.split(";") if p][0]
    res, ox, oy = (part.split("+") + ["0", "0"])[:3]
    w, h = map(int, res.split("x"))
    hw = client.choose_auto_hwaccel() if a.hwaccel == "auto" else a.hwaccel
    decoder_opts = {} if hw == "cpu" else {"hwaccel": hw}
    logging.info("Probe: host %s, desktop %s, net %s, hwaccel %s",
                 host_encoder, monitor_info_str, net_mode, hw)

    win = client.MainWindow(decoder_opts, *(stream_size or (w, h)), a.host_ip,
                            client.DEFAULT_UDP_PORT, int(ox), int(oy), net_mode,
                            ultra=False, gamepad="disable", audio=a.audio,
                            desktop_size=(w, h))

    def finish():
        if getattr(finish, "done", False):
            return
        finish.done = True
        win.close()                          # WINDOW_CLOSE + GOODBYE, as a user would
        c1 = udp_counters()
        delta = {k: c1.get(k, 0) - c0.get(k, 0) for k in c1 if c0.get(k) is not None}
        logging.info("Probe: UDP counter deltas over the run: %s", delta)
        QTimer.singleShot(3000, app.quit)

    QTimer.singleShot(int(a.minutes * 60_000), finish)
    signal.signal(signal.SIGINT, lambda *_: QTimer.singleShot(0, finish))
    signal.signal(signal.SIGTERM, lambda *_: QTimer.singleShot(0, finish))
    tick = QTimer()                          # lets Python see signals during exec_()
    tick.start(250)
    tick.timeout.connect(lambda: None)
    app.exec_()
    client.audio_stop.set()
    if client.audio_proc:
        try:
            client.audio_proc.terminate()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
