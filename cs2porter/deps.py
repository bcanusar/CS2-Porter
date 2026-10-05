"""Files that go with a file: the textures of a material (.vmt), the parts and materials of a
model (.mdl), the files a .qc points at, the textures of a CS2 material (.vmat) and the files of
a CS2 model (.vmdl). Used when files are exported, so they can be taken along."""

import os
import re

from . import config
from .materials import norm_tex, parse_vmt
from .mdl import MDL
from .sources import AssetSources, VPKSource, _norm
from .vpk import VPKArchive

MODEL_PARTS = (".vvd", ".dx90.vtx", ".dx80.vtx", ".sw.vtx", ".vtx", ".phy", ".ani")
TEXTURE_EXTS = (".vtf", ".tga", ".png", ".jpg", ".jpeg", ".psd")
# material keys that hold a texture (besides every key that ends with "texture" or "map")
TEXTURE_KEYS = {"$basetexture", "$basetexture2", "$basetexture3", "$basetexture4", "$texture2", "$detail",
                "$detail1", "$detail2", "$envmapmask", "$envmapmask2", "$selfillummask", "$iris", "%tooltexture",
                "$compress", "$stretch", "$sheenmapmask", "$blendmodulatetexture", "$fleshbordertexture1d"}
MATERIAL_KEYS = ("$bottommaterial", "$underwateroverlay", "$fallbackmaterial", "$crackmaterial")
_SKIP_TEXTURES = ("_rt_", "env_cubemap", "_white", "_black", "_grey")
_QC_FILE_RE = re.compile(r'"?([^"\s{}]+\.(?:smd|dmx|vta|qci|vrd))"?', re.I)
_VMAT_FILE_RE = re.compile(r'"([^"]+\.(?:tga|png|jpg|jpeg|psd|tif|tiff|bmp|exr|vtex))"', re.I)
_VMDL_FILE_RE = re.compile(r'"([^"]+\.(?:smd|dmx|fbx|obj|vmat))"', re.I)
MAX_DEPTH = 4


def _kind(rel):
    ext = os.path.splitext(rel)[1].lower()
    if ext in (".vmt", ".vmat"):
        return "material"
    if ext in TEXTURE_EXTS or ext == ".vtex":
        return "texture"
    if ext in (".mdl", ".vvd", ".vtx", ".phy", ".ani", ".qc", ".qci", ".smd", ".dmx", ".vta", ".vmdl", ".fbx", ".obj"):
        return "model"
    return "file"


class _Store:
    """Reads and finds game files: next to the file that uses them first (same package, same
    .bsp, same game folder), then in every source."""

    def __init__(self, cfg, extra=None):
        extra_dirs = [extra] if extra and os.path.isdir(extra) else []
        self.sources = AssetSources(None, config.resource_entries(cfg.get("resource_dirs")),
                                    config.find_s1_gamedirs(None),
                                    extra_dirs=config.resource_entries(extra_dirs), loose_dirs=extra_dirs)
        self.bsp = extra if extra and extra.lower().endswith(".bsp") and os.path.isfile(extra) else None
        self._paks, self._vpks = {}, {}

    def close(self):
        self.sources.close()
        for arc in self._vpks.values():
            arc.close()

    def _vpk(self, path):
        if path not in self._vpks:
            self._vpks[path] = VPKArchive(path)
        return self._vpks[path]

    def _pak(self, path):
        if path not in self._paks:
            from .bsp import BSPInfo
            zf = BSPInfo(path).pakfile()
            self._paks[path] = (zf, {z.filename.replace("\\", "/").lower(): z.filename
                                     for z in zf.infolist()} if zf else {})
        return self._paks[path]

    def read(self, rel, where):
        rel = _norm(rel)
        low = where.lower()
        try:
            if low.endswith(".vpk"):
                return self._vpk(where).read(rel)
            if low.endswith(".bsp"):
                zf, names = self._pak(where)
                return zf.read(names[rel]) if zf and rel in names else None
            with open(where, "rb") as f:
                return f.read()
        except (OSError, KeyError, ValueError):
            return None

    def locate(self, rel, near=None):
        """Where rel is (a file, a .vpk or a .bsp), None when it is nowhere. near: (where,
        game path) of the file that uses it."""
        rel = _norm(rel)
        if near:
            where, near_rel = near
            low = where.lower()
            try:
                if low.endswith(".vpk"):
                    if rel in self._vpk(where).entries:
                        return where
                elif low.endswith(".bsp"):
                    if rel in self._pak(where)[1]:
                        return where
                elif os.path.isfile(where):
                    full = where.replace("\\", "/")
                    tail = "/" + _norm(near_rel)
                    if full.lower().endswith(tail):
                        p = os.path.normpath(full[:-len(tail)] + "/" + rel)
                        if os.path.isfile(p):
                            return p
                    # a file put somewhere by hand: its own folder, by file name
                    p = os.path.join(os.path.dirname(where), rel.split("/")[-1])
                    if os.path.isfile(p):
                        return p
            except (OSError, ValueError):
                pass
        if self.bsp:
            try:
                if rel in self._pak(self.bsp)[1]:
                    return self.bsp
            except (OSError, ValueError):
                pass
        src = self.sources.find(rel)
        if src is None:
            return None
        return src.dir_path if isinstance(src, VPKSource) else src.path(rel)

    def texture(self, name, near):
        """(game path, where) of a texture name of a material (where None: not found)."""
        base = "materials/" + norm_tex(name)
        for ext in TEXTURE_EXTS:
            where = self.locate(base + ext, near)
            if where:
                return base + ext, where
        return base + ".vtf", None


