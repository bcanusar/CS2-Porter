"""Update check: asks the project's GitHub releases for a newer version, and installs one.

The check is one HTTPS request at startup. Installing downloads the release zip, unpacks it next
to the work files and starts a small PowerShell script that waits for the program to close,
copies the new files over the program folder (settings and the cache are not in the zip, so
they stay) and starts the program again.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

from . import __version__, config

REPO = "bcanusar/CS2-Porter"
API_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=10"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
ZIP_TOP = "CS2 Porter"
EXE_NAME = "CS2 Porter.exe"
TIMEOUT = 8


class Release:
    def __init__(self, tag, notes, page, zip_url, zip_size):
        self.tag = tag
        self.version = parse_version(tag)
        self.notes = notes
        self.page = page
        self.zip_url = zip_url
        self.zip_size = zip_size


class Staged:
    """A downloaded and unpacked update, ready to be copied over the program folder."""

    def __init__(self, release, folder, root):
        self.release = release
        self.folder = folder  # the unpacked "CS2 Porter" folder
        self.root = root  # everything the update left behind, removed after the install


def parse_version(text):
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:3]) if nums else ()


def _request(url):
    return urllib.request.Request(url, headers={"User-Agent": f"CS2Porter/{__version__}",
                                                "Accept": "application/vnd.github+json"})


def latest_release(timeout=TIMEOUT):
    """The newest published release (pre-releases too), or None. Raises OSError on failure."""
    try:
        with urllib.request.urlopen(_request(API_URL), timeout=timeout) as r:
            data = json.loads(r.read(4 * 1024 * 1024).decode("utf-8"))
    except (ValueError, OSError) as e:
        raise OSError(str(e)) from e
    best = None
    for rel in data if isinstance(data, list) else ():
        if rel.get("draft") or not parse_version(rel.get("tag_name", "")):
            continue
        asset = next((a for a in rel.get("assets") or () if a.get("name", "").lower().endswith(".zip")), None)
        cand = Release(rel.get("tag_name", ""), rel.get("body") or "", rel.get("html_url") or RELEASES_PAGE,
                       asset.get("browser_download_url", "") if asset else "",
                       int(asset.get("size") or 0) if asset else 0)
        if best is None or cand.version > best.version:
            best = cand
    return best


def newer_release(timeout=TIMEOUT):
    """The latest release when it is newer than this program, else None."""
    rel = latest_release(timeout)
    if rel is not None and rel.version > parse_version(__version__):
        return rel
    return None


def plain_notes(text, limit=4000):
    """Release notes without the Markdown marks."""
    out = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        s = line.rstrip()
        s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s)
        s = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", s)
        s = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), s)
        s = re.sub(r"`([^`]*)`", r"\1", s)
        s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
        out.append(s)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n..."
    return text


def can_install():
    """Only the built program updates itself, and only when its folder can be written to."""
    if not getattr(sys, "frozen", False):
        return False
    probe = os.path.join(config.APP_DIR, ".update_probe")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False


def _update_root():
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, "CS2Porter", "update")


def download(release, progress=None, cancelled=None):
    """Downloads and unpacks the release zip. Returns a Staged update, raises OSError on failure."""
    if not release.zip_url:
        raise OSError("the release has no zip file")
    root = _update_root()
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    zpath = os.path.join(root, "update.zip")
    got = 0
    with urllib.request.urlopen(_request(release.zip_url), timeout=30) as r, open(zpath, "wb") as f:
        total = int(r.headers.get("Content-Length") or release.zip_size or 0)
        while True:
            if cancelled is not None and cancelled():
                f.close()
                shutil.rmtree(root, ignore_errors=True)
                return None
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress is not None:
                progress(got, total)
    if total and got != total:
        raise OSError(f"the download stopped early ({got} of {total} bytes)")
    out = os.path.join(root, "files")
    try:
        with zipfile.ZipFile(zpath) as z:
            bad = z.testzip()
            if bad:
                raise OSError(f"damaged file in the download: {bad}")
            z.extractall(out)
    except zipfile.BadZipFile as e:
        raise OSError(str(e)) from e
    folder = os.path.join(out, ZIP_TOP)
    if not os.path.isfile(os.path.join(folder, EXE_NAME)):
        raise OSError("the download does not hold the program")
    os.remove(zpath)
    return Staged(release, folder, root)


_BUNDLE_VARS = ("_MEIPASS2", "_PYI_", "_PYIBOOTLOADER")


def _ps_quote(s):
    return "'" + s.replace("'", "''") + "'"


def start_install(staged):
    """Starts the script that copies the update in once this program has closed, and starts the
    new version. The caller closes the program right after."""
    exe = os.path.abspath(sys.executable)
    name = os.path.basename(exe)
    if name.lower() != EXE_NAME.lower():
        # the user renamed the program: the new one keeps that name
        os.replace(os.path.join(staged.folder, EXE_NAME), os.path.join(staged.folder, name))
    # the one file build runs as two processes, the launcher holds the exe until both are gone
    script = install_script(staged, exe, sorted({os.getpid(), os.getppid()}))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-WindowStyle", "Hidden", "-EncodedCommand", encoded]
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    # the new exe must not inherit this one's unpack folder, which is deleted when this one closes
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_BUNDLE_VARS)}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    try:
        subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, close_fds=True, env=env)
    except OSError:
        subprocess.Popen(cmd, creationflags=flags, close_fds=True, env=env)


def install_script(staged, exe, pids):
    """The PowerShell script that waits for pids to end, copies the update in and starts exe."""
    dst = os.path.dirname(exe)
    return "\n".join([
        "$ErrorActionPreference = 'SilentlyContinue'",
        f"$src = {_ps_quote(staged.folder)}",
        f"$dst = {_ps_quote(dst)}",
        f"$exe = {_ps_quote(exe)}",
        f"$tmp = {_ps_quote(staged.root)}",
        f"Wait-Process -Id {','.join(str(p) for p in pids)} -Timeout 120",
        "for ($i = 0; $i -lt 30; $i++) {",
        "  robocopy $src $dst /E /R:2 /W:1 /NJH /NJS /NFL /NDL /NP | Out-Null",
        "  if ($LASTEXITCODE -lt 8) { break }",
        "  Start-Sleep -Seconds 1",
        "}",
        "Get-ChildItem Env: | Where-Object { $_.Name -like '_MEIPASS2' -or $_.Name -like '_PYI*' } | "
        "ForEach-Object { Remove-Item -LiteralPath ('Env:' + $_.Name) }",
        "Start-Process -FilePath $exe -WorkingDirectory $dst",
        "Start-Sleep -Seconds 2",
        "Remove-Item -LiteralPath $tmp -Recurse -Force",
    ])
