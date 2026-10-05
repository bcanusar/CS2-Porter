"""Map porting and missing asset workflows."""

import collections
import os
import re
import shutil
import time

from . import __version__, bhop, i18n
from . import blend as blendmod
from . import bsp as bspmod
from . import config
from . import deps as depsmod
from . import fgd as fgdmod
from . import lighting as lightingmod
from . import particles as particlesmod
from . import postfx as postfxmod
from . import probes as probesmod
from . import s1stub
from . import sounds as soundsmod
from . import texlights as texlightsmod
from . import uvfix
from . import vmap as vmapmod
from . import vmf as vmfmod
from .i18n import t, t_en
from .materials import (BROKEN_CABLE_MATERIALS, MaterialConverter, out_name, write_black_material,
                        write_cable_fallback)
from .models import ModelConverter, QCIndex, norm_model
from .skybox import MOONDOME_VMAT, SKY_VMAT, build_skybox
from .sources import AssetSources, CS2Index, DirSource, VPKSource
from .vpk import VPKArchive, VPKError
from .valve_tools import Cancelled, ToolError, ValveTools, decompile_bsp

REPORT_FILE = "report.txt"


def status_text(st):
    return t("st_" + st)


def sanitize_name(name):
    n = re.sub(r"[^A-Za-z0-9_\-]", "_", name.strip())
    return n.lower() or "map"


class JobContext:
    """Bridge between the GUI and the worker thread."""

    def __init__(self, log, progress=None, result=None, ask=None, cancel=None, choose=None):
        self._raw_log = log
        self.progress = progress or (lambda f, text="": None)
        self._result = result or (lambda kind, name, status, detail, source: None)
        self.ask = ask or (lambda title, msg: False)
        # choose(title, text, rows) -> the rows the user ticked ([] none, None: cancel)
        self.choose = choose or (lambda title, msg, rows: [])
        self.cancel = cancel
        self._stage_t0 = None
        # everything the job did, for report.txt: (time, tag, text), (kind, name, status, ...)
        self.t0 = time.time()
        self.lines = []
        self.results = []
        self.runs = []              # commands the job ran (see begin)
        self.cfg = None             # the settings the job ran with
        self.pending_report = None
        self.english = {"exact": {}, "parts": {}}
        # only rows that changed something (or could not) go to the table: created, missing ...
        self.only_changes = False

    def begin(self):
        """Called in the worker thread before the job: the log is kept in English too, and the
        commands the job runs are noted for report.txt."""
        from . import valve_tools
        self.t0 = time.time()
        i18n.record_english(self.english)
        valve_tools.record_runs(self._record_run)

    def end(self):
        from . import valve_tools
        i18n.record_english(None)
        valve_tools.record_runs(None)

    def _record_run(self, args, code, sec, lines):
        # the whole output of a command that failed, the end of the others
        keep = lines if code != 0 else lines[-15:]
        self.runs.append((time.time() - sec, [str(a) for a in args], code, sec, len(lines), keep[-400:]))

    def result(self, kind, name, status, detail, source, group=""):
        """group: the last column of the table (the game a found file belongs to)."""
        self.results.append((kind, name, status, detail, source))
        if self.only_changes and status in ("exists", "cs2", "tool", "skip"):
            return
        if group:
            self._result(kind, name, status, detail, source, group)
        else:
            self._result(kind, name, status, detail, source)

    def log(self, msg, tag="info"):
        """Log line. Every new step ("head") also writes, as a detail line, how long the
        step before it took."""
        if tag == "head":
            now = time.time()
            if self._stage_t0 is not None:
                self._line(t("d_stage_time", sec=now - self._stage_t0), "tool")
            self._stage_t0 = now
        self._line(msg, tag)

    def detail(self, msg):
        """Line that is only shown with the verbose log."""
        self._line(msg, "tool")

    def _line(self, msg, tag):
        self.lines.append((time.time(), tag, msg))
        self._raw_log(msg, tag)

    def english_text(self, msg):
        """msg in English (see i18n.record_english); lines made of several texts get their
        known pieces replaced."""
        exact = self.english["exact"]
        if not exact:
            return msg
        # a line found whole can still hold translated values (a list of statuses ...)
        msg = exact.get(msg, msg)
        if not hasattr(self, "_parts"):
            # longer pieces first: whole sentences with their values, then single words
            pieces = dict(self.english["parts"])
            pieces.update(exact)
            self._parts = sorted(((k, v) for k, v in pieces.items() if len(k.strip()) >= 3 and k != v),
                                 key=lambda kv: -len(kv[0]))
        for k, v in self._parts:
            if k in msg:
                msg = msg.replace(k, v)
        return msg

    def check(self):
        from .valve_tools import Cancelled
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()


class Report:
    def __init__(self):
        self.materials = {}
        self.models = {}
        self.skybox = None
        self.created_files = []
        self.content_dir = ""
        self.work_dir = ""
        self.vmf_path = ""
        self.report_path = ""
        self.import_ok = None
        self.notices = []           # things the user must know when the job ends (shown in a box)
        self.input_path = ""
        self.extra_path = ""

    def counts(self, which):
        d = self.materials if which == "materials" else self.models
        out = {}
        for r in d.values():
            out[r.status] = out.get(r.status, 0) + 1
        return out

    def write(self, path, title, ctx=None, failure=None, trace=None):
        """report.txt: the whole job, step by step, always in English (see _report_lines)."""
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(_report_lines(self, title, ctx, failure, trace)) + "\n")
        self.report_path = path

    def asset_lines(self, ctx=None):
        lines = []
        groups = [(t_en("report_materials"), self.materials), (t_en("report_models"), self.models)]
        sounds = {}
        for kind, name, status, detail, source in (ctx.results if ctx is not None else ()):
            if kind == "sound":
                sounds[name] = _Row(status, ctx.english_text(detail), source)
        if sounds:
            groups.append((t_en("report_sounds"), sounds))
        for name, d in groups:
            if not d:
                continue
            lines.append(f"  {name} ({len(d)})")
            for st in ("missing", "error", "unsupported", "created", "exists", "cs2", "tool"):
                items = sorted((k, r) for k, r in d.items() if r.status == st)
                if not items:
                    continue
                lines.append(f"    [{t_en('st_' + st)}] {len(items)}")
                for k, r in items:
                    detail = ctx.english_text(r.detail) if ctx is not None else r.detail
                    source = getattr(r, "source", "")
                    if source and ctx is not None:
                        source = ctx.english_text(source)
                    extra = f"  <- {source}" if source else ""
                    lines.append(f"        {k}   {detail}{extra}".rstrip())
            lines.append("")
        if self.skybox:
            lines.append(f"  Skybox: {ctx.english_text(self.skybox) if ctx is not None else self.skybox}")
        return lines

    def write_missing_refs(self, folder):
        """missing_vmat_ref.txt / missing_vmdl_ref.txt in the same format as tehlikeli91's scripts."""
        mats = sorted(k for k, r in self.materials.items() if r.status in ("missing", "error"))
        mdls = sorted(k for k, r in self.models.items() if r.status in ("missing", "error"))
        with open(os.path.join(folder, "missing_vmat_ref.txt"), "w", encoding="utf-8") as f:
            for m in mats:
                f.write(f"{os.path.basename(m)}.vmat -> materials/{m}.vmat\n")
        with open(os.path.join(folder, "missing_vmdl_ref.txt"), "w", encoding="utf-8") as f:
            for m in mdls:
                stem = m[:-4]
                f.write(f"{os.path.basename(stem)}.vmdl -> {stem}.vmdl\n")


class _Row:
    def __init__(self, status, detail, source):
        self.status, self.detail, self.source = status, detail, source


_TAGS = {"head": "STEP ", "ok": "OK   ", "warn": "WARN ", "err": "ERROR", "dim": "INFO ", "tool": "DETL ",
         "info": "INFO "}


def _clock_text(sec):
    m, s = divmod(max(sec, 0.0), 60.0)
    return f"{int(m):02d}:{s:06.3f}"


def _dur_text(sec):
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(int(round(sec)), 60)
    return f"{m}m {s:02d}s" if m < 60 else f"{m // 60}h {m % 60:02d}m {s:02d}s"


