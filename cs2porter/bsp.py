"""BSP file info and extraction of the embedded (pakfile) files."""

import io
import os
import re
import struct
import zipfile

from .i18n import t

LUMP_ENTITIES = 0
LUMP_PLANES = 1
LUMP_TEXDATA = 2
LUMP_TEXINFO = 6
LUMP_BRUSHES = 18
LUMP_BRUSHSIDES = 19
LUMP_PAKFILE = 40
LUMP_GAME_LUMP = 35
LUMP_TEXDATA_STRING_DATA = 43
LUMP_TEXDATA_STRING_TABLE = 44

BSP_GAMES = {
    17: "Vampire: Bloodlines",
    18: "Half-Life 2 (beta)",
    19: "Source 2004 (HL2 / CSS)",
    20: "Source 2007+ (CSS / HL2 / TF2 / GMod)",
    21: "Source 2009+ (CS:GO / L4D2 / Portal 2)",
    22: "Dota 2 (Source 1)",
    23: "Dota 2 (Source 1)",
}

# files made by vbsp that CS2 does not need ("smart unpack" logic)
_CUBEMAP_VTF_RE = re.compile(r"^materials/maps/[^/]+/c-?\d+_-?\d+_-?\d+(\.hdr)?\.vtf$|^materials/maps/[^/]+/cubemapdefault(\.hdr)?\.vtf$")
_GENERATED_RE = re.compile(r"^materials/maps/[^/]+/.*_-?\d+_-?\d+_-?\d+\.vmt$|^materials/maps/[^/]+/.*_wvt_patch\.vmt$|^maps/.+\.(cache|lmp)$")


class BSPError(Exception):
    pass


