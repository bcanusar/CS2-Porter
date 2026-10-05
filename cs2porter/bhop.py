"""Bhop script: the compiled script the bhop maps use, put into the addon's scripts folder.

Every port (when the option is on) downloads the newest version and keeps it as the saved copy
(cache folder next to the program). Without a connection the saved copy is used, and without
one of those the copy that comes with the program.

The download is one plain HTTPS request made by the port job itself and shown in the log; the
file is only copied, never run by the program. It starts with the port, so a slow or missing
connection does not hold the port up.
"""

import os
import struct
import threading
import urllib.request

from . import __version__, config
from .i18n import t

FILE_NAME = "bhop_script.vjs_c"
CACHE_FILE = os.path.join(config.CACHE_DIR, FILE_NAME)
BUNDLED_FILE = os.path.join(config.ASSETS_DIR, FILE_NAME)
TIMEOUT = 10


def is_valid(data):
    """A compiled CS2 resource starts with its own size and header version 12."""
    if not data or len(data) < 16:
        return False
    size, header = struct.unpack_from("<IH", data, 0)
    return size == len(data) and header == 12


def download(timeout=TIMEOUT):
    """Returns the script bytes from the download link, raises OSError on failure."""
    last = None
    for url in config.BHOP_SCRIPT_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"CS2Porter/{__version__}"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(16 * 1024 * 1024)
        except Exception as e:  # noqa: BLE001
            last = e
            continue
        if is_valid(data):
            return data
        last = ValueError("not a compiled script")
    raise OSError(str(last) if last else "no link")


class Download:
    """The download, started when the port begins."""

    def __init__(self, timeout=TIMEOUT):
        self.data = None
        self.error = None
        self._thread = threading.Thread(target=self._run, args=(timeout,), daemon=True)
        self._thread.start()

    def _run(self, timeout):
        try:
            self.data = download(timeout)
        except OSError as e:
            self.error = e

    def result(self, wait=TIMEOUT * 2):
        """(data, error) once the download is over."""
        self._thread.join(wait)
        if self._thread.is_alive():
            return None, OSError("timed out")
        return self.data, self.error


def _read(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data if is_valid(data) else None
    except OSError:
        return None


def _save(data):
    """The newest script becomes the saved copy (written next to it first, then swapped)."""
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp = CACHE_FILE + ".new"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, CACHE_FILE)
        return True
    except OSError:
        return False


def update(pending, log=None):
    """Waits for the download and keeps it as the saved copy. Returns (data, error)."""
    log = log or (lambda m, tag="info": None)
    data, error = pending.result()
    if data:
        saved = _read(CACHE_FILE)
        if saved != data:
            _save(data)
            log(t("bh_downloaded", kb=max(1, len(data) // 1024)), "ok")
        else:
            log(t("bh_same"), "tool")
    else:
        log(t("bh_offline_detail", e=error), "tool")
    return data, error


def install(game_dir, log=None, data=None, error=None):
    """Writes the script to <game_dir>/scripts: data (the download of this port), else the
    saved copy, else the program's copy. Returns the written path or None."""
    log = log or (lambda m, tag="info": None)
    if not data:
        if error is not None:
            log(t("bh_offline", e=error), "warn")
        data = _read(CACHE_FILE) or _read(BUNDLED_FILE)
    if not data:
        log(t("bh_none"), "warn")
        return None
    out = os.path.join(game_dir, "scripts", FILE_NAME)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f:
        f.write(data)
    log(t("bh_placed", path=out), "ok")
    return out
