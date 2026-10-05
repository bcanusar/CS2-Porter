"""VTF (Valve Texture Format) reader.

Turns the first frame / face of the largest mip into a Pillow image.
Unsupported formats fall back to a backup converter.
"""

import io
import os
import struct
import subprocess
import tempfile

import numpy as np
from PIL import Image

# --- format constants -----------------------------------------------------
FMT = {
    0: "RGBA8888", 1: "ABGR8888", 2: "RGB888", 3: "BGR888", 4: "RGB565",
    5: "I8", 6: "IA88", 7: "P8", 8: "A8", 9: "RGB888_BLUESCREEN",
    10: "BGR888_BLUESCREEN", 11: "ARGB8888", 12: "BGRA8888", 13: "DXT1",
    14: "DXT3", 15: "DXT5", 16: "BGRX8888", 17: "BGR565", 18: "BGRX5551",
    19: "BGRA4444", 20: "DXT1_ONEBITALPHA", 21: "BGRA5551", 22: "UV88",
    23: "UVWQ8888", 24: "RGBA16161616F", 25: "RGBA16161616", 26: "UVLX8888",
}

_BPP = {  # bytes per pixel (uncompressed formats)
    0: 4, 1: 4, 2: 3, 3: 3, 4: 2, 5: 1, 6: 2, 7: 1, 8: 1, 9: 3, 10: 3,
    11: 4, 12: 4, 16: 4, 17: 2, 18: 2, 19: 2, 21: 2, 22: 2, 23: 4,
    24: 8, 25: 8, 26: 4,
}

TEXTUREFLAGS_ENVMAP = 0x4000

_CREATE_NO_WINDOW = 0x08000000


class VTFError(Exception):
    pass


def _image_size(fmt, w, h):
    if fmt in (13, 20):
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * 8
    if fmt in (14, 15):
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * 16
    bpp = _BPP.get(fmt)
    if bpp is None:
        raise VTFError(f"Unsupported format: {FMT.get(fmt, fmt)}")
    return w * h * bpp


class VTFInfo:
    def __init__(self, width, height, fmt, flags, frames, mips, depth, version):
        self.width = width
        self.height = height
        self.format = fmt
        self.flags = flags
        self.frames = frames
        self.mips = mips
        self.depth = depth
        self.version = version

    @property
    def format_name(self):
        return FMT.get(self.format, str(self.format))


def read_header(data):
    if data[:4] != b"VTF\x00":
        raise VTFError("No VTF signature")
    major, minor, header_size = struct.unpack_from("<III", data, 4)
    width, height, flags, frames, first_frame = struct.unpack_from("<HHIHH", data, 16)
    hi_fmt = struct.unpack_from("<i", data, 52)[0]
    mips = data[56]
    lo_fmt = struct.unpack_from("<i", data, 57)[0]
    lo_w, lo_h = data[61], data[62]
    depth = 1
    if (major, minor) >= (7, 2):
        depth = struct.unpack_from("<H", data, 63)[0] or 1
    info = VTFInfo(width, height, hi_fmt, flags, frames or 1, mips or 1, depth, (major, minor))
    info._header_size = header_size
    info._first_frame = first_frame
    info._lo = (lo_fmt, lo_w, lo_h)
    return info


def _hi_res_offset(data, info):
    major, minor = info.version
    if (major, minor) >= (7, 3):
        num_res = struct.unpack_from("<I", data, 68)[0]
        for i in range(num_res):
            tag = data[80 + i * 8: 80 + i * 8 + 3]
            _flag = data[80 + i * 8 + 3]
            off = struct.unpack_from("<I", data, 80 + i * 8 + 4)[0]
            if tag == b"\x30\x00\x00":
                return off
        raise VTFError("High resolution resource block not found")
    lo_fmt, lo_w, lo_h = info._lo
    lo_size = 0
    if lo_fmt >= 0 and lo_w and lo_h:
        lo_size = _image_size(lo_fmt, lo_w, lo_h)
    return info._header_size + lo_size


def _faces(info):
    if not info.flags & TEXTUREFLAGS_ENVMAP:
        return 1
    if info.version < (7, 5) and info._first_frame != 0xFFFF:
        return 7
    return 6


