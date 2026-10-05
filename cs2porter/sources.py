"""Asset sources: embedded BSP files, Resources folders, Source 1 game files
(folder + VPK) and a check for files that CS2 already has."""

import glob
import os

from . import kv
from .i18n import t
from .vpk import VPKArchive, VPKError

IMAGE_EXTS = (".tga", ".png", ".jpg", ".jpeg", ".psd", ".tif", ".tiff", ".bmp")


def _norm(rel):
    return rel.replace("\\", "/").lower().lstrip("/")


class DirSource:
    """A root folder of loose files (indexes materials/ and models/ under it).

    role: embedded | extra | resources | game (| loose: LooseSource)
    """

    kind = "dir"

    def __init__(self, root, label, subdirs=("materials", "models", "sound", "scripts"), role="game"):
        self.root = root
        self.label = label
        self.subdirs = subdirs
        self.role = role
        self._index = None

    def _build(self):
        idx = {}
        for sub in self.subdirs:
            base = os.path.join(self.root, sub)
            if not os.path.isdir(base):
                continue
            for dp, _dn, fn in os.walk(base):
                rel_dir = os.path.relpath(dp, self.root).replace("\\", "/").lower()
                for f in fn:
                    idx[f"{rel_dir}/{f.lower()}"] = os.path.join(dp, f)
        self._index = idx

    @property
    def index(self):
        if self._index is None:
            self._build()
        return self._index

    def has(self, rel):
        return _norm(rel) in self.index

    def path(self, rel):
        return self.index.get(_norm(rel))

    def read(self, rel):
        p = self.path(rel)
        with open(p, "rb") as f:
            return f.read()

    def files_with_ext(self, ext):
        ext = ext.lower()
        return [(k, v) for k, v in self.index.items() if k.endswith(ext)]


def _common_tail(a, b):
    n = 0
    for x, y in zip(reversed(a), reversed(b)):
        if x != y:
            break
        n += 1
    return n


class LooseSource(DirSource):
    """Every file of a folder, wherever it sits in it: a game path (materials/decals/x.vtf) is
    found by its file name, and among files with the same name by the longest matching end of
    the path (decals/x.vtf before other/x.vtf). For files the user put in a folder by hand."""

    MAX_FILES = 300000

    def _build(self):
        idx, names = {}, {}
        for dp, _dn, fn in os.walk(self.root):
            for f in fn:
                full = os.path.join(dp, f)
                rel = os.path.relpath(full, self.root).replace("\\", "/").lower()
                idx[rel] = full
                names.setdefault(f.lower(), []).append(rel)
            if len(idx) > self.MAX_FILES:
                break
        self._names = names
        self._index = idx

    def path(self, rel):
        rel = _norm(rel)
        idx = self.index
        if rel in idx:
            return idx[rel]
        parts = rel.split("/")
        cands = self._names.get(parts[-1])
        if not cands:
            return None
        return idx[max(cands, key=lambda c: _common_tail(c.split("/"), parts))]

    def has(self, rel):
        return self.path(rel) is not None


class VPKSource:
    kind = "vpk"

    def __init__(self, dir_path, label, role="game"):
        self.label = label
        self.dir_path = dir_path
        self.role = role
        self._arc = None

    @property
    def arc(self):
        if self._arc is None:
            self._arc = VPKArchive(self.dir_path)
        return self._arc

    def has(self, rel):
        return _norm(rel) in self.arc.entries

    def path(self, rel):
        return None

    def read(self, rel):
        return self.arc.read(_norm(rel))

    def close(self):
        if self._arc is not None:
            self._arc.close()


# ---------------------------------------------------------------------------
# gameinfo.txt search paths
# ---------------------------------------------------------------------------

