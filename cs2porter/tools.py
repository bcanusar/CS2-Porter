"""'Tools' tab: tehlikeli91's scripts plus some extra helper tools.

The user adds files (or folders and archives, searched for the files a tool takes) to a list;
items() turns them into the rows of that list and run_tool() converts the rows into the output
folder. status(key, status, detail) reports each row's result (key: the row id or its file).
"""

import contextlib
import hashlib
import os
import re
import shutil
import threading
import zipfile

import numpy as np
from PIL import Image

from . import mdl as mdlmod
from . import vtf as vtfmod
from .i18n import t
from .legacy import fbx_converter, jpg_converter, texnames

_cwd_lock = threading.Lock()

_ANSI_RE = re.compile(r"\x1b\[(\d+)m")
_ANSI_TAGS = {"91": "err", "93": "warn", "92": "ok"}

IMAGE_IN = {
    "TGA": (".tga",), "PNG": (".png",), "JPG": (".jpg", ".jpeg"), "BMP": (".bmp",),
    "VTF": (".vtf",), "DDS": (".dds",), "PSD": (".psd",), "WEBP": (".webp",), "TIFF": (".tif", ".tiff"),
    "EXR": (".exr",),
}
IMAGE_OUT = ("PNG", "JPG", "TGA", "BMP", "WEBP", "TIFF", "VTF")
_OUT_EXT = {"PNG": ".png", "JPG": ".jpg", "TGA": ".tga", "BMP": ".bmp", "WEBP": ".webp", "TIFF": ".tif", "VTF": ".vtf"}


class LogWriter:
    """Sends print() output to the log and turns ANSI color codes into tags."""

    def __init__(self, log):
        self.log = log
        self.buf = ""

    def write(self, s):
        if not s:
            return 0
        self.buf += s.replace("\r\n", "\n").replace("\r", "\n")
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self._emit(line)
        return len(s)

    def _emit(self, line):
        tag = "info"
        m = _ANSI_RE.search(line)
        if m:
            tag = _ANSI_TAGS.get(m.group(1), "info")
        clean = _ANSI_RE.sub("", line).rstrip()
        if clean.strip():
            low = clean.lower().strip()
            if tag == "info":
                if low.startswith(("done", "[ok]", "ok ", "ok:")):
                    tag = "ok"
                elif "error" in low or "fail" in low:
                    tag = "err"
                elif "warn" in low or "not found" in low or "missing" in low:
                    tag = "warn"
            self.log(clean, tag)

    def flush(self):
        if self.buf:
            self._emit(self.buf)
            self.buf = ""


@contextlib.contextmanager
def working_directory(path):
    with _cwd_lock:
        old = os.getcwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(old)


def run_captured(fn, log):
    w = LogWriter(log)
    with contextlib.redirect_stdout(w), contextlib.redirect_stderr(w):
        try:
            return fn()
        finally:
            w.flush()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _inputs(inp, exts, opts):
    """The files a tool works on: the ones picked in the list."""
    return sorted(opts.get("files") or (), key=lambda f: f.lower())


def _report(status, key, st, detail=""):
    if status is not None:
        status(key, st, detail)


def file_key(path):
    return os.path.normcase(os.path.abspath(path))


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# ---------------------------------------------------------------------------
# Adding files: folders are searched, archives (.zip, .rar, .7z) are unpacked into the work folder
# ---------------------------------------------------------------------------

ARCHIVE_EXTS = (".zip", ".rar", ".7z")
ARCHIVE_PATTERN = " ".join("*" + e for e in ARCHIVE_EXTS)


