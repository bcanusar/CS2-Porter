"""External tools: the map decompiler and the CS2 map / model importers."""

import os
import queue
import shutil
import subprocess
import threading
import time

from . import config
from .i18n import t

CREATE_NO_WINDOW = 0x08000000


class ToolError(Exception):
    pass


class Cancelled(Exception):
    pass


_recorder = threading.local()
# the Source 1 importers share files under CS2's folders: maps ported at the same time run them
# one after the other
_IMPORT_LOCK = threading.Lock()


class _Importing:
    """Holds the importer lock; waiting for it can still be stopped."""

    def __init__(self, cancel):
        self.cancel = cancel

    def __enter__(self):
        while not _IMPORT_LOCK.acquire(timeout=0.5):
            if self.cancel is not None and self.cancel.is_set():
                raise Cancelled()
        return self

    def __exit__(self, *exc):
        _IMPORT_LOCK.release()
        return False


def record_runs(fn):
    """fn(args, return_code, seconds, output_lines) is called after every command this thread
    runs (return_code None: it was stopped or timed out); None stops it. For report.txt."""
    _recorder.fn = fn


def run_process(args, cwd=None, log=None, cancel=None, auto_yes=False,
                idle_timeout=300, total_timeout=None, echo=True, echo_filter=None):
    """Runs the command and logs the output line by line. Returns (return_code, output)."""
    rec = getattr(_recorder, "fn", None)
    if rec is None:
        return _run_process(args, cwd, log, cancel, auto_yes, idle_timeout, total_timeout, echo, echo_filter)
    t0 = time.time()
    lines = []
    try:
        code, out = _run_process(args, cwd, log, cancel, auto_yes, idle_timeout, total_timeout, echo,
                                 echo_filter, lines)
    except BaseException:
        rec(args, None, time.time() - t0, lines)
        raise
    rec(args, code, time.time() - t0, lines)
    return code, out


def _run_process(args, cwd=None, log=None, cancel=None, auto_yes=False,
                 idle_timeout=300, total_timeout=None, echo=True, echo_filter=None, out_lines=None):
    log = log or (lambda m, t="info": None)
    try:
        proc = subprocess.Popen(
            args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE, creationflags=CREATE_NO_WINDOW,
        )
    except OSError as e:
        raise ToolError(t("vt_cant_run", exe=args[0], e=e))

    q = queue.Queue()

    def reader():
        fd = proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            q.put(chunk)
        q.put(None)

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    buf = b""
    if out_lines is None:
        out_lines = []
    last_out = time.time()
    start = time.time()
    done = False
    while not done:
        if cancel is not None and cancel.is_set():
            _kill_tree(proc)
            raise Cancelled()
        try:
            chunk = q.get(timeout=1.0)
        except queue.Empty:
            chunk = b""
            if auto_yes and proc.poll() is None:
                try:
                    proc.stdin.write(b"y\n")
                    proc.stdin.flush()
                except OSError:
                    pass
            if time.time() - last_out > idle_timeout:
                _kill_tree(proc)
                raise ToolError(t("vt_idle", sec=idle_timeout))
            if total_timeout and time.time() - start > total_timeout:
                _kill_tree(proc)
                raise ToolError(t("vt_timeout"))
            continue
        if chunk is None:
            done = True
        else:
            last_out = time.time()
            buf += chunk
        while b"\n" in buf or (done and buf):
            if b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
            else:
                line, buf = buf, b""
            s = line.decode("utf-8", errors="replace").rstrip("\r").strip()
            if not s:
                continue
            out_lines.append(s)
            if echo and (echo_filter is None or echo_filter(s)):
                log(s, "tool")
    proc.wait()
    return proc.returncode, "\n".join(out_lines)


def _kill_tree(proc):
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    except OSError:
        pass
    try:
        proc.kill()
    except OSError:
        pass


def is_process_running(image_name):
    try:
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/NH"],
                           capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        return image_name.lower() in r.stdout.lower()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Map decompiler
# ---------------------------------------------------------------------------

def bspsource_command(bspsource_dir):
    java = os.path.join(bspsource_dir, "bin", "java.exe")
    if os.path.isfile(java) and os.path.isfile(os.path.join(bspsource_dir, "lib", "modules")):
        return [java, "-m", "info.ata4.bspsrc.app/info.ata4.bspsrc.app.src.BspSourceLauncher"]
    jar = os.path.join(bspsource_dir, "bspsrc.jar")
    if os.path.isfile(jar) and os.path.isfile(java):
        return [java, "-jar", jar]
    return None


