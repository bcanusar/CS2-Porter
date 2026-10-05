"""UI and log texts. Every language is a JSON file in the lang folder (key -> text)."""

import json
import os
import threading

LANG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lang")
DEFAULT = "en"
# languages listed first, in this order; any other JSON file in lang/ is added after them
ORDER = ("en", "tr", "ru", "zh")

_cache = {}
_lang = DEFAULT


def _strings(code):
    if code not in _cache:
        data = {}
        try:
            with open(os.path.join(LANG_DIR, f"{code}.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            pass
        _cache[code] = data if isinstance(data, dict) else {}
    return _cache[code]


def languages():
    """[(code, native name)] of every language that has a JSON file."""
    try:
        found = {f[:-5] for f in os.listdir(LANG_DIR) if f.lower().endswith(".json")}
    except OSError:
        found = set()
    codes = [c for c in ORDER if c in found] + sorted(found - set(ORDER))
    return [(c, _strings(c).get("_name", c)) for c in codes if _strings(c)]


def set_lang(lang):
    global _lang
    _lang = lang if lang and _strings(lang) else DEFAULT


def get_lang():
    return _lang


def t_en(key, **kw):
    """English text, used for files that are always written in English (report.txt)."""
    return _format(_strings(DEFAULT).get(key, key), kw)


_mirror = threading.local()


def record_english(table):
    """While table is {"exact": {}, "parts": {}}, every text t() makes in this thread is also
    stored as {text: English text} ("parts" only gets the texts without values, which can be
    pieces of longer lines), so a log written in the UI language can be saved in English too.
    None stops it."""
    _mirror.table = table


def t(key, **kw):
    text = _strings(_lang).get(key)
    if text is None:
        text = _strings(DEFAULT).get(key, key)
    out = _format(text, kw)
    table = getattr(_mirror, "table", None)
    if table is not None and _lang != DEFAULT and len(table["exact"]) < 50000:
        en = t_en(key, **kw)
        table["exact"][out] = en
        if not kw:
            table["parts"][out] = en
    return out


def _format(text, kw):
    if kw:
        try:
            return text.format(**kw)
        except (KeyError, IndexError, ValueError):
            return text
    return text