def gameinfo_search_paths(gamedir):
    """Resolves the SearchPaths in gameinfo.txt (folder / vpk) into an ordered list."""
    gi = os.path.join(gamedir, "gameinfo.txt")
    if not os.path.isfile(gi):
        return [("dir", gamedir)]
    base = os.path.dirname(os.path.normpath(gamedir))
    try:
        root = kv.parse_file(gi)
    except OSError:
        return [("dir", gamedir)]
    gameinfo = root.block("GameInfo") or (root.blocks()[0] if root.blocks() else None)
    fs = gameinfo.block("FileSystem") if gameinfo else None
    sp = fs.block("SearchPaths") if fs else None
    if sp is None:
        return [("dir", gamedir)]
    out = []
    seen = set()

    def add(kind, p):
        key = (kind, os.path.normcase(os.path.normpath(p)))
        if key not in seen:
            seen.add(key)
            out.append((kind, os.path.normpath(p)))

    for key, val in sp.values():
        k = key.lower()
        if "gamebin" in k or k.startswith("platform"):
            continue
        v = val.strip()
        v = v.replace("|gameinfo_path|", gamedir + os.sep).replace("|all_source_engine_paths|", base + os.sep)
        if not os.path.isabs(v):
            v = os.path.join(base, v)
        v = os.path.normpath(v)
        if v.endswith("*"):
            parent = os.path.dirname(v)
            if os.path.isdir(parent):
                for name in sorted(os.listdir(parent)):
                    p = os.path.join(parent, name)
                    if os.path.isdir(p):
                        add("dir", p)
                    elif name.lower().endswith("_dir.vpk"):
                        add("vpk", p)
            continue
        if v.lower().endswith(".vpk"):
            dirvpk = v[:-4] + "_dir.vpk"
            if os.path.isfile(dirvpk):
                add("vpk", dirvpk)
            elif os.path.isfile(v):
                add("vpk", v)
            continue
        if os.path.isdir(v):
            add("dir", v)
            # newer branches (CS:GO, Portal 2, L4D2) mount <game path>/pak01_dir.vpk on their own
            pak = os.path.join(v, "pak01_dir.vpk")
            if os.path.isfile(pak):
                add("vpk", pak)
    return out


def _is_sound_vpk(path):
    n = os.path.basename(path).lower()
    return "sound" in n or "_vo_" in n


# ---------------------------------------------------------------------------
# Source manager
# ---------------------------------------------------------------------------

