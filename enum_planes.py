#!/usr/bin/env python3
"""Enumerate DRM planes + active framebuffers (read-only) to see real formats."""
import ctypes
import fcntl
import struct
import sys

CARD = sys.argv[1] if len(sys.argv) > 1 else "/dev/dri/card1"
fd = open(CARD, "rb")

GETPLANERESOURCES = 0xC01064B5  # 16B
GETPLANE          = 0xC02864B6  # 40B
GETFB2            = 0xC06464B9  # 100B
GETRESOURCES      = 0xC04064A0  # 64B
GETCONNECTOR      = 0xC05464A7  # 84B
GETENCODER        = 0xC02464A6  # 36B
SET_CLIENT_CAP    = 0xC010640C  # 16B

def ioctl(nr, buf):
    return fcntl.ioctl(fd, nr, buf, True)

def fourcc(v):
    return bytes([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF]).decode(errors="replace")

# expose universal (primary) planes to this client
DRM_CLIENT_CAP_UNIVERSAL_PLANES = 2
cap = ctypes.create_string_buffer(16)
struct.pack_into("<QQ", cap, 0, DRM_CLIENT_CAP_UNIVERSAL_PLANES, 1)
try:
    ioctl(SET_CLIENT_CAP, cap)
except OSError:
    pass   # old kernel; plane list may be limited

IDCAP = 64
res = ctypes.create_string_buffer(64)
fb_buf = ctypes.create_string_buffer(4 * IDCAP)
crtc_buf = ctypes.create_string_buffer(4 * IDCAP)
conn_buf = ctypes.create_string_buffer(4 * IDCAP)
enc_buf = ctypes.create_string_buffer(4 * IDCAP)
struct.pack_into("<Q", res, 0, ctypes.addressof(fb_buf))
struct.pack_into("<Q", res, 8, ctypes.addressof(crtc_buf))
struct.pack_into("<Q", res, 16, ctypes.addressof(conn_buf))
struct.pack_into("<Q", res, 24, ctypes.addressof(enc_buf))
struct.pack_into("<IIII", res, 32, IDCAP, IDCAP, IDCAP, IDCAP)
ioctl(GETRESOURCES, res)
count_crtc = struct.unpack_from("<I", res, 36)[0]
count_conn = struct.unpack_from("<I", res, 40)[0]
crtc_ids = struct.unpack_from(f"<{count_crtc}I", crtc_buf, 0)
conn_ids = struct.unpack_from(f"<{count_conn}I", conn_buf, 0)
print(f"crtcs: {list(crtc_ids)}")

TYPE_NAMES = {0: "Unknown", 1: "VGA", 2: "DVII", 3: "DVID", 4: "DVI-A",
              5: "Composite", 6: "SVIDEO", 7: "LVDS", 8: "Component", 9: "DIN",
              10: "DP", 11: "HDMI-A", 12: "HDMI-B", 13: "TV", 14: "eDP",
              15: "Virtual", 16: "DSI", 17: "DPI", 18: "Writeback"}

conn_map = {}   # crtc_id -> connector name
for cid in conn_ids:
    b = ctypes.create_string_buffer(84)
    struct.pack_into("<I", b, 48, cid)
    ioctl(GETCONNECTOR, b)
    enc_id = struct.unpack_from("<I", b, 44)[0]
    ctype = struct.unpack_from("<I", b, 52)[0]
    ctype_id = struct.unpack_from("<I", b, 56)[0]
    name = f"{TYPE_NAMES.get(ctype, '?')}-{ctype_id}"
    crtc = None
    if enc_id:
        e = ctypes.create_string_buffer(36)
        struct.pack_into("<I", e, 0, enc_id)
        ioctl(GETENCODER, e)
        crtc = struct.unpack_from("<I", e, 8)[0] or None
    conn_map[crtc] = name
    print(f"connector {cid} {name:9s} crtc={crtc}")

res2 = ctypes.create_string_buffer(16)
ids_arr = ctypes.create_string_buffer(4 * IDCAP)
struct.pack_into("<Q", res2, 0, ctypes.addressof(ids_arr))
struct.pack_into("<I", res2, 8, IDCAP)
ioctl(GETPLANERESOURCES, res2)
count = struct.unpack_from("<I", res2, 8)[0]
plane_ids = struct.unpack_from(f"<{count}I", ids_arr, 0)
print(f"planes total: {count}")

for pid in plane_ids:
    b = ctypes.create_string_buffer(40)
    fmts_arr = ctypes.create_string_buffer(4 * 64)
    struct.pack_into("<I", b, 8, pid)
    struct.pack_into("<I", b, 28, 64)
    struct.pack_into("<Q", b, 32, ctypes.addressof(fmts_arr))
    if ioctl(GETPLANE, b) != 0:
        continue
    crtc_id = struct.unpack_from("<I", b, 12)[0]
    fb_id = struct.unpack_from("<I", b, 16)[0]
    if fb_id == 0:
        continue
    f2 = ctypes.create_string_buffer(100)
    struct.pack_into("<I", f2, 0, fb_id)
    fmt = mod = None
    if ioctl(GETFB2, f2) == 0:
        fmt = fourcc(struct.unpack_from("<I", f2, 16)[0])
        mod = struct.unpack_from("<Q", f2, 24)[0]
    conn = conn_map.get(crtc_id, "?")
    print(f"plane {pid}: crtc={crtc_id} ({conn}) fb={fb_id} format={fmt!r} modifier={'0x%x' % mod if mod is not None else None}")
