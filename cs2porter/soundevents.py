"""Soundevents of an addon for the soundevent editor: reads soundevents_addon.vsndevts (KV3 text),
writes single events back where they were (the rest of the file is kept as it is) and lists the
addon's sound files."""
import os
import re

from . import sounds

EVENTS_FILE = sounds.EVENTS_FILE
SOUND_EXTS = (".wav", ".mp3")

# bases of the addon template and what each is for (a new event starts from one of them)
PRESETS = (
    ("amb.looping.stereo.base", "se_preset_stereo"),
    ("amb.looping.atXYZ.base", "se_preset_loop_xyz"),
    ("amb.base", "se_preset_plain"),
    ("amb.intermittent.randomAroundPlayer.base", "se_preset_random"),
    ("amb.intermittent.atXYZ.base", "se_preset_inter_xyz"),
    ("amb.soundscapeParent.base", "se_preset_parent"),
)

# (key, kind, group). kind: float, bool, str, choice, vec3, files, events, curve
PROPS = (
    ("base", "base", "general"),
    ("type", "choice", "general"),
    ("mixgroup", "choice", "general"),
    ("volume", "float", "general"),
    ("volume_random_min", "float", "general"),
    ("volume_random_max", "float", "general"),
    ("pitch", "float", "general"),
    ("pitch_random_min", "float", "general"),
    ("pitch_random_max", "float", "general"),
    ("delay", "float", "general"),
    ("vsnd_files_track_01", "files", "files"),
    ("position", "vec3", "position"),
    ("use_world_position", "bool", "position"),
    ("position_relative_to_player", "bool", "position"),
    ("randomize_position_min_radius", "float", "position"),
    ("randomize_position_max_radius", "float", "position"),
    ("randomize_position_hemisphere", "bool", "position"),
    ("enable_retrigger", "bool", "repeat"),
    ("retrigger_interval_min", "float", "repeat"),
    ("retrigger_interval_max", "float", "repeat"),
    ("retrigger_radius", "float", "repeat"),
    ("occlusion_intensity", "float", "effects"),
    ("distance_effect_mix", "float", "effects"),
    ("reverb_wet", "float", "effects"),
    ("restrict_source_reverb", "bool", "effects"),
    ("override_dsp_preset", "bool", "effects"),
    ("dsp_preset", "choice", "effects"),
    ("enable_child_events", "bool", "children"),
    ("set_child_position", "bool", "children"),
    ("soundevent_01", "events", "children"),
    ("distance_volume_mapping_curve", "curve", "curves"),
    ("use_time_volume_mapping_curve", "bool", "curves"),
    ("time_volume_mapping_curve", "curve", "curves"),
    ("fadetime_volume_mapping_curve", "curve", "curves"),
    ("use_distance_unfiltered_stereo_mapping_curve", "bool", "curves"),
    ("distance_unfiltered_stereo_mapping_curve", "curve", "curves"),
)
PROP_KIND = {k: kind for k, kind, _g in PROPS}
GROUPS = ("general", "files", "position", "repeat", "effects", "children", "curves")
CHOICES = {
    "type": ("csgo_mega",),
    "mixgroup": ("Amb_Common", "Music", "Ambient", "SFX"),
}
# x axis of each curve: distance in units or time in seconds
CURVE_X = {"distance_volume_mapping_curve": "units", "distance_unfiltered_stereo_mapping_curve": "units",
           "time_volume_mapping_curve": "seconds", "fadetime_volume_mapping_curve": "seconds"}
DSP_FALLBACK = ("reverb_9_mediumChamber", "reverb_22_outsideOpen")


class Typed:
    """A KV3 value with a type prefix, e.g. resource_name:"sounds/x.vsnd"."""

    def __init__(self, kind, value):
        self.kind, self.value = kind, value

    def __eq__(self, o):
        return isinstance(o, Typed) and (o.kind, o.value) == (self.kind, self.value)


class KV3Error(ValueError):
    pass


_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")


