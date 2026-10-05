"""'Tools' tab: tehlikeli91's scripts plus some extra helper tools.

Every tool takes (input, output, options). An empty output means the input folder.
"""

import contextlib
import os
import re
import shutil
import threading

import numpy as np
from PIL import Image

from . import mdl as mdlmod
from . import vtf as vtfmod
from .i18n import t
from .legacy import fbx_converter, jpg_converter, reset_folders
from .legacy import vmat_converter as VC
from .legacy import vmdl_converter as VD

_cwd_lock = threading.Lock()

_ANSI_RE = re.compile(r"\x1b\[(\d+)m")
_ANSI_TAGS = {"91": "err", "93": "warn", "92": "ok"}

IMAGE_IN = {
    "TGA": (".tga",), "PNG": (".png",), "JPG": (".jpg", ".jpeg"), "BMP": (".bmp",),
    "VTF": (".vtf",), "DDS": (".dds",), "PSD": (".psd",), "WEBP": (".webp",), "TIFF": (".tif", ".tiff"),
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

def list_files(folder, exts, recursive, skip_dirs=("_temp",)):
    exts = tuple(e.lower() for e in exts)
    out = []
    if recursive:
        for dp, dn, fn in os.walk(folder):
            dn[:] = sorted(d for d in dn if not d.startswith(skip_dirs))
            for f in sorted(fn):
                if f.lower().endswith(exts):
                    out.append(os.path.join(dp, f))
    else:
        for f in sorted(os.listdir(folder)):
            p = os.path.join(folder, f)
            if os.path.isfile(p) and f.lower().endswith(exts):
                out.append(p)
    return out


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _mirror(path, inp, out):
    """Where a file from the input folder goes in the output folder."""
    return os.path.join(out, os.path.relpath(path, inp))


def _copy_to_output(files, inp, out, log):
    if not files:
        return
    log(t("tl_copying", n=len(files)), "dim")
    for f in files:
        dst = _mirror(f, inp, out)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.isfile(dst) or os.path.getmtime(dst) < os.path.getmtime(f):
            shutil.copy2(f, dst)


def _content_root(folder):
    """If the folder is inside a 'materials' folder, returns its parent, otherwise the folder itself."""
    p = os.path.abspath(folder)
    cur = p
    while True:
        if os.path.basename(cur).lower() == "materials":
            return os.path.dirname(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            return p
        cur = parent


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

_TEX_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".tga")


def run_vmat(inp, out, opts, log):
    rec = opts.get("recursive", True)
    work = inp
    if out and not _same(out, inp):
        files = list_files(inp, _TEX_EXTS, rec)
        for extra in ("texture_info.txt", os.path.join("Missing Textures", "texture_info.txt")):
            p = os.path.join(inp, extra)
            if os.path.isfile(p):
                files.append(p)
        _copy_to_output(files, inp, out, log)
        work = out
    base = _content_root(work)

    def job():
        VC.set_base_dir(base)
        stats = {"ok": 0}
        txt = None
        for cand in (os.path.join(base, "Missing Textures", "texture_info.txt"),
                     os.path.join(base, "texture_info.txt"),
                     os.path.join(work, "texture_info.txt")):
            if os.path.isfile(cand):
                txt = cand
                break
        skip = set()
        if txt:
            skip = VC.convert_from_txt(VC.parse_texture_info(txt), stats)
        files = list_files(work, _TEX_EXTS, rec)
        print(f"  Found {len(files)} texture file(s) to scan")
        for f in files:
            VC.convert_file(os.path.dirname(f), os.path.basename(f), stats, skip)
        tmp = os.path.join(base, "_temp_fixed_vmat")
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        print(f"\nDONE  VMAT files created: {stats['ok']}")

    run_captured(job, log)


def run_vmdl(inp, out, opts, log):
    rec = opts.get("recursive", True)
    work = inp
    if out and not _same(out, inp):
        files = list_files(inp, (".qc", ".smd", ".vta", ".vrd", ".vmat"), rec)
        _copy_to_output(files, inp, out, log)
        work = out
    qcs = list_files(work, (".qc",), rec)
    if opts.get("fix_rotation", True):
        _fix_qc_rotation(qcs, log)

    def job():
        print(f"Root Folder: {work}")
        vmat_index = VD.build_vmat_index(work)
        print(f"Found .qc file: {len(qcs)}")
        collector = {}
        created = skipped = 0
        for qc in qcs:
            if VD.process_qc_file(qc, work, vmat_index, collector):
                created += 1
            else:
                skipped += 1
        if collector:
            VD.write_vmat_ref_file(work, collector)
        print(f"\nDONE. Created: {created} | Skipped: {skipped}")

    run_captured(job, log)


def _fix_qc_rotation(qcs, log):
    """Non-static models are written in compiled space; the SMDs are rotated for CS2."""
    for qc in qcs:
        try:
            with open(qc, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if re.search(r"^\s*\$staticprop\b", text, re.I | re.M):
            continue
        if "Decompiled by CS2 Porter" in text:
            continue            # models we decompiled ourselves already face the right way
        names = re.findall(r'studio\s+"([^"]+\.smd)"', text, re.I)
        names += re.findall(r'\$(?:body|model)\s+"[^"]*"\s+"([^"]+\.smd)"', text, re.I)
        names += re.findall(r'\$collision(?:model|joints)\s+"([^"]+\.smd)"', text, re.I)
        for n in dict.fromkeys(names):
            p = os.path.join(os.path.dirname(qc), n)
            if os.path.isfile(p):
                mdlmod.rotate_smd_file(p, p)


def run_mdl(inp, out, opts, log, result=None):
    rec = opts.get("recursive", True)
    files = list_files(inp, (".mdl",), rec)
    log(t("tl_found", n=len(files)), "info")
    ok = fail = 0
    for f in files:
        folder = os.path.dirname(f)
        dest = _mirror(folder, inp, out) if out else folder

        def read(rel, folder=folder):
            p = os.path.join(folder, os.path.basename(rel))
            if os.path.isfile(p):
                with open(p, "rb") as fh:
                    return fh.read()
            return None

        rel = os.path.basename(f)
        try:
            r = mdlmod.decompile(read, rel, dest)
            ok += 1
            log(t("tl_mdl_line", f=os.path.relpath(f, inp), tris=r.triangles,
                  phys=t("tl_phys") if r.physics else ""), "ok")
            if result is not None:
                result("model", os.path.relpath(f, inp), "created",
                       t("tl_mdl_detail", tris=r.triangles, phys=t("tl_phys") if r.physics else ""), f)
        except Exception as e:  # noqa: BLE001
            fail += 1
            log(t("tl_error", f=os.path.relpath(f, inp), e=e), "err")
            if result is not None:
                result("model", os.path.relpath(f, inp), "error", str(e), f)
    log(t("tl_result", ok=ok, skip=0, fail=fail), "ok" if not fail else "warn")


# ---------------------------------------------------------------------------
# Converter: one tool for every Source 1 -> CS2 conversion of a folder
# ---------------------------------------------------------------------------

CONVERT_MODES = ("vmt_vmat", "mdl_vmdl", "qc_vmdl", "mdl_qc", "tex_vmat")


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


def run_vmt_vmat(inp, out, opts, log, cfg=None, cancel=None, result=None):
    """VMT files (with their VTF textures) -> .vmat, the same way as in a map port: the VMT
    is read, its textures are found (in the folder, the resource folders or the games) and
    turned into TGA files, and the material is written with the matching CS2 shader."""
    from . import config
    from .materials import MaterialConverter
    from .sources import CS2Index
    root = _game_root(inp, "materials")
    vmts = list_files(inp, (".vmt",), opts.get("recursive", True))
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
            if result is not None:
                result("material", r.mat, r.status, r.detail, r.source)
            tag = {"created": "ok", "missing": "warn", "error": "err", "unsupported": "warn"}.get(r.status, "dim")
            log(f"  [{_status(r.status)}] {rel[:-4]}  {r.detail}", tag)
        _count_line(log, counts)
    finally:
        sources.close()


def run_mdl_vmdl(inp, out, opts, log, cfg=None, cancel=None, result=None):
    """Compiled models (.mdl + .vvd + .vtx + .phy) -> decompiled to QC / SMD, then .vmdl, with
    the model's materials (the same way as in a map port)."""
    import tempfile
    from . import config
    from .materials import MaterialConverter
    from .models import ModelConverter, QCIndex
    from .sources import CS2Index
    root = _game_root(inp, "models")
    mdls = list_files(inp, (".mdl",), opts.get("recursive", True))
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
        _count_line(log, counts)
    finally:
        sources.close()
        shutil.rmtree(tmp, ignore_errors=True)


def run_convert(inp, out, opts, log, cfg=None, cancel=None, result=None):
    """result: called with (kind, name, status, detail, source) for every converted file."""
    mode = opts.get("mode", "vmt_vmat")
    if mode == "vmt_vmat":
        return run_vmt_vmat(inp, out, opts, log, cfg, cancel, result)
    if mode == "mdl_vmdl":
        return run_mdl_vmdl(inp, out, opts, log, cfg, cancel, result)
    if mode == "qc_vmdl":
        return run_vmdl(inp, out, opts, log)
    if mode == "mdl_qc":
        return run_mdl(inp, out, opts, log, result)
    return run_vmat(inp, out, opts, log)


def run_trans(inp, out, opts, log):
    rec = opts.get("recursive", False)
    overwrite = opts.get("overwrite", False)
    files = [f for f in list_files(inp, (".png", ".tga"), rec)
             if not os.path.splitext(f)[0].lower().endswith("_transculent")]
    log(t("tl_found", n=len(files)), "info")
    ok = skip = fail = 0
    for f in files:
        base, ext = os.path.splitext(f)
        dst = (_mirror(base, inp, out) if out else base) + "_transculent" + ext
        if os.path.isfile(dst) and not overwrite:
            skip += 1
            continue
        try:
            img = Image.open(f)
            if img.mode not in ("RGBA", "LA", "PA") and "transparency" not in img.info:
                skip += 1
                log(t("tl_mask_skip", f=os.path.relpath(f, inp)), "dim")
                continue
            alpha = np.asarray(img.convert("RGBA").getchannel("A"))
            if alpha.min() == 255:
                skip += 1
                log(t("tl_mask_skip", f=os.path.relpath(f, inp)), "dim")
                continue
            mask = Image.fromarray(np.stack([alpha] * 3, axis=2), "RGB")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            mask.save(dst)
            ok += 1
            log(f"  OK  {os.path.relpath(dst, out or inp)}", "info")
        except Exception as e:  # noqa: BLE001
            fail += 1
            log(t("tl_error", f=os.path.relpath(f, inp), e=e), "err")
    log(t("tl_result", ok=ok, skip=skip, fail=fail), "ok" if not fail else "warn")


def _open_image(path):
    if path.lower().endswith(".vtf"):
        with open(path, "rb") as f:
            from . import config
            return vtfmod.image_from_bytes(f.read(), ".vtf", config.VTFCMD)
    img = Image.open(path)
    img.load()
    return img


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


def run_image(inp, out, opts, log):
    rec = opts.get("recursive", False)
    overwrite = opts.get("overwrite", False)
    src = opts.get("from", "TGA")
    dst = opts.get("to", "PNG")
    exts = sum(IMAGE_IN.values(), ()) if src == "ALL" else IMAGE_IN.get(src, (".tga",))
    new_ext = _OUT_EXT[dst]
    files = [f for f in list_files(inp, exts, rec) if not f.lower().endswith(IMAGE_IN.get(dst, (new_ext,)))]
    log(t("tl_found", n=len(files)), "info")
    ok = skip = fail = 0
    for f in files:
        target = os.path.splitext(_mirror(f, inp, out) if out else f)[0] + new_ext
        if os.path.isfile(target) and not overwrite:
            skip += 1
            continue
        try:
            _save_image(_open_image(f), target, dst)
            ok += 1
            log(f"  OK  {os.path.relpath(target, out or inp)}", "info")
        except Exception as e:  # noqa: BLE001
            fail += 1
            log(t("tl_error", f=os.path.relpath(f, inp), e=e), "err")
    log(t("tl_result", ok=ok, skip=skip, fail=fail), "ok" if not fail else "warn")


_SKY_FACE_RE = re.compile(r"^(.+?)_?(up|dn|lf|rt|ft|bk)$", re.I)


def run_skybox(inp, out, opts, log):
    """Six skybox faces (<name>up / dn / lf / rt / ft / bk, any image format) -> a CS2 cube
    cross (skybox.png) with skybox.vmat and skybox_moondome.vmat, one folder per sky."""
    from . import skybox as skymod
    files = list_files(inp, sum(IMAGE_IN.values(), ()), opts.get("recursive", False))
    skies = {}
    for f in files:
        m = _SKY_FACE_RE.match(os.path.splitext(os.path.basename(f))[0])
        if m:
            key = (os.path.dirname(f), m.group(1).rstrip("_"))
            skies.setdefault(key, {}).setdefault(m.group(2).lower(), f)
    skies = {k: v for k, v in skies.items() if sum(1 for s in skymod.SIDES if s in v) >= 4}
    log(t("tl_sky_found", n=len(skies)), "info")
    ok = skip = fail = 0
    for (folder, name), faces in sorted(skies.items()):
        dst = os.path.join(out or folder, name)
        png = os.path.join(dst, "skybox.png")
        if os.path.isfile(png) and not opts.get("overwrite", False):
            skip += 1
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
            log(f"  OK  {os.path.relpath(dst, out or inp)}  "
                + t("sk_fit", order=" ".join(ring), up=up_rot * 90, dn=dn_rot * 90).strip(), "info")
        except Exception as e:  # noqa: BLE001
            fail += 1
            log(t("tl_error", f=name, e=e), "err")
    log(t("tl_result", ok=ok, skip=skip, fail=fail), "ok" if not fail else "warn")


def run_fbx(inp, out, opts, log):
    run_captured(lambda: fbx_converter.main(inp, out or None), log)


def run_decal(inp, out, opts, log):
    run_captured(lambda: jpg_converter.main(inp, out or None), log)


def reset_fbx_folders(path, log):
    run_captured(lambda: reset_folders.clear_target_folders(reset_folders.TARGET_LIST, path), log)


def fbx_reset_targets(path):
    return [f for _p, f in reset_folders.find_existing(reset_folders.TARGET_LIST, path)]


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
# options: recursive (default), overwrite, formats, fix_rotation

TOOLS = [
    {"id": "convert", "group": "port", "name": "t_convert_name", "desc": "t_convert_desc",
     "options": {"recursive": True, "overwrite": False, "fix_rotation": True, "modes": CONVERT_MODES},
     "run": run_convert, "full": True},
    {"id": "trans", "group": "port", "name": "t_trans_name", "desc": "t_trans_desc",
     "options": {"recursive": False, "overwrite": False}, "run": run_trans},
    {"id": "image", "group": "port", "name": "t_image_name", "desc": "t_image_desc",
     "options": {"recursive": False, "overwrite": False, "formats": True}, "run": run_image},
    {"id": "skybox", "group": "port", "name": "t_sky_name", "desc": "t_sky_desc", "hint": "t_sky_hint",
     "options": {"recursive": False, "overwrite": False}, "run": run_skybox},
    {"id": "fbx", "group": "assets", "name": "t_fbx_name", "desc": "t_fbx_desc", "hint": "t_fbx_hint",
     "options": {}, "run": run_fbx, "reset": True},
    {"id": "decal", "group": "assets", "name": "t_decal_name", "desc": "t_decal_desc",
     "options": {}, "run": run_decal},
]


def tool_by_id(tid):
    return next((x for x in TOOLS if x["id"] == tid), TOOLS[0])
