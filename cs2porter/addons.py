"""CS2 addons: list them and delete one (its content/ and game/ folders) to the Recycle Bin."""

import ctypes
import os
from ctypes import wintypes

SKIP = ("addon_template", "workshop_items")


def roots(cs2_dir):
    return (os.path.join(cs2_dir, "content", "csgo_addons"), os.path.join(cs2_dir, "game", "csgo_addons"))


def names(cs2_dir):
    """Addons of content/ and game/ (one that only has one of the two shows up too)."""
    found = {}
    for base in roots(cs2_dir):
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for n in entries:
            if n.lower() not in SKIP and os.path.isdir(os.path.join(base, n)):
                found.setdefault(n.lower(), n)
    return sorted(found.values(), key=str.lower)


def _walk_size(path):
    size, newest = 0, 0.0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                st = os.stat(os.path.join(dirpath, f))
            except OSError:
                continue
            size += st.st_size
            newest = max(newest, st.st_mtime)
    return size, newest


def info(cs2_dir, name):
    """{'name', 'maps': [map names], 'size': bytes of both folders, 'mtime', 'folders': [paths]}"""
    folders = [os.path.join(base, name) for base in roots(cs2_dir) if os.path.isdir(os.path.join(base, name))]
    size, newest = 0, 0.0
    for f in folders:
        s, m = _walk_size(f)
        size += s
        newest = max(newest, m)
    maps = set()
    for f in folders:
        try:
            entries = os.listdir(os.path.join(f, "maps"))
        except OSError:
            continue
        for e in entries:
            base, ext = os.path.splitext(e)
            if ext.lower() in (".vmap", ".vpk"):
                maps.add(base)
    return {"name": name, "maps": sorted(maps, key=str.lower), "size": size, "mtime": newest, "folders": folders}


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


FO_DELETE = 3
FOF_SILENT = 0x4
FOF_NOCONFIRMATION = 0x10
FOF_ALLOWUNDO = 0x40
FOF_NOERRORUI = 0x400


def _to_recycle_bin(path):
    op = _SHFILEOPSTRUCTW(None, FO_DELETE, os.path.abspath(path) + "\0\0", None,
                          FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not op.fAnyOperationsAborted


def delete(cs2_dir, name):
    """Moves the addon's content/ and game/ folders to the Recycle Bin. Returns the folders that
    are still there (a file in use, for example)."""
    if not name or name.lower() in SKIP or name != os.path.basename(name) or name in (".", ".."):
        raise ValueError(name)
    left = []
    for base in roots(cs2_dir):
        path = os.path.join(base, name)
        if not os.path.isdir(path):
            continue
        try:
            ok = _to_recycle_bin(path)
        except (AttributeError, OSError):
            ok = False
        if not ok or os.path.exists(path):
            left.append(path)
    return left