class _Parser:
    def __init__(self, s):
        self.s, self.i = s, 0

    def ws(self):
        s = self.s
        while self.i < len(s):
            c = s[self.i]
            if c in " \t\r\n,":
                self.i += 1
            elif s.startswith("//", self.i):
                j = s.find("\n", self.i)
                self.i = len(s) if j < 0 else j + 1
            elif s.startswith("/*", self.i):
                j = s.find("*/", self.i + 2)
                self.i = len(s) if j < 0 else j + 2
            elif s.startswith("<!--", self.i):
                j = s.find("-->", self.i)
                self.i = len(s) if j < 0 else j + 3
            else:
                break

    def fail(self, what):
        line = self.s.count("\n", 0, self.i) + 1
        raise KV3Error(f"{what} (line {line})")

    def string(self):
        s = self.s
        if s.startswith('"""', self.i):
            j = s.find('"""', self.i + 3)
            if j < 0:
                self.fail("unclosed string")
            v = s[self.i + 3:j].strip("\r\n")
            self.i = j + 3
            return v
        out, i = [], self.i + 1
        while i < len(s):
            c = s[i]
            if c == "\\" and i + 1 < len(s):
                out.append({"n": "\n", "t": "\t"}.get(s[i + 1], s[i + 1]))
                i += 2
                continue
            if c == '"':
                self.i = i + 1
                return "".join(out)
            out.append(c)
            i += 1
        self.fail("unclosed string")

    def value(self):
        self.ws()
        s = self.s
        if self.i >= len(s):
            self.fail("value expected")
        c = s[self.i]
        if c == "{":
            self.i += 1
            obj = {}
            while True:
                self.ws()
                if self.i >= len(s):
                    self.fail("unclosed {")
                if s[self.i] == "}":
                    self.i += 1
                    return obj
                if s[self.i] == '"':
                    key = self.string()
                else:
                    m = _IDENT_RE.match(s, self.i)
                    if not m:
                        self.fail("key expected")
                    key, self.i = m.group(0), m.end()
                self.ws()
                if s[self.i:self.i + 1] != "=":
                    self.fail("= expected")
                self.i += 1
                obj[key] = self.value()
        if c == "[":
            self.i += 1
            arr = []
            while True:
                self.ws()
                if self.i >= len(s):
                    self.fail("unclosed [")
                if s[self.i] == "]":
                    self.i += 1
                    return arr
                arr.append(self.value())
        if c == '"':
            return self.string()
        m = _NUM_RE.match(s, self.i)
        if m and m.group(0) not in "+-":
            self.i = m.end()
            t = m.group(0)
            return float(t) if any(ch in t for ch in ".eE") else int(t)
        m = _IDENT_RE.match(s, self.i)
        if m:
            self.i = m.end()
            word = m.group(0)
            if s[self.i:self.i + 1] == ":" and s[self.i + 1:self.i + 2] == '"':
                self.i += 1
                return Typed(word, self.string())
            return {"true": True, "false": False, "null": None}.get(word, word)
        self.fail("unknown value")


def parse(text):
    """The events of a vsndevts text as {name: {key: value}} (file order)."""
    p = _Parser(text)
    root = p.value()
    if not isinstance(root, dict):
        raise KV3Error("not a KV3 object")
    return {k: v for k, v in root.items() if isinstance(v, dict)}


# --- writing ------------------------------------------------------------------
def num(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    t = f"{float(v):.6f}".rstrip("0")
    return t + "0" if t.endswith(".") else t


def _quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _scalar(v):
    if v is None:
        return "null"
    if isinstance(v, (bool, int, float)):
        return num(v)
    if isinstance(v, Typed):
        return f"{v.kind}:{_quote(v.value)}"
    return _quote(str(v))


def _is_flat(v):
    return isinstance(v, list) and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)


def _value_lines(v, ind):
    """Lines of a value that goes below its key (arrays and objects)."""
    if isinstance(v, dict):
        out = [ind + "{"]
        for k, x in v.items():
            out += _pair(k, x, ind + "\t")
        return out + [ind + "}"]
    out = [ind + "["]
    for x in v:
        if _is_flat(x) and len(x) == 6:
            # a curve point: x, y, slope in, slope out, then the two mode numbers
            out += [ind + "\t[", ind + "\t\t" + ", ".join(num(n) for n in x[:4]) + ",",
                    ind + "\t\t" + ", ".join(num(n) for n in x[4:]) + ",", ind + "\t],"]
        elif isinstance(x, (list, dict)) and not (_is_flat(x) and len(x) <= 4):
            sub = _value_lines(x, ind + "\t")
            out += sub[:-1] + [sub[-1] + ","]
        elif isinstance(x, list):
            out.append(ind + "\t[ " + ", ".join(num(n) for n in x) + " ],")
        else:
            out.append(ind + "\t" + _scalar(x) + ",")
    return out + [ind + "]"]


def _key(k):
    return k if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) else _quote(k)


def _pair(k, v, ind):
    if isinstance(v, dict) or (isinstance(v, list) and not (_is_flat(v) and len(v) <= 4)):
        return [f"{ind}{_key(k)} = "] + _value_lines(v, ind)
    if isinstance(v, list):
        return [f"{ind}{_key(k)} = [ " + ", ".join(num(n) for n in v) + " ]"]
    return [f"{ind}{_key(k)} = {_scalar(v)}"]


