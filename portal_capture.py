#!/usr/bin/env python3
"""xdg-desktop-portal ScreenCast negotiation + PipeWire feeder for LinuxPlay.

Negotiates a screen-cast session over D-Bus (pure-python jeepney client),
then bridges each captured monitor into the existing FFmpeg encode chain:

    gst-launch-1.0 -q pipewiresrc path=<node> ! videoconvert !
        video/x-raw,format=BGRx,width=W,height=H ! fdsink sync=false
        -> (stdout) ->  ffmpeg -f rawvideo -pix_fmt bgr0 -i -

Works on KDE Plasma, GNOME and wlroots compositors. The compositor shows a
screen-share permission dialog on first use; approval can be persisted by
the portal (restore token stored in ~/.config/linuxplay/).
"""
import json
import logging
import os
import secrets
import time

from jeepney import DBusAddress, MatchRule, new_method_call
from jeepney.low_level import HeaderFields, MessageType

PORTAL_DEST = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"

RESTORE_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "linuxplay", "portal_restore.json"
)

MONITOR = 1          # source type bitmask
CURSOR_EMBEDDED = 2  # cursor is drawn into the buffers
PERSISTENT = 2       # persist_mode: portal may store a restore token


def gst_pipewiresrc_available() -> bool:
    import shutil, subprocess
    if not shutil.which("gst-launch-1.0"):
        return False
    try:
        out = subprocess.run(
            ["gst-inspect-1.0", "pipewiresrc"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
        return out.returncode == 0
    except Exception:
        return False


def _load_restore_token():
    try:
        with open(RESTORE_PATH, "r") as f:
            return json.load(f).get("restore_token")
    except Exception:
        return None


def _save_restore_token(token):
    try:
        os.makedirs(os.path.dirname(RESTORE_PATH), exist_ok=True)
        with open(RESTORE_PATH, "w") as f:
            json.dump({"restore_token": token}, f)
        os.chmod(RESTORE_PATH, 0o600)
    except Exception as e:
        logging.debug("Could not persist portal restore token: %s", e)


class PortalCapture:
    """One ScreenCast session, kept alive across client reconnects."""

    def __init__(self):
        self.conn = None
        self.session = None
        self.streams = []   # [{"node", "w", "h", "x", "y"}]

    # ── low-level helpers ────────────────────────────────────────────
    def _portal_obj(self):
        return DBusAddress(PORTAL_PATH, bus_name=PORTAL_DEST, interface=SCREENCAST_IFACE)

    def _call(self, method, signature, body, timeout):
        msg = new_method_call(self._portal_obj(), method, signature, body)
        return self.conn.send_and_get_reply(msg, timeout=timeout)

    def _wait_response(self, request_path, timeout):
        """Wait for the portal Response signal on a specific request path."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for portal response on {request_path}")
            msg = self.conn.receive(timeout=remaining)
            if (msg.header.message_type == MessageType.signal
                    and msg.header.fields.get(HeaderFields.interface) == REQUEST_IFACE
                    and msg.header.fields.get(HeaderFields.member) == "Response"
                    and msg.header.fields.get(HeaderFields.path) == request_path):
                def _unwrap(v):
                    return v[1] if isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], str) else v
                code, results = msg.body[0], msg.body[1]
                return code, {k: _unwrap(v) for k, v in results.items()}

    def _add_match(self):
        rule = MatchRule(type="signal", interface=REQUEST_IFACE)
        dbus_obj = DBusAddress("/org/freedesktop/DBus", bus_name="org.freedesktop.DBus",
                               interface="org.freedesktop.DBus")
        msg = new_method_call(dbus_obj, "AddMatch", "s", (rule.serialise(),))
        self.conn.send_and_get_reply(msg, timeout=10)

    # ── negotiation ──────────────────────────────────────────────────
    def ensure(self, multiple=True, timeout_dialog=120):
        if self.session is not None:
            return True
        from jeepney.io.blocking import open_dbus_connection
        self.conn = open_dbus_connection(bus="SESSION")
        self._add_match()

        sender = (self.conn.unique_name or ":0")[1:].replace(".", "_")
        handle_token = "lp" + secrets.token_hex(4)
        session_token = "linuxplay" + secrets.token_hex(4)
        request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{handle_token}"

        reply = self._call("CreateSession", "a{sv}", ({
            "handle_token": ("s", handle_token),
            "session_handle_token": ("s", session_token),
        },), timeout=10)
        logging.debug("CreateSession -> %s", reply)

        # NOTE: jeepney builds Variant from (signature, value) tuples automatically
        code, results = self._wait_response(request_path, 15)
        if code != 0:
            raise RuntimeError(f"CreateSession failed (code {code})")
        self.session = results.get("session_handle")
        if not self.session:
            raise RuntimeError("Portal did not return a session handle")
        logging.info("Portal session: %s", self.session)

        # 2. SelectSources (monitors, cursor embedded, persist approval)
        restore_token = _load_restore_token()
        options = {
            "handle_token": ("s", handle_token),
            "types": ("u", MONITOR),
            "multiple": ("b", multiple),
            "cursor_mode": ("u", CURSOR_EMBEDDED),
            "persist_mode": ("u", PERSISTENT),
        }
        if restore_token:
            options["restore_token"] = ("s", restore_token)
        request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{handle_token}"
        self._call("SelectSources", "oa{sv}", (self.session, options), timeout=15)
        code, results = self._wait_response(request_path, timeout_dialog)
        if code != 0:
            raise RuntimeError(f"Screen selection declined (code {code})")
        new_token = results.get("restore_token")
        if new_token:
            _save_restore_token(new_token)

        # 3. Start
        request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{handle_token}"
        self._call("Start", "osa{sv}", (self.session, "", {"handle_token": ("s", handle_token)}),
                   timeout=15)
        code, results = self._wait_response(request_path, timeout_dialog)
        if code != 0:
            raise RuntimeError(f"ScreenCast start declined (code {code})")

        # 4. Parse streams
        streams = results.get("streams") or []
        def _unwrap(v):
            return v[1] if isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], str) else v
        for entry in streams:
            node, props = entry[0], entry[1]
            size = _unwrap(props.get("size")) or (0, 0)
            pos = _unwrap(props.get("position")) or (0, 0)
            self.streams.append({
                "node": int(node),
                "w": int(size[0]), "h": int(size[1]),
                "x": int(pos[0]), "y": int(pos[1]),
            })
        logging.info("Portal streams: %s", self.streams)
        if not self.streams:
            raise RuntimeError("Portal returned no streams")
        return True

    def match_stream(self, idx, monitor):
        """Match a detected monitor (w,h,ox,oy) to a portal stream."""
        w, h, ox, oy = monitor
        for s in self.streams:
            if (s["x"], s["y"]) == (ox, oy):
                return s
        if 0 <= idx < len(self.streams):
            return self.streams[idx]
        return self.streams[0] if self.streams else None

    def close(self):
        if self.conn and self.session:
            try:
                self._call("CloseSession", "o", (self.session,), timeout=5)
            except Exception:
                pass
        self.session = None
        self.streams = []
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


def build_feeder_cmd(stream):
    """gst-launch pipeline piping raw BGRx frames of this stream to stdout."""
    return [
        "gst-launch-1.0", "-q",
        "pipewiresrc", f"path={stream['node']}",
        "!",
        "videoconvert", "!",
        f"video/x-raw,format=BGRx,width={stream['w']},height={stream['h']}",
        "!",
        "fdsink", "sync=false",
    ]
