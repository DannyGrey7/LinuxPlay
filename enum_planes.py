#!/usr/bin/env python3
"""Enumerate DRM planes + active framebuffers (read-only) to see real formats."""
import ctypes
import fcntl
import struct
import sys

CARD = sys.argv[1] if len(sys.argv) > 1 else "/dev/dri/card1"
fd = open(CARD, "rb")

# ioctl numbers and struct layouts from include/uapi/drm/drm.h + drm_mode.h.
# Each DRM_IOWR() encodes sizeof(struct) in its size field, so these are the
# sizes the kernel expects the buffer to have.
GETPLANERESOURCES = 0xC01064B5  # 16B  drm_mode_get_plane_res
GETPLANE          = 0xC02064B6  # 32B  drm_mode_get_plane
GETFB2            = 0xC06864CE  # 104B drm_mode_fb_cmd2
GETRESOURCES      = 0xC04064A0  # 64B  drm_mode_card_res
GETCONNECTOR      = 0xC05064A7  # 80B  drm_mode_get_connector
GETENCODER        = 0xC01464A6  # 20B  drm_mode_get_encoder
SET_CLIENT_CAP    = 0x4010640D  # 16B  drm_set_client_cap

DRM_MODE_FB_MODIFIERS = 2

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
except OSError as e:
    print(f"SET_CLIENT_CAP failed ({e}); plane list may be limited to legacy planes")

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
    b = ctypes.create_string_buffer(80)
    struct.pack_into("<I", b, 48, cid)           # connector_id
    ioctl(GETCONNECTOR, b)
    enc_id = struct.unpack_from("<I", b, 44)[0]  # encoder_id
    ctype = struct.unpack_from("<I", b, 52)[0]   # connector_type
    ctype_id = struct.unpack_from("<I", b, 56)[0]
    name = f"{TYPE_NAMES.get(ctype, '?')}-{ctype_id}"
    crtc = None
    if enc_id:
        e = ctypes.create_string_buffer(20)
        struct.pack_into("<I", e, 0, enc_id)     # encoder_id
        ioctl(GETENCODER, e)
        crtc = struct.unpack_from("<I", e, 8)[0] or None   # crtc_id
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
    b = ctypes.create_string_buffer(32)
    fmts_arr = ctypes.create_string_buffer(4 * 64)
    struct.pack_into("<I", b, 0, pid)            # plane_id
    struct.pack_into("<I", b, 20, 64)            # count_format_types
    struct.pack_into("<Q", b, 24, ctypes.addressof(fmts_arr))  # format_type_ptr
    try:
        ioctl(GETPLANE, b)
    except OSError as e:
        print(f"plane {pid}: GETPLANE failed ({e})")
        continue
    fmt_count = struct.unpack_from("<I", b, 20)[0]
    plane_fmts = [fourcc(f) for f in
                  struct.unpack_from(f"<{min(fmt_count, 64)}I", fmts_arr, 0)]
    crtc_id = struct.unpack_from("<I", b, 4)[0]  # crtc_id
    fb_id = struct.unpack_from("<I", b, 8)[0]    # fb_id
    if fb_id == 0:
        print(f"plane {pid}: crtc={crtc_id or None} ({conn_map.get(crtc_id, '?')}) "
              f"fb=0 formats={plane_fmts}")
        continue
    f2 = ctypes.create_string_buffer(104)
    struct.pack_into("<I", f2, 0, fb_id)         # fb_id
    fmt_i = mod = None
    try:
        ioctl(GETFB2, f2)
        fmt_i = struct.unpack_from("<I", f2, 12)[0]   # pixel_format
        flags = struct.unpack_from("<I", f2, 16)[0]   # flags
        if flags & DRM_MODE_FB_MODIFIERS:
            mod = struct.unpack_from("<Q", f2, 72)[0]  # modifier[0]
    except OSError as e:
        print(f"plane {pid}: GETFB2 failed ({e})")
    fmt = fourcc(fmt_i) if fmt_i is not None else None
    print(f"plane {pid}: crtc={crtc_id} ({conn_map.get(crtc_id, '?')}) fb={fb_id} "
          f"format={fmt!r} modifier={'0x%x' % mod if mod is not None else None}")