def _size_text(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return str(n)


def _counts_text(items):
    return ", ".join(t_en("st_" + k).lower() + " " + str(v) for k, v in items)


def _system_lines(report, cfg):
    """Computer, program and folder facts that often explain a failed job."""
    import platform
    import sys
    out = [f"Program: CS2 Porter {__version__} ({'exe' if getattr(sys, 'frozen', False) else 'script'}, "
           f"Python {platform.python_version()})",
           f"Program folder: {config.APP_DIR}",
           f"Windows: {platform.platform()} ({platform.machine()})",
           f"Processors: {os.cpu_count()}"]
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            out.append(f"Memory: {_size_text(st.ullAvailPhys)} free of {_size_text(st.ullTotalPhys)}")
    except (AttributeError, OSError, ValueError):
        pass
    drives = {}
    for label, p in (("work folder", report.work_dir), ("target", report.content_dir),
                     ("CS2", (cfg or {}).get("cs2_dir", ""))):
        if not p:
            continue
        drive = os.path.splitdrive(os.path.abspath(p))[0] or p
        drives.setdefault(drive, []).append(label)
    for drive, labels in drives.items():
        try:
            du = shutil.disk_usage(drive + os.sep)
            out.append(f"Disk {drive} ({', '.join(labels)}): {_size_text(du.free)} free of {_size_text(du.total)}")
        except OSError as e:
            out.append(f"Disk {drive} ({', '.join(labels)}): cannot read ({e})")
    return out


def _settings_lines(cfg):
    if not cfg:
        return ["(not known)"]
    out = []
    cs2 = cfg.get("cs2_dir", "")
    out.append(f"CS2 folder: {cs2 or '(not set)'}  ->  {'usable' if config.is_valid_cs2(cs2) else 'NOT USABLE'}")
    if cs2:
        for rel in ("game/bin/win64/resourcecompiler.exe", "game/bin/win64/dmxconvert.exe",
                    "game/csgo/pak01_dir.vpk", "content/csgo_addons"):
            p = os.path.join(cs2, *rel.split("/"))
            out.append(f"  {rel}: {'there' if os.path.exists(p) else 'MISSING'}")
    try:
        games = config.find_s1_gamedirs()
        out.append(f"Source 1 games found: {len(games)}")
        out += [f"  {g}" for g in games]
    except Exception as e:  # noqa: BLE001
        out.append(f"Source 1 games: cannot list ({e})")
    res = cfg.get("resource_dirs") or []
    out.append(f"Resource folders: {len(res)}")
    for r in res:
        try:
            found = config.resource_entries([r])
        except Exception:  # noqa: BLE001
            found = []
        out.append(f"  {r}  ->  {'missing' if not os.path.isdir(r) else f'{len(found)} usable folders / packages'}")
    for k in sorted(cfg):
        if k in ("cs2_dir", "resource_dirs"):
            continue
        v = cfg[k]
        if isinstance(v, dict):
            out.append(f"{k}:")
            out += [f"  {kk} = {vv}" for kk, vv in sorted(v.items())]
        else:
            out.append(f"{k} = {v}")
    return out


def _report_lines(report, title, ctx, failure, trace=None):
    """report.txt: everything needed to see why a job went wrong, from a file the user sends.
    Always in English (log lines are taken back from the UI language)."""
    now = time.time()
    t0 = ctx.t0 if ctx is not None else now
    lines_in = ctx.lines if ctx is not None else []
    en = ctx.english_text if ctx is not None else (lambda m: m)
    cfg = ctx.cfg if ctx is not None else None
    out = []

    def section(name):
        out.extend(["", f"[{name}]"])

    warns = [(ts, m) for ts, tag, m in lines_in if tag == "warn"]
    errs = [(ts, m) for ts, tag, m in lines_in if tag == "err"]
    if failure:
        result = f"STOPPED: {en(failure)}"
    elif errs or warns:
        result = f"finished with {len(warns)} warning(s) and {len(errs)} error(s)"
    else:
        result = "finished without warnings or errors"

    out.append(f"{title} (CS2 Porter {__version__})")
    section("Job")
    out += [f"{k}: {v}" for k, v in (
        ("Input", report.input_path), ("Extra source", report.extra_path), ("Target", report.content_dir),
        ("Work folder", report.work_dir), ("Map file (VMF)", report.vmf_path),
        ("Started", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0))),
        ("Ended", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))),
        ("Took", _dur_text(now - t0)), ("Result", result),
        ("Map import", {None: "", True: "ok", False: "FAILED"}.get(report.import_ok, ""))) if v]
    if failure and trace:
        out.append("Error trace:")
        out += ["  " + r for r in trace.rstrip().split("\n")]

    section("Problems")
    if not (warns or errs or failure):
        out.append("none")
    for ts, m in sorted(warns + errs):
        tag = "ERROR" if (ts, m) in errs else "WARN "
        out.append(f"{_clock_text(ts - t0)} {tag} {en(m).strip()}")
    bad = [(k, r) for d in (report.materials, report.models) for k, r in sorted(d.items())
           if r.status in ("missing", "error", "unsupported")]
    if bad:
        out.append(f"Assets with a problem: {len(bad)}")
        for k, r in bad:
            src = f" <- {en(r.source)}" if getattr(r, "source", "") else ""
            out.append(f"  {t_en('st_' + r.status)}: {k}  {en(r.detail)}{src}".rstrip())

    section("Summary")
    for name, d, which in (("Materials", report.materials, "materials"), ("Models", report.models, "models")):
        if d:
            c = report.counts(which)
            out.append(f"{name}: {len(d)} ({_counts_text(sorted(c.items(), key=lambda x: -x[1]))})")
    if ctx is not None:
        snd = collections.Counter(st for kind, _n, st, _d, _s in ctx.results if kind == "sound")
        if snd:
            out.append(f"Sounds: {sum(snd.values())} ({_counts_text(snd.most_common())})")
    if report.skybox:
        out.append(f"Skybox: {en(report.skybox)}")
    steps = [(ts, m) for ts, tag, m in lines_in if tag == "head"]
    out.append(f"Log: {len(lines_in)} lines, {len(steps)} steps, {len(warns)} warnings, {len(errs)} errors")

    section("Computer")
    out += _system_lines(report, cfg)

    section("Settings")
    out += _settings_lines(cfg)

    if steps:
        section("Step times")
        for i, (ts, m) in enumerate(steps):
            end = steps[i + 1][0] if i + 1 < len(steps) else now
            out.append(f"{_dur_text(end - ts):>8}  {en(m).strip()}")

    if ctx is not None and ctx.runs:
        section("Commands")
        out.append("Every outside program the job ran: when, exit code, time, output lines. A command "
                   "that failed has its whole output below it, the others their last lines.")
        for n, (ts, args, code, sec, count, keep) in enumerate(ctx.runs, 1):
            state = "stopped / timed out" if code is None else f"exit code {code}"
            out.append(f"#{n} {_clock_text(ts - t0)} {os.path.basename(args[0])}: {state}, {_dur_text(sec)}, "
                       f"{count} output lines")
            out.append("   " + " ".join(f'"{a}"' if " " in a else a for a in args))
            out += ["   | " + r for r in keep]

    section("Log")
    out.append("Time since start, kind of line, text. STEP starts a step, DETL lines are the "
               "detailed log.")
    for ts, tag, m in lines_in:
        rows = en(m).rstrip("\n").split("\n")
        out.append(f"{_clock_text(ts - t0)} {_TAGS.get(tag, 'INFO ')} {rows[0]}")
        out += [f"{'':9} {'':5} {r}" for r in rows[1:]]
    if failure:
        out.append(f"{_clock_text(now - t0)} ERROR {en(failure)}")

    assets = report.asset_lines(ctx)
    if any(a.strip() for a in assets):
        section("Assets")
        out += [a[2:] if a.startswith("  ") else a for a in assets]
    return out


def write_failed_report(ctx, failure, trace=None):
    """report.txt of a job that was stopped or failed half way (when it got that far)."""
    pending = getattr(ctx, "pending_report", None)
    if not pending:
        return
    report, work, title = pending
    try:
        os.makedirs(work, exist_ok=True)
        report.write(os.path.join(work, REPORT_FILE), title, ctx, failure, trace)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Shared: sources and asset conversion
# ---------------------------------------------------------------------------

def open_sources(cfg, ctx, embedded_dir=None, extra_dirs=(), prefer=None):
    """Asset sources: extra folders, the map's embedded files and every installed Source 1 game.
    prefer: game mod folders searched first (e.g. ("csgo",) for a CS:GO map)."""
    ctx.log(t("p_sources"), "info")
    # an extra folder is read like a resource folder (game folders and packages in it), and
    # after the games every other file in it counts too, found by its name
    sources = AssetSources(embedded_dir, config.resource_entries(cfg.get("resource_dirs")),
                           config.find_s1_gamedirs(prefer), log=ctx.log,
                           extra_dirs=config.resource_entries(extra_dirs), loose_dirs=extra_dirs)
    shown = set()
    for s in sources.sources:
        # shorten, so dozens of VPKs of the same game are not listed one by one
        key = s.label.split("/")[0] if s.role == "game" else s.label
        if key not in shown:
            shown.add(key)
            ctx.log(t("p_source", label=key if s.role == "game" else s.label), "dim")
    return sources


def _is_tool(mat):
    # tools/* (toolstrigger, toolsskybox, toolsplayerclip, toolsinvisible-dx10 ...) already exist in CS2
    return mat.replace("\\", "/").lower().startswith("tools/")


def _texture_lights(ctx, info):
    """Texture lights of the .bsp (texlights.texture_lights), {} when it has none or can not be read."""
    try:
        found = texlightsmod.texture_lights(info)
    except Exception:  # noqa: BLE001 - the materials are still made, without the glow
        return {}
    if found:
        ctx.detail(t("d_texture_lights", n=len(found), list=", ".join(sorted(found))))
    return found


