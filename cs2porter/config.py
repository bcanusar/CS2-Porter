"""Settings: load / save and automatic folder detection."""

import json
import os
import re
import sys

# the .exe keeps its settings, cache and tools next to itself
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
CACHE_DIR = os.path.join(APP_DIR, "cache")
TOOLS_DIR = os.path.join(APP_DIR, "tools")
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

BSPSOURCE_DIR = os.path.join(TOOLS_DIR, "BSPSource")
# optional: only used for the few VTF formats the program can not read itself
VTFCMD = os.path.join(TOOLS_DIR, "VTFCmd", "VTFCmd.exe")


def _is_bspsource(d):
    bin_java = os.path.join(d, "bin", "java.exe")
    return (os.path.isfile(bin_java) and os.path.isfile(os.path.join(d, "lib", "modules"))) \
        or os.path.isfile(os.path.join(d, "bspsrc.jar"))


def find_bspsource(root=BSPSOURCE_DIR, depth=2):
    """The decompiler folder: tools/BSPSource itself or a folder up to two levels below it
    (an extracted release zip often has a folder of its own). '' when there is none."""
    if not os.path.isdir(root):
        return ""
    if _is_bspsource(root):
        return root
    if depth > 0:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return ""
        for n in names:
            p = os.path.join(root, n)
            if os.path.isdir(p):
                found = find_bspsource(p, depth - 1)
                if found:
                    return found
    return ""

# search order of the installed Source 1 games (folder, mod); other games come after these
KNOWN_S1_GAMES = [
    ("Counter-Strike Source", "cstrike"),
    ("csgo legacy", "csgo"),
    ("Half-Life 2", "hl2"),
    ("Half-Life 2", "episodic"),
    ("Half-Life 2", "ep2"),
    ("Portal 2", "portal2"),
    ("GarrysMod", "garrysmod"),
    ("Team Fortress 2", "tf"),
]

DEFAULTS = {
    "lang": "en",
    "theme": "dark",
    "maximized": False,
    "cs2_dir": "",
    "last_bsp_dir": "",
    "last_tool": "convert",
    "last_export_dir": "",
    "tool_paths": {},
    # folders with Source 1 files (materials/, models/ ...) searched before the installed games
    "resource_dirs": [],
    "opts": {
        "overwrite": False,
        "convert_cs2_existing": False,
        "fix_light_brightness": True,
        "bhop_script": True,
        "bhop_download": True,
        "bhop_blocks": True,
        "compile_assets": True,
        "split_triggers": True,
        "light_probes": True,
        "port_particles": True,
        "post_processing": True,
    },
    # the map list of the Port Map tab: [[map path, addon name], ...]
}

# folders below a resource folder that hold game files
RESOURCE_SUBDIRS = ("materials", "models", "sound", "scripts")


_VPK_PART_RE = re.compile(r"_\d{3}\.vpk$", re.I)


def _vpks(d, names):
    # packages are mounted like the files of a Source 1 custom folder; the numbered parts
    # (x_000.vpk ...) belong to their x_dir.vpk
    return [os.path.join(d, n) for n in sorted(names)
            if n.lower().endswith(".vpk") and not _VPK_PART_RE.search(n) and os.path.isfile(os.path.join(d, n))]


def resource_entries(folders, depth=3):
    """What the resource folders hold, as a Source 1 game would mount it: ("dir", root) for every
    game-like folder (it holds materials/, models/, sound/ ...) and ("vpk", path) for every
    package, also a few levels down: 'Resources' finds 'Resources/Games/Counter-Strike Source'.
    A picked materials/ (or models/ ...) folder stands for the folder above it."""
    out = []

    def walk(d, level):
        try:
            names = os.listdir(d)
        except OSError:
            return
        low = {n.lower() for n in names}
        if any(s in low for s in RESOURCE_SUBDIRS):
            out.append(("dir", d))
            out.extend(("vpk", v) for v in _vpks(d, names))
            return
        out.extend(("vpk", v) for v in _vpks(d, names))
        if level >= depth:
            return
        for n in sorted(names):
            p = os.path.join(d, n)
            if os.path.isdir(p):
                walk(p, level + 1)

    for f in folders or ():
        if not f or not os.path.isdir(f):
            continue
        f = os.path.normpath(f)
        if os.path.basename(f).lower() in RESOURCE_SUBDIRS:
            f = os.path.dirname(f)
        walk(f, 0)
    seen, uniq = set(), []
    for kind, p in out:
        if os.path.normcase(p) not in seen:
            seen.add(os.path.normcase(p))
            uniq.append((kind, p))
    return uniq


def resource_roots(folders, depth=3):
    """Paths of resource_entries() (folders and packages)."""
    return [p for _kind, p in resource_entries(folders, depth)]

BHOP_SCRIPT_PAGE = "https://steamcommunity.com/sharedfiles/filedetails/?id=3775382649"
AUTHOR_PAGE = "https://steamcommunity.com/id/tehlikeli91/"
DONATE_PAGE = "https://steamcommunity.com/tradeoffer/new/?partner=1538890593&token=F7uoB0t0"
YOUTUBE_PAGE = "https://www.youtube.com/@burak.016"
GITHUB_PAGE = "https://github.com/bcanusar"