def decompile_bsp(bsp, out_vmf, log=None, cancel=None, bspsource_dir=None):
    bspsource_dir = bspsource_dir or config.find_bspsource()
    cmd = bspsource_command(bspsource_dir) if bspsource_dir else None
    if not cmd:
        raise ToolError(t("vt_no_bspsource", path=config.BSPSOURCE_DIR))
    if os.path.isfile(out_vmf):
        os.remove(out_vmf)
    os.makedirs(os.path.dirname(out_vmf), exist_ok=True)
    args = cmd + [bsp, "-o", out_vmf]
    code, out = run_process(args, log=log, cancel=cancel, idle_timeout=600, echo=False)
    if not os.path.isfile(out_vmf):
        tail = "\n".join(out.splitlines()[-8:])
        raise ToolError(t("vt_decompile_fail", code=code, tail=tail))
    return out


# ---------------------------------------------------------------------------
# CS2 tools
# ---------------------------------------------------------------------------

class ValveTools:
    def __init__(self, cs2_dir, log=None, cancel=None):
        self.cs2_dir = cs2_dir or ""
        self.log = log or (lambda m, t="info": None)
        self.cancel = cancel
        self.bin = os.path.join(self.cs2_dir, "game", "bin", "win64")

    @property
    def has_cs2_tools(self):
        return os.path.isfile(os.path.join(self.bin, "cs_mdl_import.exe"))

    @property
    def has_source1import(self):
        return os.path.isfile(os.path.join(self.bin, "source1import.exe"))

    def exe(self, name):
        p = os.path.join(self.bin, name)
        if not os.path.isfile(p):
            raise ToolError(t("vt_no_exe", name=name, path=self.bin))
        return p

    # --- addon -------------------------------------------------------------------
    def ensure_addon(self, addon):
        """Returns (content_dir, game_dir, created). A new addon gets the same default
        folders and files that Hammer copies from addon_template."""
        created = False
        dirs = []
        for side in ("content", "game"):
            dst = os.path.join(self.cs2_dir, side, "csgo_addons", addon)
            dirs.append(dst)
            if os.path.isdir(dst) and [n for n in os.listdir(dst) if n.lower() != "addoninfo.txt"]:
                continue
            created = True
            tmpl = os.path.join(self.cs2_dir, side, "csgo_addons", "addon_template")
            if os.path.isdir(tmpl):
                copy_addon_template(tmpl, dst, addon)
            else:
                for sub in ADDON_FOLDERS[side]:
                    os.makedirs(os.path.join(dst, sub), exist_ok=True)
        info = os.path.join(dirs[1], "addoninfo.txt")
        if not os.path.isfile(info):
            with open(info, "w", encoding="utf-8", newline="\n") as f:
                f.write(ADDONINFO)
        return dirs[0], dirs[1], created

    # --- dmxconvert (.vmap binary <-> text) -----------------------------------------
    @property
    def has_dmxconvert(self):
        return os.path.isfile(os.path.join(self.bin, "dmxconvert.exe"))

    def dmxconvert(self, src, dst, encoding):
        """Converts a DMX file (.vmap) to 'keyvalues2' (text) or 'binary'. True on success."""
        if os.path.isfile(dst):
            os.remove(dst)
        args = [self.exe("dmxconvert.exe"), "-i", os.path.normpath(src), "-o", os.path.normpath(dst),
                "-oe", encoding]
        code, out = run_process(args, cwd=os.path.dirname(os.path.normpath(dst)), log=self.log,
                                cancel=self.cancel, idle_timeout=300, echo=False)
        return os.path.isfile(dst) and os.path.getsize(dst) > 0, out

    # --- model importer -----------------------------------------------------------
    def cs_mdl_import(self, input_dir, out_dir, mdl):
        args = [self.exe("cs_mdl_import.exe"), "-nop4", "-i", os.path.normpath(input_dir),
                "-o", os.path.normpath(out_dir), mdl.replace("/", "\\")]
        # run in the temp folder so crash dumps (.mdmp) do not land in the program folder
        with _Importing(self.cancel):
            code, out = run_process(args, cwd=os.path.normpath(input_dir), log=self.log, cancel=self.cancel,
                                    idle_timeout=300, echo=False)
        ok = "successfully converted" in out.lower()
        return ok, out

    # --- asset compiler (materials and models only, never the map) -----------
    @property
    def has_resourcecompiler(self):
        return os.path.isfile(os.path.join(self.bin, "resourcecompiler.exe"))

    def compile_resources(self, files, list_path):
        """Compiles .vmat / .vmdl files of an addon's content folder into its game folder
        (vmat_c / vmdl_c and their textures). Returns (ok, failed_count, output)."""
        with open(list_path, "w", encoding="utf-8") as f:
            f.write("\n".join(os.path.normpath(p) for p in files) + "\n")
        args = [self.exe("resourcecompiler.exe"), "-nop4", "-filelist", os.path.normpath(list_path)]
        code, out = run_process(args, cwd=self.bin, log=self.log, cancel=self.cancel, idle_timeout=900,
                                echo=False)
        failed = 0
        for line in out.splitlines()[::-1]:
            low = line.lower()
            if " compiled," in low and " failed" in low:
                try:
                    failed = int(low.split(" compiled,")[1].split("failed")[0].strip())
                except ValueError:
                    pass
                break
        return code == 0 or failed == 0, failed, out

    # --- map importer (map only) ---------------------------------------------
    def import_map(self, gameinfo_dir, content_root, map_name, addon):
        """gameinfo_dir: the 'csgo' folder made by s1stub.build_stub_game."""
        src1import = self.exe("source1import.exe")
        cwd = os.path.join(self.cs2_dir, "game", "csgo", "import_scripts")
        args = [src1import, "-retail", "-nop4", "-nop4sync",
                "-src1gameinfodir", os.path.normpath(gameinfo_dir),
                "-src1contentdir", os.path.normpath(content_root),
                "-s2addon", addon, "-game", "csgo", f"maps\\{map_name}.vmf"]
        with _Importing(self.cancel):
            code, out = run_process(args, cwd=cwd if os.path.isdir(cwd) else None, log=self.log,
                                    cancel=self.cancel, auto_yes=True, idle_timeout=900, echo=False)
        return "ok:" in out.lower(), out

    def import_particles(self, gameinfo_dir, addon):
        """Source 1 particle files (particles/*.pcf of the gameinfo folder, listed in its
        particles_manifest.txt) -> particles/<pcf name>/<system>.vpcf of the addon."""
        src1import = self.exe("source1import.exe")
        cwd = os.path.join(self.cs2_dir, "game", "csgo", "import_scripts")
        args = [src1import, "-retail", "-nop4", "-nop4sync",
                "-src1gameinfodir", os.path.normpath(gameinfo_dir),
                "-s2addon", addon, "-game", "csgo", "particles\\*.pcf"]
        with _Importing(self.cancel):
            code, out = run_process(args, cwd=cwd if os.path.isdir(cwd) else None, log=self.log,
                                    cancel=self.cancel, auto_yes=True, idle_timeout=600, echo=False)
        return "ok:" in out.lower(), out