def convert_assets(cfg, ctx, report, sources, materials, models, content_dir, game_dir,
                   staging_dir, valve, p0=0.3, p1=0.9, texture_lights=None):
    """materials: {mat: set(usage)}, models: {mdl: count}"""
    opts = cfg["opts"]
    index = CS2Index(cfg.get("cs2_dir", ""), content_dir, game_dir, log=ctx.log)
    if index.paths:
        ctx.log(t("p_cs2_index", n=len(index.paths)), "dim")
    matconv = MaterialConverter(sources, index, content_dir, config.VTFCMD, ctx.log,
                                overwrite=opts.get("overwrite", False),
                                convert_cs2_existing=opts.get("convert_cs2_existing", False),
                                texture_lights=texture_lights)
    qc_index = QCIndex(sources.resource_sources(), config.CACHE_DIR, ctx.log)
    if qc_index.by_model:
        ctx.log(t("p_qc_index", n=len(qc_index.by_model)), "dim")
    modelconv = ModelConverter(sources, index, matconv, content_dir, qc_index, valve=valve,
                               staging_dir=staging_dir, log=ctx.log,
                               overwrite=opts.get("overwrite", False),
                               convert_cs2_existing=opts.get("convert_cs2_existing", False))
    tools_skipped = {k for k in materials if _is_tool(k) and not matconv.own_tool(k)}
    mats = {k: set(v) for k, v in materials.items() if k not in tools_skipped}
    total = max(1, len(models) + len(mats) + 1)
    done = 0

    def step(text):
        nonlocal done
        done += 1
        ctx.progress(p0 + (p1 - p0) * min(1.0, done / total), text)

    # --- models --------------------------------------------------------------
    if models:
        ctx.log(t("p_models", n=len(models)), "head")
        for mdl in sorted(models):
            ctx.check()
            r = modelconv.convert(mdl)
            report.models[r.mdl] = r
            ctx.result("model", r.mdl, r.status, r.detail, r.source)
            tag = {"created": "ok", "missing": "warn", "error": "err"}.get(r.status, "dim")
            ctx.log(f"  [{status_text(r.status)}] {r.mdl}  {r.detail}", tag)
            if r.status == "created":
                report.created_files.append(os.path.join(content_dir, *(out_name(r.mdl[:-4]) + ".vmdl").split("/")))
                for m in r.materials:
                    if _is_tool(m) and not matconv.own_tool(m.lower()):
                        tools_skipped.add(m.lower())
                    else:
                        mats.setdefault(m.lower(), set()).add("model")
            step(t("pr_model", name=os.path.basename(mdl)))
        total = max(1, len(models) + len(mats) + 1)

    # --- materials -----------------------------------------------------------
    if mats:
        ctx.log(t("p_materials", n=len(mats)), "head")
        for mat in sorted(mats):
            ctx.check()
            r = matconv.convert(mat, mats[mat])
            report.materials[r.mat] = r
            ctx.result("material", r.mat, r.status, r.detail, r.source)
            tag = {"created": "ok", "missing": "warn", "error": "err", "unsupported": "warn"}.get(r.status, "dim")
            if r.status not in ("cs2", "exists", "tool"):
                ctx.log(f"  [{status_text(r.status)}] {r.mat}  {r.detail}", tag)
            if r.status == "created":
                report.created_files.append(os.path.join(content_dir, "materials", *(out_name(r.mat) + ".vmat").split("/")))
            step(t("pr_material", name=os.path.basename(mat)))
        skipped = sum(1 for r in report.materials.values() if r.status in ("cs2", "exists", "tool"))
        if skipped:
            ctx.log(t("p_skipped_mats", n=skipped), "dim")
    if tools_skipped:
        ctx.log(t("p_tools_skipped", n=len(tools_skipped)), "dim")


def make_skybox(cfg, ctx, report, sources, content_dir, skyname):
    """Builds skybox.png / skybox.vmat / skybox_moondome.vmat. True if they can be used."""
    ctx.check()
    st, detail = build_skybox(sources, content_dir, skyname, config.VTFCMD,
                              cfg["opts"].get("overwrite", False), ctx.log)
    report.skybox = f"{skyname}: {t_en('st_' + st)} ({detail})"
    ctx.result("skybox", SKY_VMAT, st, detail, "")
    ctx.log(t("p_skybox", sky=skyname, status=status_text(st), detail=detail),
            "ok" if st == "created" else ("warn" if st == "missing" else "dim"))
    if st == "created":
        report.created_files.append(os.path.join(content_dir, *SKY_VMAT.split("/")))
    return st in ("created", "exists")


def make_sounds(cfg, ctx, sources, content_dir, ent_sounds, bsp_name, ambients=(), map_name=""):
    """Copies the map's sounds (44100 Hz) and adds its soundscapes and one soundevent per
    ambient_generic sound to soundevents_addon.vsndevts. Returns {position: event name} for the
    ambient_generics."""
    scapes = soundsmod.map_soundscapes(sources, bsp_name)
    files = soundsmod.all_files(ent_sounds, scapes)
    if not files:
        return {}
    ctx.log(t("p_sounds", n=len(files), s=len(scapes)), "head")
    found = soundsmod.copy_sounds(files, sources, content_dir, cfg["opts"].get("overwrite", False),
                                  ctx.result, ctx.log)
    blocks, names = soundsmod.soundscape_events(scapes, found)
    amb_blocks, table = soundsmod.ambient_events(ambients, found, map_name or bsp_name)
    if blocks or amb_blocks:
        tpl = os.path.join(cfg.get("cs2_dir", ""), "content", "csgo_addons", "addon_template",
                           *soundsmod.EVENTS_FILE.split("/"))
        try:
            soundsmod.update_events_file(content_dir, blocks + amb_blocks, tpl)
            if names:
                ctx.log(t("p_soundevents", n=len(names)), "ok")
            if amb_blocks:
                ctx.log(t("p_ambient_events", n=len(amb_blocks)), "ok")
        except OSError as e:
            ctx.log(t("snd_fail", f=soundsmod.EVENTS_FILE, e=e), "err")
            table = {}
    missing = sum(1 for v in found.values() if not v)
    if missing:
        ctx.log(t("p_sounds_missing", n=missing), "warn")
    return table


# converted entity kind -> files it needs in the addon (copied from assets/, same file name)
PARTICLES = {
    "glows": ("particles/glow_particle.vpcf",),
    "beams": ("particles/beam_particle.vpcf",),
    "steam": ("particles/steam_particle.vpcf",),
    "dustmotes": ("particles/dustmotes.vpcf",),
    "spotlight": ("particles/light_ray.vpcf", "materials/cs2porter/light_beam.png",
                  "materials/cs2porter/light_beam.vtex"),
    "trail": ("particles/trail_particle.vpcf",),
}


# always written again (the program changed them; light_ray.vpcf takes length / width / alpha
# since 2.12, an older copy would read them as the beam vector)
OWN_PARTICLES = ("particles/steam_particle.vpcf", "particles/trail_particle.vpcf", "particles/light_ray.vpcf")
BOX_SPRITES_TEMPLATE = "box_sprites_template.vpcf"
SMOKE_TEMPLATE = "smoke_template.vpcf"


def make_particles(cfg, ctx, valve, sources, work, content_dir, embedded_dir, addon, wanted):
    """Turns the Source 1 particle systems the map shows into CS2 ones (see particles.py).
    wanted: {lower case name: name}. Returns {lower case name: 'particles/x/y.vpcf'}."""
    if not wanted or not cfg["opts"].get("port_particles", True) or valve is None \
            or not valve.has_source1import:
        return {}
    ctx.check()
    ctx.log(t("p_particles", n=len(wanted)), "head")
    if " " in work:
        ctx.log(t("p_particles_space"), "warn")
        return {}
    pcfs, _found = particlesmod.find_pcfs(sources, embedded_dir, set(wanted))
    if not pcfs:
        ctx.log(t("p_particles_none", n=len(wanted)), "warn")
        return {}
    ctx.detail(t("d_particle_files", n=len(pcfs), list=", ".join(sorted(pcfs))))
    before = particlesmod.snapshot(content_dir)
    t0 = time.time()
    try:
        gi = particlesmod.build_stub(work, sources, pcfs)
        valve.import_particles(gi, addon)
    except (ToolError, OSError) as e:
        ctx.log(t("p_particles_fail", e=e), "err")
    finally:
        particlesmod.remove_stub(work)
    try:
        names, kept = particlesmod.finish(content_dir, before, set(wanted))
    except OSError as e:
        ctx.log(t("p_particles_fail", e=e), "err")
        return {}
    missing = sorted(wanted[n] for n in wanted if n not in names)
    ctx.log(t("p_particles_done", n=len(names), k=kept, sec=time.time() - t0), "ok" if names else "warn")
    if missing:
        ctx.log(t("p_particles_missing", n=len(missing), list=", ".join(missing[:12])), "warn")
    return names


def _num_text(v):
    v = round(float(v), 3)
    return f"{v:.1f}" if v == int(v) else f"{v:g}"


_CONTINUOUS = """\t\t{
\t\t\t_class = "C_OP_ContinuousEmitter"
\t\t\tm_flEmitRate =
\t\t\t{
\t\t\t\tm_nType = "PF_TYPE_LITERAL"
\t\t\t\tm_flLiteralValue = %s
\t\t\t}
\t\t},"""
_INSTANT = """\t\t{
\t\t\t_class = "C_OP_InstantaneousEmitter"
\t\t\tm_nParticlesToEmit =
\t\t\t{
\t\t\t\tm_nType = "PF_TYPE_LITERAL"
\t\t\t\tm_flLiteralValue = %s
\t\t\t}
\t\t},"""
_IN_SPHERE = """\t\t{
\t\t\t_class = "C_INIT_CreateWithinSphereTransform"
\t\t\tm_fRadiusMax = %s
\t\t},"""
_IN_BOX = "\t\t{\n\t\t\t_class = \"C_INIT_CreateWithinBox\"\n\t\t\tm_bLocalSpace = false\n" + "".join(
    "\t\t\t%s =\n\t\t\t{\n\t\t\t\tm_nType = \"PVEC_TYPE_FLOAT_COMPONENTS\"\n" % name + "".join(
        "\t\t\t\tm_FloatComponent%s =\n\t\t\t\t{\n\t\t\t\t\tm_nType = \"PF_TYPE_CONTROL_POINT_COMPONENT\"\n"
        "\t\t\t\t\tm_nControlPoint = 1\n\t\t\t\t\tm_nVectorComponent = %d\n"
        "\t\t\t\t\tm_nMapType = \"PF_MAP_TYPE_MULT\"\n\t\t\t\t\tm_flMultFactor = %s\n\t\t\t\t}\n"
        % (axis, k, factor) for k, axis in enumerate("XYZ")) + "\t\t\t}\n"
    for name, factor in (("m_vecMin", "-0.5"), ("m_vecMax", "0.5"))) + "\t\t},"
_OP = "\t\t{\n\t\t\t_class = \"%s\"\n%s\t\t},"