class AssetSources:
    """Search order: extra folders, embedded files, resource folders, installed games, and last
    every other file of the extra folders (loose_dirs, matched by file name)."""

    def __init__(self, embedded_dir=None, resource_dirs=(), gamedirs=(), log=None, extra_dirs=(),
                 loose_dirs=()):
        self.sources = []
        self.sound_sources = []     # sound VPKs, only opened when a sound is looked up
        self.log = log
        # extra folders: (kind, path) entries like the resource folders, or plain folders
        for r in extra_dirs:
            kind, d = r if isinstance(r, tuple) else ("dir", r)
            label = t("src_extra", name=os.path.basename(os.path.normpath(d)))
            if kind == "dir" and d and os.path.isdir(d):
                self.sources.append(DirSource(d, label, role="extra"))
            elif kind == "vpk" and os.path.isfile(d):
                try:
                    src = VPKSource(d, label, role="extra")
                    _ = src.arc
                    self.sources.append(src)
                except (OSError, VPKError) as e:
                    if log:
                        log(t("src_vpk_fail", p=d, e=e), "warn")
        if embedded_dir and os.path.isdir(embedded_dir):
            self.sources.append(DirSource(embedded_dir, t("src_embedded"), role="embedded"))
        # resource folders: (kind, path) entries of config.resource_entries, or plain folders
        for r in resource_dirs:
            kind, p = r if isinstance(r, tuple) else ("dir", r)
            label = t("src_resources", name=os.path.basename(os.path.normpath(p)))
            if kind == "dir" and os.path.isdir(p):
                self.sources.append(DirSource(p, label, role="resources"))
            elif kind == "vpk" and os.path.isfile(p):
                try:
                    src = VPKSource(p, label, role="resources")
                    _ = src.arc
                    self.sources.append(src)
                except (OSError, VPKError) as e:
                    if log:
                        log(t("src_vpk_fail", p=p, e=e), "warn")
        seen = set()
        for gd in gamedirs:
            if not gd or not os.path.isdir(gd):
                continue
            game = os.path.basename(os.path.dirname(os.path.normpath(gd)))
            for kind, p in gameinfo_search_paths(gd):
                key = os.path.normcase(p)
                if key in seen:
                    continue
                seen.add(key)
                if kind == "vpk":
                    if _is_sound_vpk(p):
                        src = VPKSource(p, t("src_game_vpk", game=game, file=os.path.basename(p)))
                        src.game = game
                        self.sound_sources.append(src)
                        continue
                    try:
                        src = VPKSource(p, t("src_game_vpk", game=game, file=os.path.basename(p)))
                        src.game = game
                        _ = src.arc
                        self.sources.append(src)
                    except (OSError, VPKError) as e:
                        if log:
                            log(t("src_vpk_fail", p=p, e=e), "warn")
                else:
                    src = DirSource(p, t("src_game_dir", game=game, name=os.path.basename(p)))
                    src.game = game
                    self.sources.append(src)
        for d in loose_dirs:
            if d and os.path.isdir(d):
                self.sources.append(LooseSource(d, t("src_extra", name=os.path.basename(os.path.normpath(d))),
                                                role="loose"))

    @property
    def embedded(self):
        return next((s for s in self.sources if s.role == "embedded"), None)

    def resource_sources(self):
        """For the QC lookup: loose files of the extra folders and resource folders (in priority order)."""
        return [s for s in self.sources if s.role in ("extra", "resources") and isinstance(s, DirSource)]

    def compiled_sources(self):
        """Sources searched for compiled .mdl files (every source, in search order)."""
        return [s for s in self.sources if s.role in ("extra", "embedded", "resources", "game", "loose")]

    def find(self, rel):
        rel = _norm(rel)
        for s in self.sources:
            if s.has(rel):
                return s
        return None

    def find_any(self, rels):
        for s in self.sources:
            for r in rels:
                if s.has(r):
                    return s, _norm(r)
        return None, None

    def read(self, rel):
        s = self.find(rel)
        if s is None:
            return None, None
        return s.read(rel), s

    def read_sound(self, rel):
        """Like read(), but the sound VPKs are searched too. Returns (data, source)."""
        data, src = self.read(rel)
        if src is not None:
            return data, src
        rel = _norm(rel)
        for s in self.sound_sources:
            try:
                if s.has(rel):
                    return s.read(rel), s
            except (OSError, VPKError):
                continue
        return None, None

    def find_texture(self, tex):
        """tex: path under materials/ without extension. Returns (source, relative_path, extension)."""
        tex = _norm(tex)
        cands = [f"materials/{tex}{e}" for e in IMAGE_EXTS] + [f"materials/{tex}.vtf"]
        for s in self.sources:
            for c in cands:
                if s.has(c):
                    return s, c, os.path.splitext(c)[1]
        return None, None, None

    def close(self):
        for s in self.sources + self.sound_sources:
            if isinstance(s, VPKSource):
                s.close()


class CS2Index:
    """Files that already exist in the CS2 game (VPK) and in the target addon.

    content_dir: folder the files are written to (addon content folder or a custom folder)
    game_dir:    compiled (game) folder of the addon, or None
    """

    def __init__(self, cs2_dir, content_dir, game_dir=None, log=None):
        self.cs2_dir = cs2_dir
        self.content_dir = content_dir
        self.game_dir = game_dir
        self.paths = set()
        if cs2_dir:
            for rel in ("game/csgo/pak01_dir.vpk", "game/core/pak01_dir.vpk"):
                p = os.path.join(cs2_dir, *rel.split("/"))
                if os.path.isfile(p):
                    try:
                        arc = VPKArchive(p)
                        self.paths.update(arc.entries.keys())
                        arc.close()
                    except (OSError, VPKError) as e:
                        if log:
                            log(t("src_cs2_vpk_fail", p=p, e=e), "warn")

    def in_cs2(self, rel_c):
        return _norm(rel_c) in self.paths

    def in_addon(self, rel_src):
        rel_src = _norm(rel_src)
        if os.path.isfile(os.path.join(self.content_dir, *rel_src.split("/"))):
            return True
        if self.game_dir and os.path.isfile(os.path.join(self.game_dir, *(rel_src + "_c").split("/"))):
            return True
        return False


def glob_vpks(folder):
    return sorted(glob.glob(os.path.join(folder, "*_dir.vpk")))
