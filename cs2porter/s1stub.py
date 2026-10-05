"""Builds a temporary Source 1 game folder in the work folder for the map import.

The importer reads the VMT of every material and the size of its texture to work
out the texture scale of brush faces. Only the VMTs and empty VTF files of the
right size are written into the work folder, so no game folder is touched and no
admin rights are needed.

Note: the importer does not accept absolute search paths and cannot read a VTF
that only has a header, so full size zero-filled DXT1 files are written (the
output is identical to the one made with the real textures).
"""

import io
import os
import shutil
import struct

from PIL import Image

from . import vtf as vtfmod
from .materials import norm_tex, parse_vmt

_TEX_KEYS = ("$basetexture", "$basetexture2", "$hdrbasetexture", "$hdrcompressedtexture",
             "$dudvmap", "$normalmap", "$bumpmap", "$bumpmap2")
_SKY_FACES = ("up", "dn", "lf", "rt", "ft", "bk")

GAMEINFO = """"GameInfo"
{
	game	"CS2 Porter"
	FileSystem
	{
		SteamAppId	240
		SearchPaths
		{
			game+mod	|gameinfo_path|.
		}
	}
}
"""


def _vtf_stub(w, h):
    hdr = bytearray(80)
    hdr[0:4] = b"VTF\x00"
    struct.pack_into("<III", hdr, 4, 7, 2, 80)
    struct.pack_into("<HHIHH", hdr, 16, w, h, 0, 1, 0)
    struct.pack_into("<fff", hdr, 32, 0.5, 0.5, 0.5)
    struct.pack_into("<f", hdr, 48, 1.0)
    struct.pack_into("<i", hdr, 52, 13)          # DXT1
    hdr[56] = 1                                  # mip count
    struct.pack_into("<i", hdr, 57, -1)          # no thumbnail
    struct.pack_into("<H", hdr, 63, 1)
    return bytes(hdr) + bytes(max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * 8)


def _texture_size(sources, tex):
    src, rel, ext = sources.find_texture(tex)
    if src is None:
        return None
    try:
        data = src.read(rel)
        if ext == ".vtf":
            info = vtfmod.read_header(data)
            return info.width, info.height
        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:  # noqa: BLE001
        return None


def build_stub_game(work_dir, sources, materials, skyname="", aliases=None):
    """Returns the gameinfo folder (.../csgo) and the number of prepared materials."""
    root = os.path.join(work_dir, "s1game")
    shutil.rmtree(root, ignore_errors=True)
    gi = os.path.join(root, "csgo")
    os.makedirs(gi, exist_ok=True)
    with open(os.path.join(gi, "gameinfo.txt"), "w", encoding="utf-8") as f:
        f.write(GAMEINFO)

    written_vmt = set()
    written_tex = set()

    def put(rel, data):
        p = os.path.join(gi, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def loader(rel):
        data, _src = sources.read(rel)
        if data is not None:
            key = rel.lower()
            if key not in written_vmt:
                written_vmt.add(key)
                put(key, data)
        return data

    names = [f"materials/{m}.vmt" for m in materials]
    if skyname:
        sky = skyname.lower()
        for face in _SKY_FACES:
            names.append(f"materials/skybox/{sky}{face}.vmt")
            names.append(f"materials/skybox/{sky}_hdr{face}.vmt")

    count = 0
    for rel in names:
        rel = rel.replace("\\", "/").lower()
        while "//" in rel:
            rel = rel.replace("//", "/")
        data = loader(rel)
        if data is None:
            continue
        try:
            vmt = parse_vmt(data, rel, loader)
        except Exception:  # noqa: BLE001
            continue
        for key in _TEX_KEYS:
            val = vmt.get(key)
            if not val:
                continue
            tex = norm_tex(val)
            if not tex or tex in written_tex:
                continue
            size = _texture_size(sources, tex)
            if size is None:
                continue
            written_tex.add(tex)
            put(f"materials/{tex}.vtf", _vtf_stub(*size))
        count += 1

    # stand-in materials ({alias: real}): a plain VMT and texture under the alias name with
    # the size of the real texture, so nothing in it reminds the importer of the real material
    for alias, real in (aliases or {}).items():
        real_tex = real
        data = loader(f"materials/{real}.vmt")
        if data is not None:
            try:
                real_tex = norm_tex(parse_vmt(data, f"materials/{real}.vmt", loader).get("$basetexture") or real) or real
            except Exception:  # noqa: BLE001
                real_tex = real
        put(f"materials/{alias}.vtf", _vtf_stub(*(_texture_size(sources, real_tex) or (128, 128))))
        put(f"materials/{alias}.vmt", f'"LightmappedGeneric"\n{{\n\t"$basetexture" "{alias}"\n}}\n'.encode())
        count += 1
    return gi, count


def remove_stub_game(work_dir):
    shutil.rmtree(os.path.join(work_dir, "s1game"), ignore_errors=True)