def _smoke_blocks(params):
    """Emitter, position and operator blocks of assets/smoke_template.vpcf."""
    ops = []
    if params.get("count"):
        # puffs that fill a volume for good: made once, never die
        emitter = _INSTANT % _num_text(params["count"])
        ops.append(_OP % ("C_OP_FadeIn", "\t\t\tm_flFadeInTimeMin = 1.0\n\t\t\tm_flFadeInTimeMax = 1.0\n"
                                         "\t\t\tm_bProportional = false\n"))
    else:
        emitter = _CONTINUOUS % _num_text(params["rate"])
        ops.append(_OP % ("C_OP_Decay", ""))
        ops.append(_OP % ("C_OP_FadeIn", "\t\t\tm_flFadeInTimeMin = 0.1\n\t\t\tm_flFadeInTimeMax = 0.1\n"
                                         "\t\t\tm_bProportional = true\n"))
        ops.append(_OP % ("C_OP_FadeOut", "\t\t\tm_flFadeOutTimeMin = 0.6\n\t\t\tm_flFadeOutTimeMax = 0.6\n"
                                          "\t\t\tm_bProportional = true\n"))
    if params.get("color_end"):
        ops.append(_OP % ("C_OP_ColorInterpolate", "\t\t\tm_ColorFade = [ %s ]\n"
                          % ", ".join(str(int(c)) for c in params["color_end"])))
    position = _IN_SPHERE % _num_text(params["spawn_radius"]) if "spawn_radius" in params else _IN_BOX
    return {"emitter": emitter, "position": position, "operators": "\n".join(ops)}


def generated_particle(params):
    """Text of a particle made from an assets template (vmf._embers_params ...): box sprites, or
    smoke when params["_template"] is "smoke"."""
    import string
    if params.get("_template") == "dustmotes":
        # dustmotes.vpcf unchanged except for how far away it is drawn
        with open(os.path.join(config.ASSETS_DIR, "dustmotes.vpcf"), "r", encoding="utf-8") as f:
            text = f.read()
        return text.replace("m_flMaxDrawDistance = 5000.0",
                            f"m_flMaxDrawDistance = {float(params['draw_distance']):.1f}", 1)
    smoke = params.get("_template") == "smoke"
    name = SMOKE_TEMPLATE if smoke else BOX_SPRITES_TEMPLATE
    with open(os.path.join(config.ASSETS_DIR, name), "r", encoding="utf-8") as f:
        tmpl = string.Template(f.read())
    vals = {}
    for k, v in params.items():
        if k.startswith("_") or v is None:
            continue
        if isinstance(v, tuple):
            ints = k.startswith("color")
            vals[k] = ", ".join(str(int(c)) if ints else _num_text(c) for c in v)
        else:
            vals[k] = _num_text(v)
    vals.setdefault("draw_distance", "5000")
    # enough room for every particle that is alive at once
    if params.get("count"):
        vals["max_particles"] = str(int(params["count"]) + 8)
    else:
        vals["max_particles"] = str(min(int(params["rate"] * params["life_max"] * 1.25) + 16, 10000))
    if smoke:
        vals.update(_smoke_blocks(params))
        vals["self_illum"] = "0.3"
    return tmpl.substitute(vals)


def write_particles(ctx, content_dir, kinds, overwrite=False, generated=None):
    """Copies the particle systems (and their textures) the converted entities use into the
    addon. kinds: the PARTICLES keys that are needed; generated: {vpcf: (kind, params, key)}
    of particles made for entity values (always written)."""
    for rel, (_kind, params, _key) in sorted((generated or {}).items()):
        dst = os.path.join(content_dir, *rel.split("/"))
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="utf-8", newline="\n") as f:
                f.write(generated_particle(params))
            ctx.log(t("p_particle", f=rel), "ok")
        except (OSError, KeyError, ValueError) as e:
            ctx.log(t("snd_fail", f=rel, e=e), "err")
    for key, files in PARTICLES.items():
        if key not in kinds:
            continue
        for rel in files:
            dst = os.path.join(content_dir, *rel.split("/"))
            # the program's own files (materials/cs2porter, the steam particle) are always brought
            # up to date; the other bundled particles are only replaced on overwrite
            own = rel.startswith("materials/cs2porter/") or rel in OWN_PARTICLES
            if os.path.isfile(dst) and not overwrite and not own:
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(os.path.join(config.ASSETS_DIR, os.path.basename(rel)), dst)
                if rel.endswith(".vpcf"):
                    ctx.log(t("p_particle", f=rel), "ok")
            except OSError as e:
                ctx.log(t("snd_fail", f=rel, e=e), "err")


def missing_rope_materials(report, ropes):
    """Rope materials that are neither in CS2 nor converted, or that CS2 draws broken (they get
    the black material)."""
    mats = {r.get("material") for r in ropes.values() if r.get("material")}
    bad = ("missing", "error", "unsupported")
    return {m for m in mats if m in BROKEN_CABLE_MATERIALS or m not in report.materials
            or report.materials[m].status in bad}


def water_vmats(sources, materials):
    """'materials/x.vmat' of the map's materials that use the Source 1 water shader."""
    from .materials import parse_vmt

    def load(rel):
        return sources.read(rel)[0]

    out = set()
    for mat in materials:
        rel = f"materials/{mat}.vmt"
        data = load(rel)
        if not data or b"water" not in data.lower():
            continue
        try:
            if parse_vmt(data, rel, load).shader == "water":
                out.add(f"materials/{out_name(mat)}.vmat")
        except Exception:  # noqa: BLE001
            continue
    return out


def _color_corrections(root):
    """Map-wide color_correction entities (no falloff) of the VMF: [(file, weight)], the ones
    that start on first."""
    out = []
    for e in root.children("entity"):
        if e.classname != "color_correction" or not e.get("filename"):
            continue
        try:
            global_ = float(e.get("minfalloff") or -1) < 0 or float(e.get("maxfalloff") or -1) < 0
            weight = float(e.get("maxweight") or 1)
        except ValueError:
            continue
        if global_:
            out.append((e.get("StartDisabled", "0") == "1", e.get("filename"), weight))
    return [(f, w) for _off, f, w in sorted(out, key=lambda x: x[0])]


def _apply_color_correction(ctx, sources, content_dir, map_name, entries):
    """The first color correction table that is found goes into the map's .vpost as layers."""
    vpost = os.path.join(content_dir, "postprocess", f"{map_name}.vpost")
    if not os.path.isfile(vpost):
        return
    for filename, weight in entries:
        rel = filename.replace("\\", "/").lower().lstrip("/")
        data, _src = sources.read(rel)
        lut = postfxmod.read_lut(data)
        if lut is None:
            ctx.log(t("p_cc_missing", f=rel), "warn")
            continue
        try:
            if postfxmod.add_layers(vpost, postfxmod.cc_layers(lut, weight)):
                ctx.log(t("p_cc_done", f=os.path.basename(rel)), "ok")
        except (OSError, ValueError) as e:
            ctx.log(t("p_cc_fail", e=e), "warn")
        return


def _underwater(ctx, opts, content_dir, water_set):
    """Underwater .vpost files for the map's water materials: {vmat: 'postprocess/x.vpost'}."""
    if not water_set or not opts.get("post_processing", True):
        return {}
    fogs = {}
    for v in water_set:
        fog = postfxmod.water_fog(os.path.join(content_dir, *v.split("/")))
        if fog:
            fogs[v] = fog
    if not fogs:
        return {}
    try:
        out = postfxmod.write_underwater(content_dir, fogs)
    except OSError as e:
        ctx.log(t("p_cc_fail", e=e), "warn")
        return {}
    ctx.detail(t("d_underwater", n=len(set(out.values()))))
    return out


def _surface_lights(ctx, sources, rects, tex_lights):
    """Lights in front of glowing water (and refract) surfaces: their shaders can not glow."""
    if not rects:
        return []
    from .materials import parse_vmt

    def load(rel):
        return sources.read(rel)[0]

    no_glow = set()
    for mat in {r[0] for r in rects}:
        rel = f"materials/{mat}.vmt"
        data = load(rel)
        try:
            if data and parse_vmt(data, rel, load).shader in ("water", "refract"):
                no_glow.add(mat)
        except Exception:  # noqa: BLE001
            continue
    lights = texlightsmod.rect_lights([r for r in rects if r[0] in no_glow], tex_lights)
    if lights:
        ctx.detail(t("d_surface_lights", n=len(lights), list=", ".join(sorted(no_glow))))
    return lights


def _detail_files(ctx, folder, key):
    """Verbose line: the files of a folder counted by type."""
    kinds = collections.Counter()
    for _dp, _dn, fn in os.walk(folder):
        for f in fn:
            kinds[os.path.splitext(f)[1].lower().lstrip(".") or "?"] += 1
    if kinds:
        ctx.detail(t(key, n=sum(kinds.values()), list=", ".join(f"{k} {n}" for k, n in kinds.most_common())))