def _decode(fmt, w, h, raw):
    """Turns raw pixel data into an RGBA/RGB Pillow image."""
    if fmt in (13, 20):
        pw, ph = max(4, (w + 3) // 4 * 4), max(4, (h + 3) // 4 * 4)
        img = Image.frombytes("RGBA", (pw, ph), raw, "bcn", 1)
        return img.crop((0, 0, w, h)) if (pw, ph) != (w, h) else img
    if fmt in (14, 15):
        pw, ph = max(4, (w + 3) // 4 * 4), max(4, (h + 3) // 4 * 4)
        img = Image.frombytes("RGBA", (pw, ph), raw, "bcn", 2 if fmt == 14 else 3)
        return img.crop((0, 0, w, h)) if (pw, ph) != (w, h) else img

    a = np.frombuffer(raw, dtype=np.uint8)
    if fmt == 0:      # RGBA8888
        return Image.fromarray(a.reshape(h, w, 4), "RGBA")
    if fmt == 1:      # ABGR8888
        p = a.reshape(h, w, 4)
        return Image.fromarray(p[:, :, [3, 2, 1, 0]].copy(), "RGBA")
    if fmt in (2, 9):  # RGB888
        return Image.fromarray(a.reshape(h, w, 3), "RGB")
    if fmt in (3, 10):  # BGR888
        p = a.reshape(h, w, 3)
        return Image.fromarray(p[:, :, ::-1].copy(), "RGB")
    if fmt == 11:     # ARGB8888 (VTFLib actually treats it like BGRA: R and B are swapped)
        p = a.reshape(h, w, 4)
        return Image.fromarray(p[:, :, [1, 2, 3, 0]].copy(), "RGBA")
    if fmt == 12:     # BGRA8888
        p = a.reshape(h, w, 4)
        return Image.fromarray(p[:, :, [2, 1, 0, 3]].copy(), "RGBA")
    if fmt == 16:     # BGRX8888
        p = a.reshape(h, w, 4)
        return Image.fromarray(p[:, :, [2, 1, 0]].copy(), "RGB")
    if fmt == 5:      # I8
        return Image.fromarray(a.reshape(h, w), "L").convert("RGB")
    if fmt == 6:      # IA88
        p = a.reshape(h, w, 2)
        rgba = np.dstack([p[:, :, 0], p[:, :, 0], p[:, :, 0], p[:, :, 1]])
        return Image.fromarray(rgba, "RGBA")
    if fmt == 8:      # A8
        p = a.reshape(h, w)
        rgba = np.dstack([np.zeros_like(p)] * 3 + [p])
        return Image.fromarray(rgba, "RGBA")
    if fmt == 22:     # UV88
        p = a.reshape(h, w, 2)
        rgb = np.dstack([p[:, :, 0], p[:, :, 1], np.zeros_like(p[:, :, 0])])
        return Image.fromarray(rgb, "RGB")
    if fmt in (23, 26):  # UVWQ8888 / UVLX8888
        return Image.fromarray(a.reshape(h, w, 4), "RGBA")
    if fmt in (4, 17):   # RGB565 / BGR565
        v = np.frombuffer(raw, dtype="<u2").reshape(h, w).astype(np.uint32)
        c0 = ((v >> 11) & 0x1F) * 255 // 31
        c1 = ((v >> 5) & 0x3F) * 255 // 63
        c2 = (v & 0x1F) * 255 // 31
        if fmt == 4:
            rgb = np.dstack([c2, c1, c0])   # RGB565: R in the low bits (VTFLib)
        else:
            rgb = np.dstack([c0, c1, c2])
        return Image.fromarray(rgb.astype(np.uint8), "RGB")
    if fmt in (18, 21):  # BGRX5551 / BGRA5551
        v = np.frombuffer(raw, dtype="<u2").reshape(h, w).astype(np.uint32)
        b = (v & 0x1F) * 255 // 31
        g = ((v >> 5) & 0x1F) * 255 // 31
        r = ((v >> 10) & 0x1F) * 255 // 31
        if fmt == 21:
            al = ((v >> 15) & 1) * 255
            return Image.fromarray(np.dstack([r, g, b, al]).astype(np.uint8), "RGBA")
        return Image.fromarray(np.dstack([r, g, b]).astype(np.uint8), "RGB")
    if fmt == 19:     # BGRA4444
        v = np.frombuffer(raw, dtype="<u2").reshape(h, w).astype(np.uint32)
        b = (v & 0xF) * 17
        g = ((v >> 4) & 0xF) * 17
        r = ((v >> 8) & 0xF) * 17
        al = ((v >> 12) & 0xF) * 17
        return Image.fromarray(np.dstack([r, g, b, al]).astype(np.uint8), "RGBA")
    if fmt == 24:     # RGBA16161616F (HDR) -> simple tone mapping
        p = np.frombuffer(raw, dtype="<f2").reshape(h, w, 4).astype(np.float32)
        rgb = np.clip(p[:, :, :3], 0, None)
        rgb = rgb / (1.0 + rgb)
        rgb = np.power(rgb, 1 / 2.2)
        out = np.clip(rgb * 255.0 + 0.5, 0, 255).astype(np.uint8)
        return Image.fromarray(out, "RGB")
    if fmt == 25:     # RGBA16161616
        p = np.frombuffer(raw, dtype="<u2").reshape(h, w, 4)
        return Image.fromarray((p >> 8).astype(np.uint8), "RGBA")
    raise VTFError(f"Unsupported format: {FMT.get(fmt, fmt)}")


def load_vtf(data):
    """Returns a Pillow image from VTF bytes (largest mip, first frame)."""
    info = read_header(data)
    fmt = info.format
    if fmt < 0:
        raise VTFError("No image data")
    w, h = info.width, info.height
    faces = _faces(info)
    start = _hi_res_offset(data, info)
    # mips are stored small to large; the largest (mip 0) is last
    skip = 0
    for mip in range(info.mips - 1, 0, -1):
        mw, mh = max(1, w >> mip), max(1, h >> mip)
        md = max(1, info.depth >> mip)
        skip += _image_size(fmt, mw, mh) * info.frames * faces * md
    size0 = _image_size(fmt, w, h)
    off = start + skip
    raw = data[off: off + size0]
    if len(raw) < size0:
        raise VTFError("VTF data is incomplete or broken")
    return _decode(fmt, w, h, raw), info


CUBE_FACE_NAMES = ("rt", "lf", "bk", "ft", "up", "dn")


def load_vtf_faces(data):
    """The six faces of a cubemap VTF (largest mip, first frame) as {face name: image}, with
    the names of Source skybox faces; None for a texture that is not a cubemap."""
    info = read_header(data)
    fmt = info.format
    if fmt < 0 or not info.flags & TEXTUREFLAGS_ENVMAP:
        return None
    w, h = info.width, info.height
    faces = _faces(info)
    skip = 0
    for mip in range(info.mips - 1, 0, -1):
        mw, mh = max(1, w >> mip), max(1, h >> mip)
        skip += _image_size(fmt, mw, mh) * info.frames * faces * max(1, info.depth >> mip)
    size0 = _image_size(fmt, w, h)
    off = _hi_res_offset(data, info) + skip
    out = {}
    for i, name in enumerate(CUBE_FACE_NAMES):
        raw = data[off + i * size0: off + (i + 1) * size0]
        if len(raw) < size0:
            break
        out[name] = _decode(fmt, w, h, raw)
    return out if len(out) == 6 else None


def load_vtf_frames(data, limit=256):
    """Every frame of an animated VTF (largest mip, first face) as Pillow images."""
    info = read_header(data)
    fmt = info.format
    if fmt < 0:
        raise VTFError("No image data")
    w, h = info.width, info.height
    faces = _faces(info)
    skip = 0
    for mip in range(info.mips - 1, 0, -1):
        mw, mh = max(1, w >> mip), max(1, h >> mip)
        skip += _image_size(fmt, mw, mh) * info.frames * faces * max(1, info.depth >> mip)
    size0 = _image_size(fmt, w, h)
    # mip 0 holds frame after frame, each with its faces and depth slices
    step = size0 * faces * max(1, info.depth)
    off = _hi_res_offset(data, info) + skip
    frames = []
    for i in range(min(info.frames, limit)):
        raw = data[off + i * step: off + i * step + size0]
        if len(raw) < size0:
            break
        frames.append(_decode(fmt, w, h, raw))
    if not frames:
        raise VTFError("VTF data is incomplete or broken")
    return frames, info


def strip_opaque_alpha(img):
    """Converts to RGB if the alpha channel is fully opaque."""
    if img.mode != "RGBA":
        return img
    alpha = np.asarray(img.getchannel("A"))
    if alpha.min() == 255:
        return img.convert("RGB")
    return img


def vtf_to_image_file(vtf_bytes, out_path, vtfcmd=None, log=None):
    """Saves VTF data as TGA/PNG. Returns True on success."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    try:
        img, _info = load_vtf(vtf_bytes)
        img = strip_opaque_alpha(img)
        img.save(out_path)
        return True
    except Exception as e:  # noqa: BLE001
        if log:
            from .i18n import t
            log(t("vtf_fallback", e=e), "dim")
    if vtfcmd and os.path.isfile(vtfcmd):
        return _vtfcmd_convert(vtf_bytes, out_path, vtfcmd)
    return False


def _vtfcmd_convert(vtf_bytes, out_path, vtfcmd):
    ext = os.path.splitext(out_path)[1].lstrip(".").lower() or "tga"
    with tempfile.TemporaryDirectory(prefix="cs2porter_vtf_") as tmp:
        src = os.path.join(tmp, "texture.vtf")
        with open(src, "wb") as f:
            f.write(vtf_bytes)
        try:
            subprocess.run(
                [vtfcmd, "-file", src, "-output", tmp, "-exportformat", ext],
                capture_output=True, timeout=120, creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        produced = os.path.join(tmp, "texture." + ext)
        if not os.path.isfile(produced):
            return False
        with open(produced, "rb") as f:
            data = f.read()
    with open(out_path, "wb") as f:
        f.write(data)
    return True


def image_from_bytes(data, ext, vtfcmd=None):
    """Opens any texture file (vtf/tga/png/jpg) as a Pillow image."""
    if ext.lower() == ".vtf":
        try:
            return load_vtf(data)[0]
        except Exception:
            if vtfcmd:
                with tempfile.TemporaryDirectory(prefix="cs2porter_vtf_") as tmp:
                    out = os.path.join(tmp, "t.png")
                    if _vtfcmd_convert(data, out, vtfcmd):
                        img = Image.open(out)
                        img.load()
                        return img
            raise
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


# ---------------------------------------------------------------------------
# VTF writing (DXT1 / DXT5, with mipmaps, version 7.2)
# ---------------------------------------------------------------------------

def _pow2(n, limit=4096):
    p = 1
    while p * 2 <= n and p * 2 <= limit:
        p *= 2
    # round to the nearest power of two
    if p < limit and n - p > p * 2 - n:
        p *= 2
    return max(1, p)


def _dxt(img, fmt):
    w, h = img.size
    if w < 4 or h < 4:
        img = img.resize((max(4, w), max(4, h)), Image.NEAREST)
    b = io.BytesIO()
    img.save(b, "DDS", pixel_format=fmt)
    d = b.getvalue()
    off = 128 + (20 if d[84:88] == b"DX10" else 0)
    return d[off:off + _image_size(13 if fmt == "DXT1" else 15, w, h)]


def save_vtf(img, path):
    """Saves an image as VTF. DXT5 if there is alpha, DXT1 otherwise."""
    has_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
    img = img.convert("RGBA" if has_alpha else "RGB")
    if has_alpha and np.asarray(img.getchannel("A")).min() == 255:
        img = img.convert("RGB")
        has_alpha = False
    w, h = _pow2(img.width), _pow2(img.height)
    if (w, h) != img.size:
        img = img.resize((w, h), Image.LANCZOS)
    fmt_name, fmt_id = ("DXT5", 15) if has_alpha else ("DXT1", 13)
    mips = []
    level = img
    while True:
        mips.append(_dxt(level, fmt_name))
        if level.width == 1 and level.height == 1:
            break
        level = level.resize((max(1, level.width // 2), max(1, level.height // 2)), Image.LANCZOS)
    lw, lh = min(16, w), min(16, h)
    if w > h:
        lh = max(1, lw * h // w)
    elif h > w:
        lw = max(1, lh * w // h)
    low = _dxt(img.convert("RGB").resize((lw, lh), Image.LANCZOS), "DXT1")
    rgb = np.asarray(img.convert("RGB").resize((1, 1), Image.BOX), dtype=np.float32)[0, 0] / 255.0
    refl = np.power(rgb, 2.2)
    hdr = bytearray(80)
    hdr[0:4] = b"VTF\x00"
    struct.pack_into("<III", hdr, 4, 7, 2, 80)
    flags = 0x2000 if has_alpha else 0                # EIGHTBITALPHA
    struct.pack_into("<HHIHH", hdr, 16, w, h, flags, 1, 0)
    struct.pack_into("<fff", hdr, 32, float(refl[0]), float(refl[1]), float(refl[2]))
    struct.pack_into("<f", hdr, 48, 1.0)
    struct.pack_into("<i", hdr, 52, fmt_id)
    hdr[56] = len(mips)
    struct.pack_into("<i", hdr, 57, 13)
    hdr[61], hdr[62] = lw, lh
    struct.pack_into("<H", hdr, 63, 1)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(bytes(hdr))
        f.write(low)
        for m in reversed(mips):               # small to large
            f.write(m)
    return fmt_name, (w, h)