def _unpack_other(path, dest):
    """.rar and .7z: Windows' own tar (libarchive) unpacks them; 7-Zip or WinRAR when it cannot."""
    import subprocess
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    tries = [[os.path.join(windir, "System32", "tar.exe"), "-xf", path, "-C", dest]]
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        tries.append([os.path.join(base, "7-Zip", "7z.exe"), "x", "-y", "-o" + dest, path])
        tries.append([os.path.join(base, "WinRAR", "UnRAR.exe"), "x", "-y", "-idq", path, dest + os.sep])
    err = None
    for cmd in tries:
        if not os.path.isfile(cmd[0]):
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, creationflags=flags, timeout=3600)
        except (OSError, subprocess.SubprocessError) as e:
            err = e
            continue
        if r.returncode == 0:
            return
        lines = (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        err = lines[-1] if lines else f"exit code {r.returncode}"
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest, exist_ok=True)
    raise RuntimeError(err if err is not None else "no program to unpack the archive")


def _zip_dir(path):
    """Folder the archive is unpacked into (once; again when the archive changes)."""
    from . import config
    st = os.stat(path)
    key = f"{file_key(path)}|{st.st_size}|{int(st.st_mtime)}"
    stem = re.sub(r"[^\w\-]", "_", os.path.splitext(os.path.basename(path))[0])[:40]
    d = os.path.join(config.work_root(), "_zips", f"{stem}_{hashlib.md5(key.encode()).hexdigest()[:8]}")
    if not os.path.isdir(d):
        part = d + ".part"
        shutil.rmtree(part, ignore_errors=True)
        os.makedirs(part, exist_ok=True)
        try:
            if path.lower().endswith(".zip"):
                with zipfile.ZipFile(path) as zf:
                    zf.extractall(part)
            else:
                _unpack_other(path, part)
        except BaseException:
            shutil.rmtree(part, ignore_errors=True)
            raise
        os.replace(part, d)
    return d


