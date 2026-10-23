"""Reads a little of compiled CS2 resources (*.vmat_c ...): the strings of their DATA block.

A resource file is a header plus blocks (RERL, RED2, DATA ...). The DATA block of a material is
binary KeyValues3 (versions 3 to 5), LZ4 compressed; its strings (key names and string values,
the shader name among them) sit together in the first buffer.
"""

import re
import struct

KV3_MAGIC_TAIL = b"3VK"
LZ4 = 1


def lz4_block(src, size):
    """Decompresses one LZ4 block (no frame header) of size bytes."""
    out = bytearray()
    i, n = 0, len(src)
    while i < n and len(out) < size:
        tok = src[i]
        i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n or len(out) >= size:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(out) - off
        if start < 0:
            raise ValueError("bad lz4 offset")
        if off >= ml:
            out += out[start:start + ml]
        else:
            for k in range(ml):
                out.append(out[start + k])
    return bytes(out)


def blocks(data):
    """{block name: bytes} of a compiled resource."""
    _size, _hv, _v, boff, count = struct.unpack_from("<IHHII", data, 0)
    out = {}
    for i in range(count):
        p = 8 + boff + i * 12
        name, off, ln = struct.unpack_from("<4sII", data, p)
        out[name.decode("latin-1")] = data[p + 4 + off:p + 4 + off + ln]
    return out


def kv3_strings_buffer(kv3):
    """The decompressed buffer of a binary KV3 block that holds its strings."""
    if len(kv3) < 64 or kv3[1:4] != KV3_MAGIC_TAIL:
        raise ValueError("not binary KV3")
    ver = kv3[0]
    if ver < 3:
        raise ValueError(f"KV3 version {ver}")
    method = struct.unpack_from("<I", kv3, 20)[0]
    usize, csize = struct.unpack_from("<ii", kv3, 48)
    head = 64
    if ver >= 4:
        head += 8
    if ver >= 5:
        # the values are split into two buffers; the first one has the strings
        usize, csize = struct.unpack_from("<ii", kv3, head)
        head += 48
    raw = kv3[head:head + csize]
    return lz4_block(raw, usize) if method == LZ4 else raw[:usize]


_VFX_RE = re.compile(rb"([a-z0-9_]+\.vfx)\x00")
# whole names only: a model's textures are 'characters/models/x/materials/y.vtex', not 'materials/y.vtex'
_VTEX_RE = re.compile(rb"(?<![!-~])([!-~]+?\.vtex)\x00")


def texture_refs(data):
    """Compiled textures ('materials/x_color_tga_1234abcd.vtex') a compiled material uses, from
    its external reference list (RERL)."""
    try:
        rerl = blocks(data).get("RERL") or b""
    except (ValueError, struct.error):
        return []
    return [m.group(1).decode("ascii") for m in _VTEX_RE.finditer(rerl)]


_VMAT_RE = re.compile(rb"(?<![!-~])([!-~]+?\.vmat)\x00")


def material_refs(data):
    """Materials ('materials/x.vmat') a compiled model uses, from its external reference list."""
    try:
        rerl = blocks(data).get("RERL") or b""
    except (ValueError, struct.error):
        return []
    return [m.group(1).decode("ascii").lower() for m in _VMAT_RE.finditer(rerl)]


def material_shader(data):
    """Shader file name ('csgo_complex.vfx') of a compiled material, None when unreadable."""
    try:
        buf = kv3_strings_buffer(blocks(data)["DATA"])
    except (KeyError, ValueError, IndexError, struct.error):
        return None
    m = _VFX_RE.search(buf)
    return m.group(1).decode("ascii") if m else None