# folders of a new addon when CS2's addon_template is missing
ADDON_FOLDERS = {
    "content": ("maps", "postprocess", "soundevents", "sounds"),
    "game": ("cfg/maps", "maps", "postprocess", "soundevents", "sounds"),
}
# addoninfo.txt as Hammer writes it for a new addon
ADDONINFO = ("<!-- kv3 encoding:text:version{e21c7f3c-8a33-41c5-9977-a76d3a32aa0d} "
             "format:generic:version{7412167c-06e9-4698-aff2-e63eb59037e7} -->\n{\n}\n")
_TEMPLATE_NAME = "xxx_mapname_xxx"
# template files Hammer does not copy into a new addon
_TEMPLATE_SKIP = {"addoninfo.txt", "readonly_tools_asset_info.bin"}


def copy_addon_template(tmpl, dst, addon):
    """Copies addon_template like Hammer does: xxx_mapname_xxx in file names becomes the addon name."""
    for dp, _dn, fn in os.walk(tmpl):
        rel = os.path.relpath(dp, tmpl)
        out = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(out, exist_ok=True)
        for f in fn:
            if rel == "." and f.lower() in _TEMPLATE_SKIP:
                continue
            target = os.path.join(out, f.replace(_TEMPLATE_NAME, addon))
            if not os.path.exists(target):
                shutil.copyfile(os.path.join(dp, f), target)


def copy_tree(src, dst):
    count = 0
    for dp, _dn, fn in os.walk(src):
        rel = os.path.relpath(dp, src)
        out = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(out, exist_ok=True)
        for f in fn:
            shutil.copyfile(os.path.join(dp, f), os.path.join(out, f))
            count += 1
    return count