# latest bhop script (Google Drive file); offline the saved copy (cache) or the bundled copy in
# assets/ is used
BHOP_SCRIPT_ID = "1HeA1_yWUsaUYLvg1P6e4DTizbyHxgtfy"
BHOP_SCRIPT_URLS = (
    f"https://drive.google.com/uc?export=download&id={BHOP_SCRIPT_ID}",
    f"https://drive.usercontent.google.com/download?id={BHOP_SCRIPT_ID}&export=download&confirm=t",
)


def _merge(base, override):
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    data = {}
    if os.path.isfile(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
    data = {k: v for k, v in data.items() if k in DEFAULTS}
    if isinstance(data.get("opts"), dict):
        data["opts"] = {k: v for k, v in data["opts"].items() if k in DEFAULTS["opts"]}
    cfg = _merge(DEFAULTS, data)
    autodetect(cfg, only_missing=True)
    return cfg


def save(cfg):
    data = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Auto detection
# ---------------------------------------------------------------------------

def steam_libraries():
    paths = []
    steam = None
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for name in ("SteamPath", "InstallPath"):
                        try:
                            steam = winreg.QueryValueEx(k, name)[0]
                            break
                        except OSError:
                            continue
                if steam:
                    break
            except OSError:
                continue
    except ImportError:
        pass
    candidates = [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]
    if steam:
        candidates.insert(0, steam)
    seen = set()

    def add(p):
        p = _real_case(os.path.normpath(p))
        key = os.path.normcase(p)
        if key not in seen and os.path.isdir(os.path.join(p, "steamapps")):
            seen.add(key)
            paths.append(p)

    for c in candidates:
        if not c:
            continue
        add(c)
        vdf = os.path.join(c, "steamapps", "libraryfolders.vdf")
        if os.path.isfile(vdf):
            try:
                with open(vdf, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
                for p in re.findall(r'"path"\s+"([^"]+)"', text):
                    add(p.replace("\\\\", "\\"))
            except OSError:
                pass
    return paths


def _real_case(path):
    """Converts a Windows path to the real letter case on disk (for display)."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        h = ctypes.windll.kernel32.GetLongPathNameW
        if h(path, buf, 1024):
            short = ctypes.create_unicode_buffer(1024)
            ctypes.windll.kernel32.GetShortPathNameW(path, short, 1024)
            ctypes.windll.kernel32.GetLongPathNameW(short.value, buf, 1024)
            path = buf.value or path
    except Exception:  # noqa: BLE001
        pass
    if len(path) > 1 and path[1] == ":":
        path = path[0].upper() + path[1:]
    return path


def find_cs2():
    for lib in steam_libraries():
        d = os.path.join(lib, "steamapps", "common", "Counter-Strike Global Offensive")
        if is_valid_cs2(d):
            return d
    return ""


def is_valid_cs2(d):
    if not d:
        return False
    gi = os.path.join(d, "game", "csgo", "gameinfo.gi")
    if not os.path.isfile(gi):
        return False
    try:
        with open(gi, "r", encoding="utf-8", errors="replace") as f:
            return "Counter-Strike 2" in f.read()
    except OSError:
        return False


_S1_CACHE = None


def find_s1_gamedirs(prefer=None):
    """gameinfo.txt folders of every installed Source 1 game (steamapps/common/<game>/<mod>).
    prefer: mod folder names searched first, e.g. ("csgo",) for a CS:GO map."""
    global _S1_CACHE
    if _S1_CACHE is None:
        found = []
        for lib in steam_libraries():
            common = os.path.join(lib, "steamapps", "common")
            try:
                games = sorted(os.listdir(common))
            except OSError:
                continue
            for game in games:
                gdir = os.path.join(common, game)
                try:
                    mods = sorted(os.listdir(gdir))
                except OSError:
                    continue
                for mod in mods:
                    d = os.path.join(gdir, mod)
                    if os.path.isfile(os.path.join(d, "gameinfo.txt")) and d not in found:
                        found.append(d)
        _S1_CACHE = found
    order = [m.lower() for m in (prefer or ())] + [m for _g, m in KNOWN_S1_GAMES]

    def rank(d):
        mod = os.path.basename(d).lower()
        return order.index(mod) if mod in order else len(order)

    return sorted(_S1_CACHE, key=rank)


def rescan_s1_games():
    global _S1_CACHE
    _S1_CACHE = None
    return find_s1_gamedirs()


def s1_game_names(gamedirs):
    names = []
    for d in gamedirs:
        n = os.path.basename(os.path.dirname(os.path.normpath(d)))
        if n not in names:
            names.append(n)
    return names


def work_root():
    """Picks a work folder without spaces, since the CS2 tools fail on paths with spaces."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    path = os.path.join(base, "CS2Porter", "work")
    if " " not in path:
        return path
    try:
        import ctypes
        os.makedirs(path, exist_ok=True)
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024) and " " not in buf.value:
            return buf.value
    except Exception:  # noqa: BLE001
        pass
    drive = os.environ.get("SystemDrive", "C:")
    return os.path.join(drive + os.sep, "CS2PorterWork")


def autodetect(cfg, only_missing=False):
    if not only_missing or not is_valid_cs2(cfg.get("cs2_dir")):
        cfg["cs2_dir"] = find_cs2() or cfg.get("cs2_dir", "")
    if not cfg.get("lang"):
        cfg["lang"] = "en"
    return cfg


def list_addons(cs2_dir):
    d = os.path.join(cs2_dir, "content", "csgo_addons")
    if not os.path.isdir(d):
        return []
    return sorted(n for n in os.listdir(d)
                  if os.path.isdir(os.path.join(d, n)) and n.lower() not in ("addon_template", "workshop_items"))