def expand_paths(paths, exts, log=None):
    """Files with these extensions in the given paths: folders with their subfolders, and the
    archives (also the ones in a folder or in another archive). Returns [(file, shown path)]; the
    shown path of a file from an archive is the archive's path followed by the file's path in it."""
    exts = tuple(e.lower() for e in exts)
    out, seen = [], set()

    def add(f, shown):
        k = file_key(f)
        if k not in seen:
            seen.add(k)
            out.append((f, shown))

    def walk(d, shown_root):
        for dp, dn, fn in os.walk(d):
            dn.sort(key=str.lower)
            for name in sorted(fn, key=str.lower):
                p = os.path.join(dp, name)
                shown = os.path.join(shown_root, os.path.relpath(p, d))
                if name.lower().endswith(exts):
                    add(p, shown)
                elif name.lower().endswith(ARCHIVE_EXTS):
                    unzip(p, shown)

    def unzip(z, shown):
        try:
            walk(_zip_dir(z), shown)
        except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as e:
            if log:
                log(t("tl_zip_fail", f=os.path.basename(z), e=e), "warn")

    for p in paths:
        p = os.path.normpath(p)
        if os.path.isdir(p):
            walk(p, p)
        elif p.lower().endswith(ARCHIVE_EXTS) and os.path.isfile(p):
            unzip(p, p)
        elif p.lower().endswith(exts) and os.path.isfile(p):
            add(p, p)
    return out


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _game_root(inp, top):
    """Folder that holds top/ (materials or models) when the input is in one or has one."""
    p = os.path.abspath(inp)
    cur = p
    while True:
        if os.path.basename(cur).lower() == top:
            return os.path.dirname(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return p if os.path.isdir(os.path.join(p, top)) else None


def _tool_sources(cfg, root, inp, log):
    """The input folder first (a game-like folder, or loose files found by name), then the
    resource folders and the installed games for what the files use from them."""
    from . import config
    from .sources import AssetSources
    cfg = cfg or {}
    games = config.find_s1_gamedirs(("csgo",))
    res = config.resource_entries(cfg.get("resource_dirs"))
    if root:
        return AssetSources(None, res, games, log=log, extra_dirs=[root])
    src = AssetSources(None, res, games, log=log, loose_dirs=[inp])
    src.sources.sort(key=lambda s: s.role != "loose")
    return src


def _game_names(files, inp, root, top):
    """Game paths (materials/x/y.vmt -> x/y for materials, models/x.mdl for models)."""
    base = os.path.join(root, top) if root else inp
    out = []
    for f in files:
        rel = os.path.relpath(f, base).replace("\\", "/")
        out.append((f, rel))
    return out


def _status(s):
    from .pipeline import status_text
    return status_text(s)


def _count_line(log, counts):
    parts = ", ".join(f"{_status(k)} {n}" for k, n in sorted(counts.items()))
    log(t("tl_convert_done", parts=parts), "ok" if not counts.get("error") and not counts.get("missing") else "warn")


def run_vmt_vmat(inp, out, opts, log, cfg=None, cancel=None, status=None, result=None):
    """VMT files (with their VTF textures) -> .vmat, the same way as in a map port: the VMT
    is read, its textures are found (in the folder, the resource folders or the games) and
    turned into TGA files, and the material is written with the matching CS2 shader."""
    from . import config
    from .materials import MaterialConverter
    from .sources import CS2Index
    root = _game_root(inp, "materials")
    vmts = _inputs(inp, (".vmt",), opts)
    log(t("tl_found", n=len(vmts)), "info")
    if not vmts:
        return
    content = out or root or inp
    sources = _tool_sources(cfg, root, inp, log)
    try:
        conv = MaterialConverter(sources, CS2Index("", content), content, config.VTFCMD, log,
                                 overwrite=opts.get("overwrite", False), convert_cs2_existing=True)
        counts = {}
        for f, rel in _game_names(vmts, inp, root, "materials"):
            if cancel is not None and cancel.is_set():
                break
            r = conv.convert(rel[:-4])
            counts[r.status] = counts.get(r.status, 0) + 1
            _report(status, f, r.status, r.detail)
            if result is not None:
                result("material", r.mat, r.status, r.detail, r.source)
            tag = {"created": "ok", "missing": "warn", "error": "err", "unsupported": "warn"}.get(r.status, "dim")
            log(f"  [{_status(r.status)}] {rel[:-4]}  {r.detail}", tag)
        _count_line(log, counts)
    finally:
        sources.close()


def run_mdl_vmdl(inp, out, opts, log, cfg=None, cancel=None, status=None, result=None):
    """Compiled models (.mdl + .vvd + .vtx + .phy) -> decompiled to QC / SMD, then .vmdl, with
    the model's materials (the same way as in a map port)."""
    import tempfile
    from . import config
    from .materials import MaterialConverter
    from .models import ModelConverter, QCIndex
    from .sources import CS2Index
    root = _game_root(inp, "models")
    mdls = _inputs(inp, (".mdl",), opts)
    log(t("tl_found", n=len(mdls)), "info")
    if not mdls:
        return
    content = out or root or inp
    sources = _tool_sources(cfg, root, inp, log)
    tmp = tempfile.mkdtemp(prefix="cs2porter_")
    try:
        index = CS2Index("", content)
        matconv = MaterialConverter(sources, index, content, config.VTFCMD, log,
                                    overwrite=opts.get("overwrite", False), convert_cs2_existing=True)
        modelconv = ModelConverter(sources, index, matconv, content, QCIndex([], tmp, log), valve=None,
                                   staging_dir=os.path.join(tmp, "staging"), log=log,
                                   overwrite=opts.get("overwrite", False), convert_cs2_existing=True)
        counts, mats = {}, {}
        for f, rel in _game_names(mdls, inp, root, "models"):
            if cancel is not None and cancel.is_set():
                break
            r = modelconv.convert("models/" + rel)
            counts[r.status] = counts.get(r.status, 0) + 1
            _report(status, f, r.status, r.detail)
            if result is not None:
                result("model", r.mdl, r.status, r.detail, r.source)
            tag = {"created": "ok", "missing": "warn", "error": "err"}.get(r.status, "dim")
            log(f"  [{_status(r.status)}] {rel}  {r.detail}", tag)
            for m in r.materials:
                mats.setdefault(m.lower(), True)
        made = 0
        for m in sorted(mats):
            if m.startswith("tools/"):
                continue
            r = matconv.convert(m, ("model",))
            made += r.status == "created"
            if result is not None:
                result("material", r.mat, r.status, r.detail, r.source)
            if r.status in ("missing", "error"):
                log(f"    [{_status(r.status)}] {m}  {r.detail}", "warn")
        if mats:
            log(t("tl_model_mats", n=made, total=len(mats)), "info")
        modelconv.fix_mesh_materials()
        _count_line(log, counts)
    finally:
        sources.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _flat_target(f, out, ext):
    """Output file of a list item: its name (with the new extension) in the output folder."""
    return os.path.join(out, os.path.splitext(os.path.basename(f))[0] + ext)


def run_trans(items, out, opts, log, cfg=None, cancel=None, status=None):
    overwrite = opts.get("overwrite", False)
    ok = skip = fail = 0
    for it in items:
        if cancel is not None and cancel.is_set():
            break
        f = it["files"][0]
        base, ext = os.path.splitext(os.path.basename(f))
        dst = os.path.join(out, base + "_transculent" + ext)
        if os.path.isfile(dst) and not overwrite:
            skip += 1
            _report(status, it["id"], "exists", t("m_in_folder"))
            continue
        try:
            img = Image.open(f)
            alpha = None
            if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
                alpha = np.asarray(img.convert("RGBA").getchannel("A"))
            if alpha is None or alpha.min() == 255:
                skip += 1
                log(t("tl_mask_skip", f=it["name"]), "dim")
                _report(status, it["id"], "skip", t("tl_no_alpha"))
                continue
            mask = Image.fromarray(np.stack([alpha] * 3, axis=2), "RGB")
            os.makedirs(out, exist_ok=True)
            mask.save(dst)
            ok += 1
            log(f"  OK  {os.path.basename(dst)}", "info")
            _report(status, it["id"], "created", os.path.basename(dst))
        except Exception as e:  # noqa: BLE001
            fail += 1
            log(t("tl_error", f=it["name"], e=e), "err")
            _report(status, it["id"], "error", str(e))
    log(t("tl_result", ok=ok, skip=skip, fail=fail), "ok" if not fail else "warn")


def _open_image(path):
    if path.lower().endswith(".vtf"):
        with open(path, "rb") as f:
            from . import config
            return vtfmod.image_from_bytes(f.read(), ".vtf", config.VTFCMD)
    return texnames.open_image(path)


def _save_image(img, path, fmt):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if fmt == "VTF":
        vtfmod.save_vtf(img, path)
        return
    if img.mode in ("P", "PA", "LA", "I;16", "I", "F", "CMYK", "YCbCr", "L") and fmt != "TGA":
        img = img.convert("RGBA" if "A" in img.mode or "transparency" in img.info else "RGB")
    if fmt == "JPG":
        img.convert("RGB").save(path, "JPEG", quality=95)
    elif fmt == "PNG":
        img.save(path, "PNG")
    elif fmt == "TGA":
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGBA" if "A" in img.mode else "RGB")
        img.save(path, "TGA")
    elif fmt == "BMP":
        img.convert("RGBA" if img.mode == "RGBA" else "RGB").save(path, "BMP")
    elif fmt == "WEBP":
        img.save(path, "WEBP", quality=95)
    elif fmt == "TIFF":
        img.save(path, "TIFF", compression="tiff_lzw")


def run_image(items, out, opts, log, cfg=None, cancel=None, status=None):
    overwrite = opts.get("overwrite", False)
    dst = opts.get("to", "PNG")
    new_ext = _OUT_EXT[dst]
    ok = skip = fail = 0
    for it in items:
        if cancel is not None and cancel.is_set():
            break
        f = it["files"][0]
        if f.lower().endswith(IMAGE_IN.get(dst, (new_ext,))):
            skip += 1
            _report(status, it["id"], "skip", t("tl_same_format"))
            continue
        target = _flat_target(f, out, new_ext)
        if os.path.isfile(target) and not overwrite:
            skip += 1
            _report(status, it["id"], "exists", t("m_in_folder"))
            continue
        try:
            _save_image(_open_image(f), target, dst)
            ok += 1
            log(f"  OK  {os.path.basename(target)}", "info")
            _report(status, it["id"], "created", os.path.basename(target))
        except Exception as e:  # noqa: BLE001
            fail += 1
            log(t("tl_error", f=it["name"], e=e), "err")
            _report(status, it["id"], "error", str(e))
    log(t("tl_result", ok=ok, skip=skip, fail=fail), "ok" if not fail else "warn")


_SKY_FACE_RE = re.compile(r"^(.+?)_?(up|dn|lf|rt|ft|bk)$", re.I)


def run_skybox(items, out, opts, log, cfg=None, cancel=None, status=None):
    """Six skybox faces (<name>up / dn / lf / rt / ft / bk, any image format) -> a CS2 cube
    cross (skybox.png) with skybox.vmat and skybox_moondome.vmat, one folder per sky."""
    from . import skybox as skymod
    ok = skip = fail = 0
    for it in items:
        if cancel is not None and cancel.is_set():
            break
        name, faces = it["name"], it["faces"]
        dst = os.path.join(out, name)
        png = os.path.join(dst, "skybox.png")
        if os.path.isfile(png) and not opts.get("overwrite", False):
            skip += 1
            _report(status, it["id"], "exists", t("m_in_folder"))
            continue
        try:
            imgs = {face: _open_image(p).convert("RGB") for face, p in faces.items()}
            ring, up_rot, dn_rot = skymod.arrange(imgs)
            os.makedirs(dst, exist_ok=True)
            skymod.compose(imgs, ring, up_rot, dn_rot).save(png, "PNG")
            with open(os.path.join(dst, "skybox.vmat"), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(skymod.SKY_TEMPLATE % {"png": skymod.SKY_PNG})
            with open(os.path.join(dst, "skybox_moondome.vmat"), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(skymod.MOONDOME_TEMPLATE % {"png": skymod.SKY_PNG})
            ok += 1
            fit = t("sk_fit", order=" ".join(ring), up=up_rot * 90, dn=dn_rot * 90).strip()
            log(f"  OK  {name}  {fit}", "info")
            _report(status, it["id"], "created", name)
        except Exception as e:  # noqa: BLE001
            fail += 1
            log(t("tl_error", f=name, e=e), "err")
            _report(status, it["id"], "error", str(e))
    log(t("tl_result", ok=ok, skip=skip, fail=fail), "ok" if not fail else "warn")


def _fbx_runner(kind):
    def run(items, out, opts, log, cfg=None, cancel=None, status=None):
        """Polyhaven / Megascans models (FBX with their textures) -> .vmdl and .vmat. Every row
        has its own collision: none, a convex hull or the exact shape of the model."""
        def job():
            ok = fail = 0
            print(f"  Models to process: {len(items)}")
            for it in items:
                if cancel is not None and cancel.is_set():
                    break
                on = it.get("on", {})
                asset = kind + ("_basic" if on.get("convex", True) else "_complex") if on.get("collision") else kind
                try:
                    done = fbx_converter.process_group(it["key"], it["files"], out, "", None, out, asset_type=asset)
                except Exception as e:  # noqa: BLE001
                    print(f"  ERROR   {it['name']}: {e}")
                    done = False
                ok += bool(done)
                fail += not done
                _report(status, it["id"], "created" if done else "error")
            print(f"\nDONE  OK: {ok}   Failed: {fail}")
        run_captured(job, log)
    return run


def run_textures(items, out, opts, log, cfg=None, cancel=None, status=None):
    """Polyhaven / Megascans textures (color, normal, AO ... of one surface) -> one .vmat each,
    as a texture or as a decal."""
    def job():
        ok = fail = 0
        for it in items:
            if cancel is not None and cancel.is_set():
                break
            category = "decals" if it.get("on", {}).get("decal") else "textures"
            try:
                done = jpg_converter.process_group(it["name"], it["files"], category, out)
            except Exception as e:  # noqa: BLE001
                print(f"  ERROR   {it['name']}: {e}")
                done = False
            ok += bool(done)
            fail += not done
            _report(status, it["id"], "created" if done else "error")
        print(f"\nDONE  OK: {ok}   Failed: {fail}")
    run_captured(job, log)


# ---------------------------------------------------------------------------
# List rows: what the user adds becomes the rows a tool converts
# ---------------------------------------------------------------------------
# a row: {"id", "name", "path" (shown), "files" (real paths), "detail", "on" (the row's switches),
# "children" (the files the row also uses: [{"name", "detail", "path"}], shown below it)

def _child(path, detail="", name=None):
    return {"name": name or os.path.basename(path), "detail": detail, "path": path}


_VMT_KEY_RE = re.compile(r'^\s*"?(\$?[\w%]+)"?\s+"?([^"\r\n{}]+?)"?\s*(?://.*)?$')
_VMT_TEX_ENDS = ("texture", "texture2", "map", "map2", "mask", "mask2", "detail", "detail2", "iris")


def _vmt_children(f):
    """The textures (and the material it includes) a .vmt names."""
    try:
        with open(f, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    root = _game_root(os.path.dirname(f), "materials")
    out, seen = [], set()
    for line in lines:
        m = _VMT_KEY_RE.match(line)
        if not m:
            continue
        key, val = m.group(1).lower(), m.group(2).strip().replace("\\", "/")
        if key == "include":
            ext, rel = "", val[len("materials/"):] if val.lower().startswith("materials/") else val
        elif key.startswith("$") and key.endswith(_VMT_TEX_ENDS) and key != "$envmap":
            ext, rel = ".vtf", val
        else:
            continue
        low = rel.lower()
        if not rel or low.startswith(("_rt_", "env_cubemap", "[")) or re.fullmatch(r"[-\d. ]+", rel) or low in seen:
            continue
        seen.add(low)
        name = rel if low.endswith((".vtf", ".vmt")) else rel + ext
        cands = [os.path.join(root, "materials", name)] if root else []
        cands.append(os.path.join(os.path.dirname(f), os.path.basename(name)))
        path = next((p for p in cands if os.path.isfile(p)), "")
        out.append({"name": name, "detail": key.lstrip("$"), "path": path})
    return out


_MDL_PARTS = (".vvd", ".dx90.vtx", ".dx80.vtx", ".sw.vtx", ".vtx", ".phy", ".ani")


def _mdl_children(f):
    """The files that come with a .mdl (.vvd, .vtx, .phy) and the materials it uses."""
    stem = f[:-4]
    out = [_child(stem + ext) for ext in _MDL_PARTS if os.path.isfile(stem + ext)]
    try:
        with open(f, "rb") as fh:
            m = mdlmod.MDL(fh.read())
    except Exception:  # noqa: BLE001
        return out
    root = _game_root(os.path.dirname(f), "models")
    seen = set()
    for tex in m.textures:
        cds = [cd.replace("\\", "/").strip("/") for cd in m.cdmaterials] or [""]
        names = [(cd + "/" if cd else "") + tex.replace("\\", "/") + ".vmt" for cd in cds]
        path = ""
        if root:
            path = next((p for p in (os.path.join(root, "materials", n) for n in names) if os.path.isfile(p)), "")
        name = names[0].lower() if not path else os.path.relpath(path, os.path.join(root, "materials")).replace("\\", "/")
        if name.lower() not in seen:
            seen.add(name.lower())
            out.append({"name": name, "detail": t("fl_child_material"), "path": path})
    return out


def _file_rows(pairs, keep=None, children=None):
    rows = []
    for f, shown in pairs:
        if keep is not None and not keep(f):
            continue
        rows.append({"id": file_key(f), "name": os.path.basename(f), "path": shown, "files": [f], "detail": "",
                     "children": children(f) if children else []})
    return rows


_SKY_ORDER = ("up", "dn", "lf", "rt", "ft", "bk")


def _sky_rows(pairs):
    skies = {}
    for f, shown in pairs:
        m = _SKY_FACE_RE.match(os.path.splitext(os.path.basename(f))[0])
        if m:
            key = (os.path.dirname(f), m.group(1).rstrip("_"))
            sky = skies.setdefault(key, {"shown": os.path.dirname(shown), "faces": {}})
            sky["faces"].setdefault(m.group(2).lower(), f)
    rows = []
    from . import skybox as skymod
    for (folder, name), sky in sorted(skies.items()):
        faces = sky["faces"]
        if sum(1 for s in skymod.SIDES if s in faces) < 4:
            continue
        rows.append({"id": file_key(os.path.join(folder, name)), "name": name, "path": sky["shown"],
                     "files": list(faces.values()), "faces": faces,
                     "detail": " ".join(s for s in _SKY_ORDER if s in faces),
                     "children": [_child(faces[s], s) for s in _SKY_ORDER if s in faces]})
    return rows


_SLOT_NAMES = {"color": "Color", "normal": "Normal", "ao": "AO", "translucency": "Opacity",
               "roughness": "Roughness"}
_FBX_SLOTS = {"TextureLayer1Color": "color", "TextureLayer1Normal": "normal", "TextureLayer1Roughness": "roughness",
              "TextureLayer1AmbientOcclusion": "ao", "TextureLayer1Translucency": "translucency"}


def _fbx_textures(fbx):
    """The textures a model gets (the same folder, or its textures/ folder)."""
    folder = os.path.dirname(fbx)
    texs = fbx_converter.find_textures_in_dir(folder)
    if not texs and os.path.isdir(os.path.join(folder, "textures")):
        texs = fbx_converter.find_textures_in_dir(os.path.join(folder, "textures"))
    used = fbx_converter.select_textures_for_slots(texs) if texs else {}
    return [_child(p, _SLOT_NAMES.get(_FBX_SLOTS.get(slot, ""), "")) for slot, p in
            sorted(used.items(), key=lambda kv: list(_FBX_SLOTS).index(kv[0]) if kv[0] in _FBX_SLOTS else 9)]


def _fbx_rows(pairs, defaults):
    shown_of = {f: s for f, s in pairs}
    rows = []
    for key, files in sorted(fbx_converter.group_fbx_files([f for f, _s in pairs]).items()):
        name = os.path.basename(key)
        with contextlib.redirect_stdout(None):
            texs = _fbx_textures(files[0])
        kids = [_child(f, t("fl_child_model") + (f" (LOD {i})" if len(files) > 1 else ""))
                for i, f in enumerate(files)] + texs
        rows.append({"id": file_key(key), "key": key, "name": name,
                     "path": os.path.dirname(shown_of[files[0]]), "files": files,
                     "detail": t("tl_lods", n=len(files)) if len(files) > 1 else "", "on": dict(defaults),
                     "children": kids})
    return rows


def _texture_rows(pairs):
    by_dir = {}
    for f, shown in pairs:
        if jpg_converter.classify_texture(f):
            by_dir.setdefault(os.path.dirname(f), []).append((f, shown))
    rows = []
    for folder, group in sorted(by_dir.items()):
        shown_of = dict(group)
        for gname, files in sorted(jpg_converter.group_textures([f for f, _s in group]).items()):
            safe = re.sub(r"[^\w\-]", "_", gname).lower()
            # roughness maps are not used by the material
            slots = texnames.pick_slots(files, ("color", "normal", "ao", "translucency"))
            if "color" not in slots and "normal" not in slots:
                continue
            decal = "decal" in folder.lower().replace("\\", "/").split("/")[-1]
            order = [s for s in _SLOT_NAMES if s in slots]
            rows.append({"id": file_key(os.path.join(folder, safe)), "name": safe,
                         "path": os.path.dirname(shown_of[files[0]]), "files": files,
                         "detail": ", ".join(_SLOT_NAMES[s] for s in order),
                         "on": {"decal": decal},
                         "children": [_child(slots[s], _SLOT_NAMES[s]) for s in order]})
    return rows


_IMAGE_EXTS = sum(IMAGE_IN.values(), ())


def items(td, paths, log=None):
    """Rows of a tool's list for the dropped / picked paths."""
    pairs = expand_paths(paths, td["exts"], log)
    kind = td.get("rows", "files")
    if kind == "sky":
        return _sky_rows(pairs)
    if kind == "fbx":
        return _fbx_rows(pairs, {k: d for k, _label, d in td.get("switches", ())})
    if kind == "textures":
        return _texture_rows(pairs)
    return _file_rows(pairs, td.get("keep"), td.get("children"))


def run_tool(td, rows, out, opts, log, cfg=None, cancel=None, status=None):
    """Converts the rows into the output folder. The converters that work like a map port take
    the rows' files folder by folder: a file under a materials (models) folder keeps its game path."""
    os.makedirs(out, exist_ok=True)
    top = td.get("game_root")
    if not top:
        return td["run"](rows, out, opts, log, cfg=cfg, cancel=cancel, status=status)
    groups = {}
    for it in rows:
        f = it["files"][0]
        base = _game_root(os.path.dirname(f), top) or os.path.dirname(f)
        groups.setdefault(base, []).append(f)
    for base, files in groups.items():
        if cancel is not None and cancel.is_set():
            break
        td["run"](base, out, dict(opts, files=files), log, cfg=cfg, cancel=cancel, status=status)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
# exts: the files a tool takes; rows: how they are listed; options: overwrite, formats;
# switches: (key, label, default) per row; game_root: converted folder by folder;
# children: the files a row also uses; add: "folder" when only folders are added

TOOLS = [
    {"id": "vmt_vmat", "group": "port", "name": "t_vmt_name", "desc": "t_vmt_desc", "exts": (".vmt",),
     "game_root": "materials", "children": _vmt_children, "options": {"overwrite": False}, "run": run_vmt_vmat},
    {"id": "mdl_vmdl", "group": "port", "name": "t_mdl_name", "desc": "t_mdl_desc", "exts": (".mdl",),
     "game_root": "models", "children": _mdl_children, "options": {"overwrite": False}, "run": run_mdl_vmdl},
    {"id": "image", "group": "port", "name": "t_image_name", "desc": "t_image_desc", "exts": _IMAGE_EXTS,
     "options": {"overwrite": False, "formats": True}, "run": run_image},
    {"id": "trans", "group": "port", "name": "t_trans_name", "desc": "t_trans_desc", "exts": (".png", ".tga"),
     "keep": lambda f: not os.path.splitext(f)[0].lower().endswith("_transculent"),
     "options": {"overwrite": False}, "run": run_trans},
    {"id": "skybox", "group": "port", "name": "t_sky_name", "desc": "t_sky_desc", "exts": _IMAGE_EXTS,
     "rows": "sky", "add": "folder", "options": {"overwrite": False}, "run": run_skybox},
    {"id": "fbx", "group": "assets", "name": "t_fbx_name", "desc": "t_fbx_desc", "exts": (".fbx",),
     "rows": "fbx", "options": {}, "run": _fbx_runner("static"),
     "switches": (("collision", "sw_collision", True), ("convex", "sw_convex", True)),
     "requires": {"convex": "collision"}},
    {"id": "foliage", "group": "assets", "name": "t_foliage_name", "desc": "t_foliage_desc", "exts": (".fbx",),
     "rows": "fbx", "options": {}, "run": _fbx_runner("foliage"),
     "switches": (("collision", "sw_collision", False), ("convex", "sw_convex", True)),
     "requires": {"convex": "collision"}},
    {"id": "textures", "group": "assets", "name": "t_tex_name", "desc": "t_tex_desc",
     "exts": tuple(sorted(jpg_converter.TEXTURE_EXT)),
     "rows": "textures", "options": {}, "run": run_textures, "switches": (("decal", "sw_decal", False),)},
]


def tool_by_id(tid):
    return next((x for x in TOOLS if x["id"] == tid), TOOLS[0])