def dump_event(name, data):
    """Text block of one event, indented like the template."""
    lines = [f"\t{_quote(name)} = ", "\t{"]
    for k, v in data.items():
        lines += _pair(k, v, "\t\t")
    return "\n".join(lines + ["\t}"])


def _event_span(text, name):
    """(start, end) of an event block in the text, end after its line break; None when missing."""
    m = re.search(r'^[ \t]*"%s"[ \t]*=\s*\{' % re.escape(name), text, re.M)
    if not m:
        return None
    depth, i, n = 0, m.end() - 1, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                end = text.find("\n", i)
                return m.start(), (end + 1) if end >= 0 else n
        i += 1
    return None


def set_event(text, name, data, old=None):
    """Writes the event in place of the old one (old: the name it had before a rename), or
    adds it above the template's events."""
    block = dump_event(name, data) + "\n"
    for k in (old, name):
        span = _event_span(text, k) if k else None
        if span:
            text = text[:span[0]] + block + text[span[1]:]
            if k != name:
                text = remove_event(text, name, keep_first=span[0] + len(block))
            return text
    m = sounds._ANCHOR_RE.search(text)
    if m:
        i = m.start()
    else:
        i = text.find("/////////////// BASE SOUNDEVENT")
        i = text.rfind("\n", 0, i) + 1 if i >= 0 else text.rstrip().rfind("}")
        if i < 0:
            text, i = sounds._KV3_HEAD, sounds._KV3_HEAD.rfind("}")
    return text[:i] + block + text[i:]


def remove_event(text, name, keep_first=0):
    span = _event_span(text[keep_first:], name)
    if not span:
        return text
    a, b = span[0] + keep_first, span[1] + keep_first
    return text[:a] + text[b:]


# --- files --------------------------------------------------------------------
def events_path(content_dir):
    return os.path.join(content_dir, *EVENTS_FILE.split("/"))


def template_dir(cs2_dir):
    return os.path.join(cs2_dir, "content", "csgo_addons", "addon_template")


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def template_info(cs2_dir):
    """(event names of the template file, dsp presets it lists, its sound files)."""
    tdir = template_dir(cs2_dir)
    names, dsp = set(), []
    p = events_path(tdir)
    if os.path.isfile(p):
        text = read_text(p)
        names = set(re.findall(r'^[ \t]*"([^"]+)"[ \t]*=\s*$', text, re.M))
        dsp = sorted(set(re.findall(r"^//\s*(reverb_\w+)", text, re.M)) | set(DSP_FALLBACK),
                     key=lambda s: (int(re.search(r"\d+", s).group(0)) if re.search(r"\d+", s) else 0, s))
    files = {rel.lower() for rel, _p in list_sound_files(tdir)}
    return names, dsp or list(DSP_FALLBACK), files


def list_sound_files(content_dir):
    """[(path relative to sounds/, full path)] of the sound files of a content folder."""
    root = os.path.join(content_dir, "sounds")
    out = []
    for d, _subs, files in os.walk(root):
        for f in files:
            if f.lower().endswith(SOUND_EXTS):
                full = os.path.join(d, f)
                out.append((os.path.relpath(full, root).replace("\\", "/"), full))
    return sorted(out, key=lambda x: x[0].lower())


def vsnd_of(rel):
    """Soundevent path of a file below sounds/."""
    return sounds.vsnd_path(rel)


def event_name(stem, taken):
    base = re.sub(r"[^a-z0-9_.]+", "_", stem.lower()).strip("_.") or "sound"
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}_{n}"
    return name


def resolve(events, name, key, seen=None):
    """Value of a key for an event, looked up through its bases. (value, event that sets it)."""
    seen = seen or set()
    while name in events and name not in seen:
        seen.add(name)
        ev = events[name]
        if key in ev:
            return ev[key], name
        name = ev.get("base")
        if not isinstance(name, str):
            break
    return None, None


def curve_slopes(points):
    """Curve points [(x, y)] as KV3 rows with the slopes of the straight lines between them."""
    pts = sorted(points)
    rows = []
    for i, (x, y) in enumerate(pts):
        if len(pts) == 1:
            s = 0.0
        elif i == len(pts) - 1:
            s = (y - pts[i - 1][1]) / max(x - pts[i - 1][0], 1e-6)
        else:
            s = (pts[i + 1][1] - y) / max(pts[i + 1][0] - x, 1e-6)
        rows.append([float(x), float(y), round(s, 7), round(s, 7), 2.0, 3.0])
    return rows
