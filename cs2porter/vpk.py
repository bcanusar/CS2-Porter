"""Valve VPK (v1 / v2) directory reader and file extractor.

Reads the *_dir.vpk files of Source 1 games (CSS, HL2, CS:GO) and CS2.
"""

import os
import struct
import threading

VPK_SIGNATURE = 0x55AA1234
EMBEDDED_ARCHIVE = 0x7FFF


class VPKError(Exception):
    pass


class VPKArchive:
    def __init__(self, dir_path):
        self.dir_path = dir_path
        if not dir_path.lower().endswith("_dir.vpk"):
            raise VPKError(f"not a _dir.vpk: {dir_path}")
        self.base = dir_path[: -len("_dir.vpk")]
        self.entries = {}          # path_lower -> (preload, archive_index, offset, length)
        self.data_offset = 0
        self._handles = {}
        self._lock = threading.Lock()
        self._read_tree()

    # ------------------------------------------------------------------
    def _read_tree(self):
        with open(self.dir_path, "rb") as f:
            head = f.read(12)
            if len(head) < 12:
                raise VPKError("Could not read the VPK header")
            sig, version, tree_size = struct.unpack("<III", head)
            if sig != VPK_SIGNATURE:
                raise VPKError("Invalid VPK signature")
            if version == 1:
                header_size = 12
            elif version == 2:
                header_size = 28
                f.read(16)
            else:
                raise VPKError(f"Unsupported VPK version: {version}")
            tree = f.read(tree_size)
        self.data_offset = header_size + tree_size

        pos = 0
        n = len(tree)
        entries = self.entries

        def read_str():
            nonlocal pos
            end = tree.index(b"\x00", pos)
            s = tree[pos:end].decode("utf-8", errors="replace")
            pos = end + 1
            return s

        while pos < n:
            ext = read_str()
            if not ext:
                break
            while True:
                path = read_str()
                if not path:
                    break
                while True:
                    name = read_str()
                    if not name:
                        break
                    _crc, preload_len, arch, off, length, _term = struct.unpack_from("<IHHIIH", tree, pos)
                    pos += 18
                    preload = tree[pos:pos + preload_len] if preload_len else b""
                    pos += preload_len
                    full = name if ext == " " else f"{name}.{ext}"
                    if path != " ":
                        full = f"{path}/{full}"
                    entries[full.lower()] = (preload, arch, off, length)

    # ------------------------------------------------------------------
    def __contains__(self, path):
        return path.lower().replace("\\", "/") in self.entries

    def paths(self):
        return self.entries.keys()

    def read(self, path):
        key = path.lower().replace("\\", "/")
        entry = self.entries.get(key)
        if entry is None:
            raise KeyError(path)
        preload, arch, off, length = entry
        if length == 0:
            return preload
        if arch == EMBEDDED_ARCHIVE:
            fname = self.dir_path
            off += self.data_offset
        else:
            fname = f"{self.base}_{arch:03d}.vpk"
        with self._lock:
            fh = self._handles.get(fname)
            if fh is None:
                fh = open(fname, "rb")
                self._handles[fname] = fh
            fh.seek(off)
            data = fh.read(length)
        return preload + data

    def extract(self, path, dest):
        data = self.read(path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        return dest

    def close(self):
        with self._lock:
            for fh in self._handles.values():
                try:
                    fh.close()
                except OSError:
                    pass
            self._handles.clear()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def list_paths_cached(dir_path, cache_dir):
    """For big VPKs like the ones of CS2, caches only the path list."""
    st = os.stat(dir_path)
    tag = f"{st.st_size}_{int(st.st_mtime)}"
    safe = dir_path.replace(":", "").replace("\\", "_").replace("/", "_").replace(" ", "_")
    cache_file = os.path.join(cache_dir, f"vpkindex_{safe[-80:]}_{tag}.txt")
    if os.path.isfile(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            return set(line.rstrip("\n") for line in f)
    arc = VPKArchive(dir_path)
    paths = set(arc.paths())
    arc.close()
    os.makedirs(cache_dir, exist_ok=True)
    # remove old cache files
    prefix = f"vpkindex_{safe[-80:]}_"
    for old in os.listdir(cache_dir):
        if old.startswith(prefix) and old != os.path.basename(cache_file):
            try:
                os.remove(os.path.join(cache_dir, old))
            except OSError:
                pass
    with open(cache_file, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(paths)))
    return paths