def compile_assets(cfg, ctx, valve, content_dir, game_dir, work):
    """Compiles the addon's materials and models (vmat_c / vmdl_c) into its game folder, so
    Hammer does not have to do it the first time the map is opened. Only files without an
    up to date compiled version are given to the compiler. The map itself is never compiled."""
    if not cfg["opts"].get("compile_assets", True) or not game_dir or valve is None \
            or not valve.has_resourcecompiler:
        return
    todo = []
    for sub, ext in (("materials", ".vmat"), ("materials", ".vtex"), ("models", ".vmdl"), ("particles", ".vpcf")):
        base = os.path.join(content_dir, sub)
        for dp, _dn, fn in os.walk(base):
            for f in fn:
                if not f.lower().endswith(ext):
                    continue
                src = os.path.join(dp, f)
                out = os.path.join(game_dir, os.path.relpath(src, content_dir)) + "_c"
                if not os.path.isfile(out) or os.path.getmtime(out) < os.path.getmtime(src):
                    todo.append(src)
    if not todo:
        return
    ctx.check()
    ctx.progress(0.93, t("pr_compile"))
    ctx.log(t("p_compile", n=len(todo)), "head")
    kinds = collections.Counter(os.path.splitext(p)[1].lower().lstrip(".") for p in todo)
    ctx.detail(t("d_compile_list", list=", ".join(f"{k} {n}" for k, n in kinds.most_common())))
    t0 = time.time()
    try:
        ok, failed, _out = valve.compile_resources(todo, os.path.join(work, "compile_list.txt"))
    except ToolError as e:
        ctx.log(str(e), "err")
        return
    done = 0
    for src in todo:
        rel = os.path.relpath(src, content_dir)
        if os.path.isfile(os.path.join(game_dir, rel) + "_c"):
            done += 1
        else:
            ctx.detail(t("d_not_compiled", f=rel.replace("\\", "/")))
    ctx.log(t("p_compile_done", n=done, total=len(todo), sec=time.time() - t0),
            "ok" if done == len(todo) else "warn")
    if failed:
        ctx.log(t("p_compile_failed", n=failed), "warn")


def _summary(ctx, report):
    for which, key in (("models", "p_sum_models"), ("materials", "p_sum_materials")):
        c = report.counts(which)
        if not c:
            continue
        parts = [f"{status_text(k)}: {v}" for k, v in sorted(c.items(), key=lambda x: -x[1])]
        bad = c.get("missing", 0) + c.get("error", 0)
        ctx.log(t(key, parts=" | ".join(parts)), "warn" if bad else "ok")


def _finish(ctx, report, work, title, t0):
    os.makedirs(work, exist_ok=True)
    old = os.path.join(work, "rapor.txt")       # Turkish report name of older versions
    if os.path.isfile(old):
        os.remove(old)
    rp = os.path.join(work, REPORT_FILE)
    report.write_missing_refs(work)
    ctx.log(t("p_report", path=rp), "dim")
    ctx.progress(1.0, t("pr_done"))
    ctx.log(t("p_done", sec=time.time() - t0), "head")
    # written last, so it holds every line above
    report.write(rp, title, ctx)
    ctx.pending_report = None


# ---------------------------------------------------------------------------
# Reference lists (_refs.txt, console log, missing_*_ref.txt ...)
# ---------------------------------------------------------------------------

_REF_RE = re.compile(r'((?:materials|models)[/\\][^\s"\'!)<>|*?]+?\.(?:vmt|vmat|mdl|vmdl))(?:_c)?(?![A-Za-z0-9_])',
                     re.IGNORECASE)


def parse_reference_text(text):
    mats, mdls = {}, {}
    for m in _REF_RE.finditer(text):
        p = m.group(1).replace("\\", "/").lower()
        while "//" in p:
            p = p.replace("//", "/")
        if p.endswith((".vmt", ".vmat")):
            name = vmfmod.normalize_material(p.rsplit(".", 1)[0])
            mats.setdefault(name, set()).add("ref")
        else:
            mdls[norm_model(p.rsplit(".", 1)[0] + ".mdl")] = 1
    return mats, mdls