class BSPInfo:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            header = f.read(8 + 64 * 16 + 4)
        if len(header) < 8 + 64 * 16:
            raise BSPError("Could not read the BSP header")
        ident = header[:4]
        if ident != b"VBSP":
            raise BSPError(f"No VBSP signature ({ident!r}). Unsupported BSP format.")
        self.version = struct.unpack_from("<i", header, 4)[0]
        self.lumps = []
        for i in range(64):
            ofs, length, ver, fourcc = struct.unpack_from("<iii4s", header, 8 + i * 16)
            self.lumps.append((ofs, length, ver, fourcc))
        self.map_revision = struct.unpack_from("<i", header, 8 + 64 * 16)[0] if len(header) >= 8 + 64 * 16 + 4 else 0

    @property
    def game_guess(self):
        return BSP_GAMES.get(self.version) or t("bsp_unknown", v=self.version)

    @property
    def is_csgo_family(self):
        return self.version >= 21

    def read_lump(self, index):
        ofs, length, _ver, _fourcc = self.lumps[index]
        if length <= 0:
            return b""
        with open(self.path, "rb") as f:
            f.seek(ofs)
            data = f.read(length)
        if data[:4] == b"LZMA":
            data = _decompress_valve_lzma(data)
        return data

    def pakfile(self):
        data = self.read_lump(LUMP_PAKFILE)
        if not data:
            return None
        return zipfile.ZipFile(io.BytesIO(data))

    def worldspawn_keys(self):
        """Reads the worldspawn keys from the entity lump (skyname etc.)."""
        try:
            data = self.read_lump(LUMP_ENTITIES)
        except Exception:
            return {}
        text = data.split(b"\x00", 1)[0].decode("latin-1", errors="replace")
        end = text.find("}")
        block = text[: end if end != -1 else len(text)]
        return {k.lower(): v for k, v in re.findall(r'"([^"]+)"\s+"([^"]*)"', block)}

    def entities(self):
        """Every entity of the entity lump as {key: value} (lower case keys, first value wins)."""
        try:
            data = self.read_lump(LUMP_ENTITIES)
        except Exception:
            return []
        text = data.split(b"\x00", 1)[0].decode("latin-1", errors="replace")
        out = []
        for block in re.findall(r"\{([^{}]*)\}", text):
            ent = {}
            for k, v in re.findall(r'"([^"]+)"\s+"([^"]*)"', block):
                ent.setdefault(k.lower(), v)
            out.append(ent)
        return out

    def brushes(self):
        """{frozenset of plane keys: (contents, {plane key: texture})} for every brush of the map
        (bevel sides left out, lower case texture names). See plane_key()."""
        planes = self.read_lump(LUMP_PLANES)
        texinfo = self.read_lump(LUMP_TEXINFO)
        texdata = self.read_lump(LUMP_TEXDATA)
        strdata = self.read_lump(LUMP_TEXDATA_STRING_DATA)
        strtab = self.read_lump(LUMP_TEXDATA_STRING_TABLE)
        brushes = self.read_lump(LUMP_BRUSHES)
        sides = self.read_lump(LUMP_BRUSHSIDES)
        names = {}

        def tex(ti):
            if ti not in names:
                name = ""
                try:
                    td = struct.unpack_from("<i", texinfo, ti * 72 + 68)[0]
                    sid = struct.unpack_from("<i", texdata, td * 32 + 12)[0]
                    off = struct.unpack_from("<i", strtab, sid * 4)[0]
                    name = strdata[off:strdata.index(b"\0", off)].decode("latin-1").lower()
                except (struct.error, ValueError, IndexError):
                    pass
                names[ti] = name
            return names[ti]

        out = {}
        for b in range(len(brushes) // 12):
            first, num, contents = struct.unpack_from("<3i", brushes, b * 12)
            found = {}
            for s in range(first, min(first + num, len(sides) // 8)):
                pn, ti = struct.unpack_from("<Hh", sides, s * 8)
                if sides[s * 8 + 6] or pn * 20 + 16 > len(planes):    # bevel side
                    continue
                nx, ny, nz, dist = struct.unpack_from("<4f", planes, pn * 20)
                found[plane_key((nx, ny, nz), dist)] = tex(ti) if ti >= 0 else ""
            if found:
                out.setdefault(frozenset(found), (contents, found))
        return out


def plane_key(normal, dist):
    """A plane rounded so that the planes of a .bsp and of its decompiled .vmf compare equal."""
    return (round(normal[0], 3) + 0.0, round(normal[1], 3) + 0.0, round(normal[2], 3) + 0.0,
            round(dist, 1) + 0.0)


def _decompress_valve_lzma(data):
    import lzma
    # Valve LZMA header: "LZMA" + actualSize(4) + lzmaSize(4) + props(5)
    actual = struct.unpack_from("<I", data, 4)[0]
    props = data[12:17]
    payload = data[17:]
    # convert to the lzma_alone format: props(5) + uncompressed size(8)
    alone = props + struct.pack("<Q", actual) + payload
    return lzma.decompress(alone, format=lzma.FORMAT_ALONE)[:actual]


def is_generated_file(name):
    n = name.lower().replace("\\", "/")
    return bool(_CUBEMAP_VTF_RE.match(n) or _GENERATED_RE.match(n))


def extract_pakfile(bsp_path, out_dir, smart=True, log=None):
    """Extracts the files embedded in the BSP into out_dir. Returns the extracted relative paths."""
    info = BSPInfo(bsp_path)
    zf = info.pakfile()
    if zf is None:
        return []
    extracted = []
    skipped = 0
    failed = 0
    with zf:
        for zi in zf.infolist():
            if zi.is_dir():
                continue
            name = zi.filename.replace("\\", "/").lstrip("/")
            if smart and is_generated_file(name):
                skipped += 1
                continue
            dest = os.path.join(out_dir, *name.split("/"))
            try:
                data = zf.read(zi)
            except Exception as e:  # noqa: BLE001
                failed += 1
                if log:
                    log(t("bsp_read_fail", name=name, e=e), "warn")
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            extracted.append(name.lower())
    if log:
        msg = t("bsp_extracted", n=len(extracted))
        if skipped:
            msg += t("bsp_skipped", n=skipped)
        if failed:
            msg += t("bsp_failed", n=failed)
        log(msg + ".", "ok" if not failed else "warn")
    return extracted