def _vmt_refs(store, rel, data, near):
    """(textures, materials) a .vmt uses."""
    includes = []

    def loader(inc):
        includes.append(inc)
        where = store.locate(inc, near)
        return store.read(inc, where) if where else None

    try:
        vmt = parse_vmt(data, rel, loader)
    except Exception:  # noqa: BLE001 - a broken material has no files to take along
        return [], includes
    textures, materials = [], list(includes)
    for key, val in vmt.params.items():
        v = str(val).strip().strip('"')
        if not v or not re.search(r"[a-z]", v, re.I) or v.startswith(("[", "{")):
            continue
        if key in MATERIAL_KEYS:
            m = v.replace("\\", "/").lower().lstrip("/")
            if not m.startswith("materials/"):
                m = "materials/" + m
            materials.append(m if m.endswith(".vmt") else m + ".vmt")
        elif key in TEXTURE_KEYS or key.endswith("texture") or key.endswith("map"):
            if v.lower().startswith(_SKIP_TEXTURES) or norm_tex(v).startswith(_SKIP_TEXTURES):
                continue
            textures.append(v)
    return textures, materials


def _mdl_refs(store, rel, data, near):
    """[(game path, where)] of the parts and materials of a .mdl."""
    out = []
    stem = rel[:-4]
    for ext in MODEL_PARTS:
        where = store.locate(stem + ext, near)
        if where:
            out.append((stem + ext, where))
    try:
        mdl = MDL(data)
    except Exception:  # noqa: BLE001
        return out
    cds = [c.replace("\\", "/").strip("/").lower() for c in mdl.cdmaterials] or [""]
    for tex in mdl.textures:
        name = tex.replace("\\", "/").lower().strip("/")
        hit = None
        for cd in cds:
            m = f"materials/{cd}/{name}.vmt" if cd else f"materials/{name}.vmt"
            where = store.locate(m, near)
            if where:
                hit = (m, where)
                break
        out.append(hit or (f"materials/{cds[0]}/{name}.vmt".replace("//", "/"), None))
    return out


def _text_refs(rel, data, pattern):
    """Files a text file points at; paths without a folder are next to the file."""
    try:
        text = data.decode("utf-8", "replace")
    except AttributeError:
        return []
    base = rel.rsplit("/", 1)[0] if "/" in rel else ""
    out = []
    for m in pattern.finditer(text):
        p = m.group(1).replace("\\", "/").lstrip("/")
        if p.lower().startswith(("materials/", "models/", "sounds/")) or not base:
            out.append(p.lower())
        else:
            out.append(os.path.normpath(base + "/" + p).replace("\\", "/").lower())
    return out


def find(cfg, items, extra=None, check=None):
    """items: [(game path, where)] of the files to export. Returns the files they use:
    [{"rel", "where" (None: not found), "parent", "kind"}], without the items themselves."""
    store = _Store(cfg, extra)
    out, seen = [], {_norm(r) for r, _w in items}
    try:
        todo = [(_norm(r), w, 0) for r, w in items]
        while todo:
            if check:
                check()
            rel, where, depth = todo.pop(0)
            if depth >= MAX_DEPTH or not where:
                continue
            ext = os.path.splitext(rel)[1].lower()
            if ext not in (".vmt", ".mdl", ".qc", ".vmat", ".vmdl"):
                continue
            data = store.read(rel, where)
            if not data:
                continue
            near = (where, rel)
            found = []
            if ext == ".vmt":
                textures, materials = _vmt_refs(store, rel, data, near)
                found += [store.texture(tx, near) for tx in textures]
                found += [(m, store.locate(m, near)) for m in materials]
            elif ext == ".mdl":
                found += _mdl_refs(store, rel, data, near)
            else:
                pattern = {".qc": _QC_FILE_RE, ".vmat": _VMAT_FILE_RE, ".vmdl": _VMDL_FILE_RE}[ext]
                found += [(p, store.locate(p, near)) for p in _text_refs(rel, data, pattern)]
            for dep, dwhere in found:
                dep = _norm(dep)
                if dep in seen:
                    continue
                seen.add(dep)
                out.append({"rel": dep, "where": dwhere, "parent": rel, "kind": _kind(dep)})
                todo.append((dep, dwhere, depth + 1))
    finally:
        store.close()
    return out