def read_reference_file(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return parse_reference_text(f.read())


# ---------------------------------------------------------------------------
# Map porting
# ---------------------------------------------------------------------------

def _tracer(info, root):
    """Line traces against the compiled map (None for a .vmf input or a broken .bsp)."""
    if info is None:
        return None
    try:
        return lightingmod.BspTracer(info, root)
    except Exception:  # noqa: BLE001 - a broken lump only costs the line traces
        return None


def _light_setup(ctx, opts, root, info, tracer):
    """Lights for the .vmap fixes, the bounds of the map (for the visibility_hint) and the light
    probe volumes with their rooms. info: the .bsp (None for a .vmf input)."""
    bounds = vmfmod.map_bounds(root)
    rooms = None
    if opts.get("light_probes", True):
        try:
            rooms = probesmod.plan(root, tracer)
        except Exception as e:  # noqa: BLE001 - the map is still ported without them
            ctx.log(t("p_light_rooms_fail", e=e), "warn")
        if rooms:
            voxels = sum((hi[0] - lo[0]) * (hi[1] - lo[1]) * (hi[2] - lo[2]) for lo, hi, _r in rooms) / 108.0 ** 3
            ctx.detail(t("d_light_rooms", n=len(rooms), rooms=sum(1 for r in rooms if r[2]), k=round(voxels / 1000)))
    if not opts.get("fix_light_brightness", True):
        return vmfmod.collect_lights(root), bounds, rooms
    try:
        values = lightingmod.light_values(root, info if tracer is not None else None, tracer)
    except Exception:  # noqa: BLE001
        values = lightingmod.light_values(root)
    if values:
        lum = sorted(v["lumens"] * v.get("cone", 1.0) for v in values.values())
        ctx.detail(t("d_light_lumens", n=len(lum), lo=lum[0], mid=lum[len(lum) // 2], hi=lum[-1]))
    return vmfmod.collect_lights(root, values), bounds, rooms


def port_map(cfg, ctx, input_path, addon_name):
    t0 = time.time()
    opts = cfg["opts"]
    report = Report()
    is_vmf = input_path.lower().endswith(".vmf")
    map_name = sanitize_name(os.path.splitext(os.path.basename(input_path))[0])
    addon = sanitize_name(addon_name) if addon_name else map_name

    work = os.path.join(config.work_root(), map_name)
    content_root = os.path.join(work, "content")
    embedded_dir = os.path.join(work, "embedded")
    staging_dir = os.path.join(work, "mdl_staging")
    vmf_path = os.path.join(content_root, "maps", f"{map_name}.vmf")
    os.makedirs(os.path.dirname(vmf_path), exist_ok=True)
    report.work_dir = work
    report.vmf_path = vmf_path
    report.input_path = input_path
    ctx.cfg = cfg
    # a job that stops half way still leaves its report
    ctx.pending_report = (report, work, t_en("report_title_port", map=map_name, addon=addon))

    cs2_dir = cfg.get("cs2_dir", "")
    cs2_ok = config.is_valid_cs2(cs2_dir)
    valve = ValveTools(cs2_dir if cs2_ok else "", ctx.log, ctx.cancel)
    # the newest bhop script downloads while the map is ported and becomes the saved copy
    bhop_dl = bhop.Download() if opts.get("bhop_script", True) and opts.get("bhop_download", True) else None

    ctx.log(t("p_map", map=map_name, addon=addon), "head")
    ctx.log(t("p_work", path=work), "dim")
    if not is_vmf and not config.find_bspsource():
        # stop before an addon is made for a map that can not be opened
        raise ToolError(t("vt_no_bspsource", path=config.BSPSOURCE_DIR))

    if cs2_ok:
        content_dir, game_dir, created = valve.ensure_addon(addon)
        if created:
            ctx.log(t("p_new_addon", addon=addon), "ok")
    else:
        content_dir = os.path.join(work, "output")
        game_dir = None
        os.makedirs(content_dir, exist_ok=True)
        ctx.log(t("p_no_cs2_out"), "warn")
    report.content_dir = content_dir
    ctx.log(t("p_target", path=content_dir), "dim")

    # --- 1) embedded files + decompile --------------------------------------------
    ctx.progress(0.02, t("pr_read_bsp"))
    if os.path.isdir(embedded_dir):
        shutil.rmtree(embedded_dir, ignore_errors=True)
    if not is_vmf:
        info = bspmod.BSPInfo(input_path)
        ctx.log(t("p_bsp_version", ver=info.version, game=info.game_guess), "info")
        ctx.log(t("p_extract"), "head")
        try:
            bspmod.extract_pakfile(input_path, embedded_dir, smart=True, log=ctx.log)
        except Exception as e:  # noqa: BLE001
            ctx.log(t("p_extract_fail", e=e), "warn")
        _detail_files(ctx, embedded_dir, "d_extracted")
        ctx.check()
        ctx.progress(0.05, t("pr_decompile"))
        ctx.log(t("p_decompile"), "head")
        decompile_bsp(input_path, vmf_path, log=ctx.log, cancel=ctx.cancel)
        ctx.log(t("p_vmf_ready", path=vmf_path), "ok")
        ctx.detail(t("d_vmf_size", mb=os.path.getsize(vmf_path) / 1048576.0))
    else:
        if os.path.normcase(os.path.abspath(input_path)) != os.path.normcase(os.path.abspath(vmf_path)):
            shutil.copyfile(input_path, vmf_path)
        ctx.log(t("p_vmf_input"), "info")
    ctx.check()

    # --- 2) analysis and fixes -------------------------------------------------
    ctx.progress(0.15, t("pr_analyze"))
    ctx.log(t("p_analyze"), "head")
    root = vmfmod.parse_vmf(vmf_path)
    if not is_vmf:
        vmfmod.restore_brush_angles(root, info.entities())
        try:
            clips, sides_fixed = vmfmod.restore_clip_brushes(root, info.brushes())
        except Exception:  # noqa: BLE001 - an unusual .bsp keeps the decompiled textures
            clips = sides_fixed = 0
        if clips or sides_fixed:
            ctx.detail(t("d_clip_brushes", n=clips, m=sides_fixed))
        try:
            nonsolid = vmfmod.restore_nonsolid(root, info.brushes())
        except Exception:  # noqa: BLE001 - the brushes stay as they are
            nonsolid = 0
        if nonsolid:
            ctx.detail(t("d_nonsolid", n=nonsolid))
    vinfo = vmfmod.analyze(root)
    ctx.log(t("p_analyze_counts", brushes=vinfo.brush_count, disps=vinfo.disp_count, ents=vinfo.entity_count), "info")
    ctx.log(t("p_analyze_assets", mats=len(vinfo.materials), mdls=len(vinfo.models), sky=vinfo.skyname or "-"), "info")
    uses = collections.Counter(u for v in vinfo.materials.values() for u in v)
    if uses:
        ctx.detail(t("d_material_uses", list=", ".join(f"{k} {n}" for k, n in uses.most_common())))
    classes = collections.Counter(e.classname for e in root.children("entity"))
    if classes:
        ctx.detail(t("d_entities", list=", ".join(f"{k} {n}" for k, n in classes.most_common(12))))
    original = os.path.join(content_root, "maps", f"{map_name}_original.vmf")
    shutil.copyfile(vmf_path, original)
    # toolsskybox faces get a stand-in material for the import (see vmf.pick_sky_alias)
    sky_alias = vmfmod.pick_sky_alias(vinfo.materials) if vmfmod.has_sky_faces(vinfo.materials) else None
    tracer = _tracer(None if is_vmf else info, root)
    lights, vis_bounds, light_rooms = _light_setup(ctx, opts, root, None if is_vmf else info, tracer)
    if light_rooms and sky_alias:
        # the white rooms are wrapped in sky brushes (moondome) so they look like the sky
        shells = vmfmod.add_room_shells(root, light_rooms, probesmod.ROOM_HALF, vmfmod.SKYBOX_MAT)
        if shells:
            ctx.detail(t("d_room_shells", n=shells))
    bhop_on = bool(opts.get("bhop_script", True))
    attributes, meshes, effects = {}, {}, {}
    vmfmod.fix_for_cs2(root, sky_alias=sky_alias, attributes=attributes, meshes=meshes,
                       teleport_key=vmapmod.TELEPORT_KEY if bhop_on else None,
                       split_triggers=bool(opts.get("split_triggers", True)), effects=effects, log=ctx.log,
                       bhop_blocks=bool(opts.get("bhop_blocks", True)), world=tracer)
    del tracer
    vmfmod.write_vmf(root, vmf_path)
    # texture lights, and the faces of the ones that may need lights in front of them (water)
    tex_lights = {} if is_vmf else _texture_lights(ctx, info)
    glow_rects = vmfmod.surface_rects(root, set(tex_lights)) if tex_lights else []
    color_corrections = _color_corrections(root)
    ent_sounds = soundsmod.entity_sounds(root)
    ambients = soundsmod.collect_ambients(root)
    wanted_particles = particlesmod.wanted_systems(root)
    ropes = vmfmod.collect_ropes(root)
    disps = blendmod.collect_disps(root)
    del root
    ctx.check()

    materials = {k: set(v) for k, v in vinfo.materials.items()}
    models = dict(vinfo.models)
    # CS:GO maps (BSP v21+) look in CS:GO files first, older maps in CSS files first
    prefer = ("csgo",) if (not is_vmf and info.version >= 21) else ("cstrike",)
    sources = open_sources(cfg, ctx, embedded_dir if os.path.isdir(embedded_dir) else None, prefer=prefer)
    try:
        # --- 3) map geometry (import) ---------------------------------------
        ctx.progress(0.2, t("pr_import"))
        aliases = {sky_alias: vmfmod.SKYBOX_MAT} if sky_alias else None
        refs = _import_map(cfg, ctx, report, valve, sources, work, content_root, materials,
                           vinfo.skyname, map_name, addon, cs2_ok, aliases)
        ctx.check()

        # --- 3b) skybox, sounds and soundscapes --------------------------------------------
        sky_ok = make_skybox(cfg, ctx, report, sources, content_dir, vinfo.skyname) if vinfo.skyname else False
        bsp_name = os.path.splitext(os.path.basename(input_path))[0]
        ambient_table = make_sounds(cfg, ctx, sources, content_dir, ent_sounds, bsp_name, ambients, map_name)
        particle_names = make_particles(cfg, ctx, valve if cs2_ok else None, sources, work, content_dir,
                                        embedded_dir if os.path.isdir(embedded_dir) else None, addon,
                                        wanted_particles)
        ctx.check()

        # --- 4) assets from the refs.txt list are added too --------------------------------
        if refs:
            m1, m2 = read_reference_file(refs)
            # the importer's list is only read here, it does not belong in the addon
            try:
                os.remove(refs)
            except OSError:
                pass
            # skybox faces listed for env_sky are not made into materials (handled in the skybox step)
            if vinfo.skyname:
                sky = vinfo.skyname.lower()
                faces = {f"skybox/{sky}{h}{f}" for h in ("", "_hdr") for f in ("up", "dn", "lf", "rt", "ft", "bk")}
                m1 = {k: v for k, v in m1.items() if k not in faces}
            if sky_alias:
                m1.pop(sky_alias, None)
            new = sum(1 for k in m1 if k not in materials) + sum(1 for k in m2 if k not in models)
            for k, v in m1.items():
                materials.setdefault(k, set()).update(v)
            for k in m2:
                models.setdefault(k, 1)
            ctx.log(t("p_refs", mats=len(m1), mdls=len(m2)), "info")
            if new:
                ctx.log(t("p_refs_new", n=new), "dim")

        # --- 5) missing assets ---------------------------------------------------------
        if os.path.isdir(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)
        convert_assets(cfg, ctx, report, sources, materials, models,
                       content_dir, game_dir, staging_dir, valve if cs2_ok else None, 0.3, 0.85,
                       texture_lights=tex_lights or None)
        _summary(ctx, report)
        ctx.check()

        # --- 6) .vmap fixes and the bhop script (after the materials: the texture scale fix
        # needs their texture sizes) ----------------------------------------------------------
        bhop_data, bhop_err = bhop.update(bhop_dl, ctx.log) if bhop_dl else (None, None)
        if report.import_ok:
            ctx.progress(0.86, t("pr_vmap"))
            ctx.log(t("p_vmap"), "head")
            maps_dir = os.path.join(cfg["cs2_dir"], "content", "csgo_addons", addon, "maps")
            cable_missing = missing_rope_materials(report, ropes)
            if cable_missing:
                write_cable_fallback(content_dir, opts.get("overwrite", False))
            water_set = water_vmats(sources, materials)
            underwater = _underwater(ctx, opts, content_dir, water_set)
            surface_lights = _surface_lights(ctx, sources, glow_rects, tex_lights)
            fx = vmapmod.Fixes(sky_alias=sky_alias, sky_vmat=SKY_VMAT if sky_ok else None,
                               moondome_vmat=MOONDOME_VMAT if sky_ok else None, lights=lights,
                               lumens=bool(opts.get("fix_light_brightness", True)),
                               bhop=bhop_on, attributes=attributes, meshes=meshes,
                               blends=blendmod.only_blends(disps, sources) if disps else None,
                               fgd=fgdmod.load_cs2(cfg["cs2_dir"]), ropes=ropes, ambients=ambient_table,
                               water=water_set, underwater=underwater, surface_lights=surface_lights,
                               commands=map_name.lower().startswith("bhop_"),
                               uv_sizes=uvfix.TextureSizes(content_dir),
                               effects=effects, cable_missing=cable_missing, vis_bounds=vis_bounds,
                               light_rooms=light_rooms, particle_names=particle_names)
            res = vmapmod.post_process(valve, work, maps_dir, map_name, fx, ctx.log)
            if res.stats.get("black"):
                # black faces (also the ones the import itself gives a black material)
                write_black_material(content_dir)
            if res.stats.get("cables_black"):
                write_cable_fallback(content_dir, overwrite=True)
            # the bhop script is only needed when the map has basevelocity / gravity / teleport outputs
            if bhop_on and res.bhop_outputs and game_dir:
                bhop.install(game_dir, ctx.log, bhop_data, bhop_err)
            kinds = {k for k in ("glows", "beams") if res.stats.get(k)}
            if res.stats.get("effects"):
                kinds |= set(effects.get("kinds") or {})
            write_particles(ctx, content_dir, kinds, opts.get("overwrite", False),
                            effects.get("generated") if res.stats.get("effects") else None)
            _beam_notice(report, res.stats)
        moved = vmapmod.move_postprocess(content_dir, overwrite=created or opts.get("overwrite", False)) \
            if cs2_ok else 0
        if moved:
            ctx.log(t("p_vpost_moved", n=moved), "dim")
        if cs2_ok and color_corrections and opts.get("post_processing", True):
            _apply_color_correction(ctx, sources, content_dir, map_name, color_corrections)
    finally:
        sources.close()
    ctx.check()
    if cs2_ok:
        compile_assets(cfg, ctx, valve, content_dir, game_dir, work)
    for note in report.notices:
        ctx.log(note, "warn")

    # Note: the map itself is never compiled; that is done in Hammer.
    _finish(ctx, report, work, t_en("report_title_port", map=map_name, addon=addon), t0)
    return report


MAX_PARALLEL = 4
# maps of different addons ported at the same time
PARALLEL_PORTS = 2


class BatchReport:
    """Result of port_maps: what the window needs of every map's Report."""

    def __init__(self):
        self.reports = []           # (map path, Report or None, error or None)
        self.notices = []
        self.import_ok = True
        self.content_dir = ""
        self.report_path = ""


def port_maps(cfg, ctx, jobs, parallel=PARALLEL_PORTS, on_state=None):
    """Ports a list of maps: jobs = [(map path, addon name or "")] ("" = the map's name). Maps of
    different addons are ported PARALLEL_PORTS at a time, maps of the same addon one after the
    other. The log lines of a map start with its name, its table rows carry it, and every map
    writes its own report.txt. on_state(index, state, seconds) is told when a map starts
    ("running") and ends ("done", "failed", "cancelled")."""
    import concurrent.futures
    import threading
    import traceback
    on_state = on_state or (lambda i, state, sec=0.0: None)
    jobs = [(p, sanitize_name(a) if a else sanitize_name(os.path.splitext(os.path.basename(p))[0]))
            for p, a in jobs]
    names = [sanitize_name(os.path.splitext(os.path.basename(p))[0]) for p, _a in jobs]
    groups = {}
    for i, (_p, addon) in enumerate(jobs):
        groups.setdefault(addon.lower(), []).append(i)
    parallel = max(1, min(int(parallel or 1), MAX_PARALLEL, len(groups)))
    batch = BatchReport()
    lock = threading.Lock()
    fractions = [0.0] * len(jobs)
    many = len(jobs) > 1
    if many:
        ctx.log(t("p_batch", n=len(jobs), k=parallel), "head")

    def one(i):
        path, addon = jobs[i]
        name = names[i]

        def log(msg, tag="info"):
            ctx._raw_log(f"[{name}] {msg}" if many else msg, tag)

        def progress(f, text=""):
            with lock:
                fractions[i] = f
                total = sum(fractions) / len(fractions)
            ctx.progress(total, (f"{name}: {text}" if many else text) if text else "")

        def result(kind, rname, status, detail, source):
            ctx._result(kind, rname, status, detail, source, name)

        child = JobContext(log, progress, result, ctx.ask, ctx.cancel, ctx.choose)
        child.begin()
        t0 = time.time()
        on_state(i, "running")
        try:
            rep = port_map(cfg, child, path, addon)
            progress(1.0)
            on_state(i, "done", time.time() - t0)
            return rep, None, time.time() - t0
        except Cancelled:
            write_failed_report(child, t("cancelled"))
            on_state(i, "cancelled")
            raise
        except ToolError as e:
            write_failed_report(child, str(e))
            log(str(e), "err")
            on_state(i, "failed")
            return None, str(e), time.time() - t0
        except Exception as e:  # noqa: BLE001 - one broken map does not stop the others
            tb = traceback.format_exc()
            log(tb, "err")
            write_failed_report(child, t("unexpected_error", e=e), tb)
            on_state(i, "failed")
            return None, t("unexpected_error", e=e), time.time() - t0
        finally:
            progress(1.0)
            child.end()

    def chain(indices):
        # the maps of one addon, one after the other
        out = []
        for i in indices:
            if ctx.cancel is not None and ctx.cancel.is_set():
                on_state(i, "cancelled")
                continue
            out.append((i,) + one(i))
        return out

    cancelled = False
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = [pool.submit(chain, idx) for idx in groups.values()]
        for fut in concurrent.futures.as_completed(futures):
            try:
                results += fut.result()
            except Cancelled:
                cancelled = True
    for i, rep, err, sec in sorted(results):
        batch.reports.append((jobs[i][0], rep, err))
        if rep is not None:
            batch.content_dir = rep.content_dir
            batch.report_path = rep.report_path or batch.report_path
            batch.notices += [f"{names[i]}: {n}" if many else n for n in rep.notices]
            if rep.import_ok is False:
                batch.import_ok = False
            if many:
                ctx.log(t("p_batch_map_done", map=names[i], sec=sec), "ok")
        elif many:
            ctx.log(t("p_batch_map_fail", map=names[i], e=err), "err")
    if cancelled:
        raise Cancelled()
    ok = sum(1 for _p, r, _e in batch.reports if r is not None)
    if many:
        ctx.log(t("p_batch_done", ok=ok, n=len(jobs)), "head" if ok == len(jobs) else "warn")
    elif not ok and batch.reports:
        # one map that failed: the job fails with its error, like a single port
        raise ToolError(batch.reports[0][2] or t("unexpected_error", e="?"))
    return batch


def _beam_notice(report, stats):
    """env_beam / env_laser crash CS2, and particles have no Toggle input: the user is told
    what happened to them."""
    parts = []
    if stats.get("beams_found"):
        parts.append(t("notice_beams", n=stats["beams_found"], p=stats.get("beams", 0),
                       r=stats.get("beams_removed", 0)))
    if stats.get("unsupported_inputs"):
        parts.append(t("notice_inputs", n=stats["unsupported_inputs"]))
    if parts:
        report.notices.append("\n\n".join(parts))


def _import_map(cfg, ctx, report, valve, sources, work, content_root, materials, skyname,
                map_name, addon, cs2_ok, aliases=None):
    """Imports the map as a .vmap. Returns the path of the new _refs.txt (or None)."""
    if not cs2_ok:
        ctx.log(t("p_import_skip_cs2"), "warn")
        return None
    if not valve.has_source1import:
        ctx.log(t("p_import_skip_tools"), "warn")
        report.import_ok = False
        return None
    if " " in content_root:
        ctx.log(t("p_import_space"), "err")
        report.import_ok = False
        return None

    ctx.log(t("p_import"), "head")
    gi, n = s1stub.build_stub_game(work, sources, sorted(materials), skyname, aliases)
    ctx.log(t("p_stub", n=n), "dim")
    maps_dir = os.path.join(cfg["cs2_dir"], "content", "csgo_addons", addon, "maps")
    vmap = os.path.join(maps_dir, f"{map_name}.vmap")
    refs = os.path.join(maps_dir, f"{map_name}_refs.txt")
    started = time.time() - 2
    try:
        ok, _out = valve.import_map(gi, content_root, map_name, addon)
    except ToolError as e:
        ctx.log(str(e), "err")
        ok = False
    finally:
        s1stub.remove_stub_game(work)
    report.import_ok = ok and os.path.isfile(vmap)
    if ok and os.path.isfile(vmap):
        ctx.log(t("p_import_ok", path=vmap), "ok")
        parts = len(vmapmod.map_files(maps_dir, map_name))
        ctx.detail(t("d_import_files", n=parts, mb=os.path.getsize(vmap) / 1048576.0))
    elif ok:
        ctx.log(t("p_import_novmap"), "warn")
    else:
        ctx.log(t("p_import_fail"), "err")
    if os.path.isfile(refs) and os.path.getmtime(refs) >= started:
        return refs
    return None


# ---------------------------------------------------------------------------
# Missing asset completion (from refs / log / list files)
# ---------------------------------------------------------------------------

_VMAP_REF_RE = re.compile(rb"((?:materials|models)/[A-Za-z0-9_\-./ +()]+?\.(?:vmat|vmdl))")
_SKY_FACE_RE = re.compile(r"^skybox/(.+?)(?:_hdr)?(?:up|dn|lf|rt|ft|bk)$")
_OWN_MATERIALS = ("skybox/skybox", "skybox/skybox_moondome", "cs2porter/black", "cs2porter/cable_black")


def read_vmap_refs(path):
    """Materials and models a .vmap (text or binary) uses, as Source 1 names."""
    with open(path, "rb") as f:
        data = f.read()
    mats, mdls = {}, {}
    for m in _VMAP_REF_RE.finditer(data):
        p = m.group(1).decode("latin-1").lower()
        if p.endswith(".vmat"):
            name = vmfmod.normalize_material(p[:-5])
            if name not in _OWN_MATERIALS:
                mats.setdefault(name, set()).add("ref")
        else:
            mdls[norm_model(p[:-5] + ".mdl")] = 1
    return mats, mdls


def _sky_from_refs(mats):
    """Sky name from the skybox face materials of a list (skybox/<name>up ...); the faces are
    removed from mats, since the skybox step makes them."""
    names = {}
    for k in list(mats):
        m = _SKY_FACE_RE.match(k)
        if m:
            names[m.group(1)] = names.get(m.group(1), 0) + 1
            del mats[k]
    return max(names, key=names.get) if names else ""


FIND_LIMIT = 2000
_FIND_KINDS = {".vmt": "material", ".vmat": "material", ".vtf": "texture", ".tga": "texture", ".png": "texture",
               ".jpg": "texture", ".jpeg": "texture", ".psd": "texture", ".mdl": "model", ".vvd": "model",
               ".vtx": "model", ".phy": "model", ".qc": "model", ".smd": "model", ".wav": "sound", ".mp3": "sound",
               ".vtex": "texture", ".vmdl": "model", ".fbx": "model", ".dmx": "model", ".vsnd": "sound"}
CS2_GAME = "Counter-Strike 2"
# CS2 packages searched by Find a file (compiled files: .vmat_c, .vmdl_c ...)
_CS2_VPKS = ("game/csgo/pak01_dir.vpk", "game/core/pak01_dir.vpk")


def _find_kind(path):
    ext = os.path.splitext(path)[1]
    if ext.endswith("_c"):
        ext = ext[:-2]
    return _FIND_KINDS.get(ext, "file")


def _source_game(src):
    """Name of the game (or folder) a source of find_files stands for."""
    game = getattr(src, "game", "")
    if game:
        return game
    if src.role == "embedded":
        return t("src_embedded")
    root = getattr(src, "root", "") or getattr(src, "dir_path", "")
    if src.role == "resources" and root:
        return os.path.basename(os.path.normpath(root))
    return os.path.basename(os.path.normpath(root)) if root else src.label


def find_files(cfg, ctx, query, exact=False, extra=None):
    """Looks for files by name in every source: the extra folder or .bsp, the resource folders
    and the installed games (with their sound packages). exact: the file name (with or without
    its extension) or the whole path is the query; else every path that contains it."""
    t0 = time.time()
    q = query.strip().replace("\\", "/").lower().strip("/")
    report = Report()
    if not q:
        return report

    def match(path):
        if not exact:
            return q in path
        name = path.rsplit("/", 1)[-1]
        return q in (path, path.rsplit(".", 1)[0], name, name.rsplit(".", 1)[0])

    lists = []
    if extra and extra.lower().endswith(".bsp") and os.path.isfile(extra):
        try:
            zf = bspmod.BSPInfo(extra).pakfile()
            if zf:
                lists.append((t("src_embedded"), extra,
                              [z.filename.replace("\\", "/").lower() for z in zf.infolist() if not z.is_dir()],
                              os.path.basename(extra)))
        except Exception as e:  # noqa: BLE001
            ctx.log(t("p_extract_fail", e=e), "warn")
    extra_dirs = [extra] if extra and os.path.isdir(extra) else []
    sources = open_sources(cfg, ctx, None, extra_dirs)
    found = 0
    seen = set()            # a file of the extra folder is listed once (it is in two sources)
    try:
        ctx.log(t("find_start", q=query.strip(), mode=t("find_exact" if exact else "find_contains")), "head")
        all_src = sources.sources + sources.sound_sources
        for s in all_src:
            try:
                names = list(s.index) if isinstance(s, DirSource) else list(s.arc.entries)
            except (OSError, VPKError):
                continue
            lists.append((s.label, s, names, _source_game(s)))
        # CS2 itself (its compiled files)
        for rel in _CS2_VPKS:
            p = os.path.join(cfg.get("cs2_dir") or "", *rel.split("/"))
            if not cfg.get("cs2_dir") or not os.path.isfile(p):
                continue
            try:
                arc = VPKArchive(p)
                names = list(arc.entries)
                arc.close()
            except (OSError, VPKError) as e:
                ctx.log(t("src_vpk_fail", p=p, e=e), "warn")
                continue
            src = VPKSource(p, t("src_game_vpk", game=CS2_GAME, file=rel.split("/")[1] + "/" + rel.split("/")[2]))
            lists.append((src.label, src, names, CS2_GAME))
        for i, (label, src, names, game) in enumerate(lists):
            ctx.check()
            ctx.progress((i + 1) / max(len(lists), 1), label[:60])
            for n in names:
                if not match(n):
                    continue
                if isinstance(src, DirSource):
                    where = src.path(n) or src.root
                    key = os.path.normcase(where)
                    if key in seen:
                        continue
                    seen.add(key)
                elif isinstance(src, VPKSource):
                    where = src.dir_path
                else:
                    where = src
                found += 1
                if found > FIND_LIMIT:
                    continue
                ctx.result(_find_kind(n), n, "found", label, where, game)
    finally:
        sources.close()
    if found > FIND_LIMIT:
        ctx.log(t("find_limit", n=found, shown=FIND_LIMIT), "warn")
    ctx.log(t("find_done", n=found, sec=time.time() - t0), "ok" if found else "warn")
    return report


def _free_name(path, taken):
    """path, or path with _2, _3 ... before the extension when another export already used it."""
    base, ext = os.path.splitext(path)
    n, out = 1, path
    while out.lower() in taken:
        n += 1
        out = f"{base}_{n}{ext}"
    taken.add(out.lower())
    return out


def export_files(ctx, items, out_dir, flat=False, cfg=None, extra=None):
    """Copies files found by find_files into out_dir, keeping their game paths (flat: only the
    files, side by side). items: [(path, where)]; where is the file itself, the package (.vpk)
    or the .bsp it is in. With cfg, the files they use (textures of a material, parts of a
    model ...) are looked up and the user picks which go along (extra: the search's extra
    folder or .bsp). out_dir can be a function that asks for the folder after that (None: stop)."""
    t0 = time.time()
    if cfg is not None:
        ctx.log(t("deps_looking"), "info")
        try:
            found = depsmod.find(cfg, items, extra, ctx.check)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - the export itself still works
            found = []
            ctx.log(t("deps_fail", e=e), "warn")
        if found:
            ctx.detail(t("deps_found", n=len(found), missing=sum(1 for d in found if not d["where"])))
            chosen = ctx.choose(t("deps_title"), t("deps_msg"), found)
            if chosen is None:
                ctx.log(t("export_cancelled"), "warn")
                return None
            chosen = [d for d in chosen if d["where"]]
            if chosen:
                ctx.log(t("deps_added", n=len(chosen)), "info")
                items = list(items) + [(d["rel"], d["where"]) for d in chosen]
        else:
            ctx.detail(t("deps_none"))
    if callable(out_dir):
        out_dir = out_dir()
        if not out_dir:
            ctx.log(t("export_cancelled"), "warn")
            return None
    ctx.log(t("export_start", n=len(items), out=out_dir), "head")
    archives, paks = {}, {}
    taken = set()
    done = 0
    try:
        for i, (rel, where) in enumerate(items):
            ctx.check()
            ctx.progress((i + 1) / max(len(items), 1), rel[-60:])
            rel = rel.replace("\\", "/").strip("/")
            if flat:
                dst = _free_name(os.path.join(out_dir, rel.split("/")[-1]), taken)
            else:
                dst = os.path.join(out_dir, *rel.split("/"))
            try:
                low = where.lower()
                if os.path.isfile(where) and low.endswith(".vpk"):
                    if where not in archives:
                        archives[where] = VPKArchive(where)
                    data = archives[where].read(rel.lower())
                elif os.path.isfile(where) and low.endswith(".bsp"):
                    if where not in paks:
                        zf = bspmod.BSPInfo(where).pakfile()
                        paks[where] = (zf, {z.filename.replace("\\", "/").lower(): z.filename
                                            for z in zf.infolist()} if zf else {})
                    zf, names = paks[where]
                    data = zf.read(names[rel.lower()])
                elif os.path.isfile(where):
                    with open(where, "rb") as f:
                        data = f.read()
                else:
                    raise OSError(t("export_gone"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(data)
                done += 1
                ctx.detail(t("export_file", f=rel))
            except Exception as e:  # noqa: BLE001
                ctx.log(t("export_fail", f=rel, e=e), "err")
    finally:
        for arc in archives.values():
            arc.close()
    ctx.log(t("export_done", n=done, out=out_dir, sec=time.time() - t0), "ok" if done == len(items) else "warn")
    return None


def complete_missing(cfg, ctx, source, addon_name=None, out_dir=None, extra_dir=None):
    """source: {"vmap": path} (every asset the map uses is checked) or {"text": pasted text}
    (paths in a compile / console log or any list)."""
    t0 = time.time()
    report = Report()
    report.input_path = source.get("vmap") or ""
    report.extra_path = extra_dir or ""
    ctx.cfg = cfg
    # the table only lists what was missing: found and added, or still not found
    ctx.only_changes = True
    if source.get("vmap"):
        mats, mdls = read_vmap_refs(source["vmap"])
        ctx.log(t("p_ref_file", file=os.path.basename(source["vmap"]), mats=len(mats), mdls=len(mdls)), "info")
    else:
        mats, mdls = parse_reference_text(source.get("text", ""))
        ctx.log(t("p_ref_text", mats=len(mats), mdls=len(mdls)), "info")
    skyname = _sky_from_refs(mats)
    if extra_dir and extra_dir.lower().endswith(".bsp") and os.path.isfile(extra_dir):
        try:
            skyname = bspmod.BSPInfo(extra_dir).worldspawn_keys().get("skyname", "").strip() or skyname
        except Exception:  # noqa: BLE001
            pass
    if not mats and not mdls and not skyname:
        ctx.log(t("p_ref_none"), "warn")
        return report

    cs2_dir = cfg.get("cs2_dir", "")
    cs2_ok = config.is_valid_cs2(cs2_dir)
    valve = ValveTools(cs2_dir if cs2_ok else "", ctx.log, ctx.cancel)
    if out_dir:
        content_dir, game_dir = out_dir, None
        os.makedirs(content_dir, exist_ok=True)
    elif cs2_ok and addon_name:
        content_dir, game_dir, created = valve.ensure_addon(sanitize_name(addon_name))
        if created:
            ctx.log(t("p_new_addon", addon=addon_name), "ok")
    else:
        raise ToolError(t("p_no_target"))
    report.content_dir = content_dir
    work = os.path.join(config.work_root(), "_missing")
    staging = os.path.join(work, "mdl_staging")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    report.work_dir = work
    ctx.pending_report = (report, work, t_en("report_title_missing"))
    ctx.log(t("p_target", path=content_dir), "dim")
    # extra source: a folder or the map's .bsp file (for its embedded files)
    embedded, extra_dirs = None, []
    if extra_dir and extra_dir.lower().endswith(".bsp") and os.path.isfile(extra_dir):
        embedded = os.path.join(work, "embedded")
        shutil.rmtree(embedded, ignore_errors=True)
        ctx.log(t("p_extract"), "head")
        try:
            bspmod.extract_pakfile(extra_dir, embedded, smart=True, log=ctx.log)
        except Exception as e:  # noqa: BLE001
            ctx.log(t("p_extract_fail", e=e), "warn")
            embedded = None
    elif extra_dir and os.path.isdir(extra_dir):
        extra_dirs = [extra_dir]
    sources = open_sources(cfg, ctx, embedded, extra_dirs)
    try:
        # writing into an addon also makes the skybox, like a port does
        if game_dir and skyname:
            make_skybox(cfg, ctx, report, sources, content_dir, skyname)
        elif game_dir:
            ctx.log(t("p_sky_unknown"), "dim")
        tex_lights = None
        if extra_dir and extra_dir.lower().endswith(".bsp") and os.path.isfile(extra_dir):
            try:
                tex_lights = _texture_lights(ctx, bspmod.BSPInfo(extra_dir))
            except Exception:  # noqa: BLE001 - an unreadable .bsp only costs the glow
                tex_lights = None
        convert_assets(cfg, ctx, report, sources, mats, mdls, content_dir, game_dir, staging,
                       valve if cs2_ok else None, 0.05, 0.92, texture_lights=tex_lights)
    finally:
        sources.close()
    _summary(ctx, report)
    if cs2_ok and game_dir:
        compile_assets(cfg, ctx, valve, content_dir, game_dir, work)
    _finish(ctx, report, work, t_en("report_title_missing"), t0)
    return report
