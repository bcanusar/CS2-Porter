"""Sounds used by the map: found in the embedded files / installed games, copied to
content/csgo_addons/<addon>/sounds at 44100 Hz, and the map's soundscapes are added to
soundevents/soundevents_addon.vsndevts.

Source 1 soundscape (scripts/soundscapes_<map>.txt):
    "soundscape.level1" { "dsp" "0" "playlooping" { "volume" "0.45" "pitch" "100" "wave" "custom/lvl1.mp3" } }
CS2 soundevent (added just above "ambient_example.outdoors"):
    "soundscape.level1" =
    {
        base = "amb.looping.stereo.base"
        volume = 1.00000
        pitch = 1.000000
        vsnd_files_track_01 = "sounds/custom/lvl1.vsnd"
    }
"""

import os
import re

from . import audio
from . import kv
from .i18n import t

SOUND_EXTS = (".wav", ".mp3")
# Source 1 sound prefix characters (stereo, distance, music ...)
_PREFIX = "*#@><^)}$!?&~`+%( "
EVENTS_FILE = "soundevents/soundevents_addon.vsndevts"
_ANCHOR_RE = re.compile(r'^[ \t]*"ambient_example\.outdoors"[ \t]*=', re.M)
_KV3_HEAD = ("<!-- kv3 encoding:text:version{e21c7f3c-8a33-41c5-9977-a76d3a32aa0d} "
             "format:generic:version{7412167c-06e9-4698-aff2-e63eb59037e7} -->\n{\n}\n")


def clean_sound(path):
    p = (path or "").strip().replace("\\", "/").lstrip(_PREFIX).lstrip("/")
    while "//" in p:
        p = p.replace("//", "/")
    if p.lower().startswith("sound/"):
        p = p[6:]
    return p.lower()


def vsnd_path(rel):
    return "sounds/" + os.path.splitext(rel)[0] + ".vsnd"


class Soundscape:
    def __init__(self, name):
        self.name = name
        self.looping = []       # waves of playlooping blocks
        self.random = []        # waves of playrandom blocks


def parse_soundscapes(text):
    out = []
    for block in kv.parse(text).blocks():
        sc = Soundscape(block.name)
        for rule in block.blocks():
            rn = rule.name.lower()
            if rn == "playlooping":
                w = clean_sound(rule.get("wave", ""))
                if w:
                    sc.looping.append(w)
            elif rn == "playrandom":
                rnd = rule.block("rndwave")
                for _k, v in (rnd.values() if rnd else []):
                    w = clean_sound(v)
                    if w:
                        sc.random.append(w)
        out.append(sc)
    return out


_IN_TEXT_RE = re.compile(r"[^\s,\"\x1b]+\.(?:wav|mp3)\b", re.I)


def entity_sounds(root):
    """Sound files named in entity keys (ambient_generic message ...) and in outputs
    (point_clientcommand "play ..." and the like)."""
    out = []

    def add(v):
        rel = clean_sound(v)
        if rel and rel.endswith(SOUND_EXTS) and rel not in out:
            out.append(rel)

    for ent in root.children("entity"):
        for _k, v in ent.kvs():
            if v.lower().rstrip().endswith(SOUND_EXTS):
                add(v)
        for conn in ent.children("connections"):
            for _k, v in conn.kvs():
                for m in _IN_TEXT_RE.finditer(v):
                    add(m.group(0))
    return out


def map_soundscapes(sources, map_name):
    """[Soundscape] from the map's scripts/soundscapes_<map>.txt (embedded or game files)."""
    data, _src = sources.read(f"scripts/soundscapes_{map_name.lower()}.txt")
    return parse_soundscapes(kv.decode_bytes(data)) if data else []


def all_files(ent_files, scapes):
    out = list(ent_files)
    for sc in scapes:
        for w in sc.looping + sc.random:
            if w.endswith(SOUND_EXTS) and w not in out:
                out.append(w)
    return out


def copy_sounds(files, sources, content_dir, overwrite=False, result=None, log=None):
    """Copies / converts the sound files. Returns {rel: written path or None}."""
    log = log or (lambda m, tag="info": None)
    done = {}
    for rel in files:
        data, src = sources.read_sound("sound/" + rel)
        if data is None:
            done[rel] = None
            log(f"  [{t('st_missing')}] sound/{rel}", "warn")
            if result:
                result("sound", "sound/" + rel, "missing", "", "")
            continue
        base = os.path.join(content_dir, "sounds", *os.path.splitext(rel)[0].split("/"))
        existing = [base + e for e in SOUND_EXTS if os.path.isfile(base + e)]
        if existing and not overwrite:
            done[rel] = existing[0]
            if result:
                result("sound", "sound/" + rel, "exists", "", src.label)
            continue
        try:
            out, ext, note = audio.to_cs2(data, os.path.splitext(rel)[1])
        except (ValueError, OSError) as e:
            done[rel] = None
            log(t("snd_fail", f=rel, e=e), "err")
            if result:
                result("sound", "sound/" + rel, "error", str(e), src.label)
            continue
        os.makedirs(os.path.dirname(base), exist_ok=True)
        for old in existing:
            if not old.endswith(ext):
                os.remove(old)          # an older copy with the other extension would clash
        with open(base + ext, "wb") as f:
            f.write(out)
        done[rel] = base + ext
        detail = note or t("snd_copied")
        log(f"  [{t('st_created')}] sounds/{os.path.splitext(rel)[0]}{ext}  {detail}", "ok")
        if result:
            result("sound", "sound/" + rel, "created", detail, src.label)
    return done


def _event(name, base, vsnds, extra=()):
    lines = [f'\t"{name}" =', "\t{", f'\t\tbase = "{base}"']
    lines += [f"\t\t{line}" for line in extra]
    if vsnds is not None:
        if len(vsnds) == 1:
            lines.append(f'\t\tvsnd_files_track_01 = "{vsnds[0]}"')
        else:
            lines.append("\t\tvsnd_files_track_01 = ")
            lines.append("\t\t[")
            lines += [f'\t\t\t"{v}",' for v in vsnds]
            lines.append("\t\t]")
    lines.append("\t}")
    return "\n".join(lines)


def soundscape_events(scapes, found):
    """Text of the soundevents for the soundscapes whose looping sounds were found."""
    blocks, names = [], []
    for sc in scapes:
        waves = [w for w in sc.looping if found.get(w)]
        if not waves:
            continue
        loop = ("volume = 1.00000", "pitch = 1.000000")
        if len(waves) == 1:
            blocks.append(_event(sc.name, "amb.looping.stereo.base", [vsnd_path(waves[0])], loop))
        else:
            # several loops at once: a parent event starts one child event per sound
            kids = [f"{sc.name}.loop_{i:02d}" for i in range(1, len(waves) + 1)]
            parent = [f'\t"{sc.name}" =', "\t{", '\t\tbase = "amb.soundscapeParent.base"',
                      "\t\tenable_child_events = true", "\t\tsoundevent_01 = ", "\t\t["]
            parent += [f'\t\t\t"{k}",' for k in kids] + ["\t\t]", "\t}"]
            blocks.append("\n".join(parent))
            for k, w in zip(kids, waves):
                blocks.append(_event(k, "amb.looping.stereo.base", [vsnd_path(w)], loop))
        names.append(sc.name)
    return blocks, names


AMBIENT_EVERYWHERE = 1      # ambient_generic spawnflag "Play everywhere"


def collect_ambients(root):
    """ambient_generic entities that play a sound file:
    [{"pos", "rel", "radius", "volume", "pitch", "everywhere"}]."""
    out = []
    for ent in root.children("entity"):
        if ent.classname != "ambient_generic":
            continue
        rel = clean_sound(ent.get("message") or "")
        parts = (ent.get("origin") or "").split()
        if not rel.endswith(SOUND_EXTS) or len(parts) != 3:
            continue
        try:
            pos = tuple(round(float(v), 1) for v in parts)
            radius = float(ent.get("radius") or 1250)
            volume = min(max(float(ent.get("health") or 10) / 10.0, 0.0), 1.0)
            pitch = min(max(float(ent.get("pitch") or 100) / 100.0, 0.01), 2.55)
            flags = int(float(ent.get("spawnflags") or 0))
            fadein = min(max(float(ent.get("fadeinsecs") or 0), 0.0), 100.0)
        except ValueError:
            continue
        out.append({"pos": pos, "rel": rel, "radius": max(radius, 1.0), "volume": volume,
                    "pitch": pitch, "everywhere": bool(flags & AMBIENT_EVERYWHERE), "fadein": fadein})
    return out


# Source 1 sound falloff: an ambient_generic's radius becomes a sound level
# (50 + 20 * radius / 1000 dB, so the default 1250 is the normal 75 dB) and a sound plays at
# full volume up to 36 * 10^((level - 60) / 20) units, then falls off as 1 / distance until it
# drops under 1 / 100 (the engine's lowest volume)
SND_CLIP_DIST = 1000.0
SND_REF_DB = 60.0
SND_REF_DIST = 36.0
SND_GAIN_MIN = 0.01
SND_MAX_DIST = 15000.0


def full_volume_distance(radius):
    level = 50.0 + 20.0 * radius / SND_CLIP_DIST
    return SND_REF_DIST * 10.0 ** ((level - SND_REF_DB) / 20.0)


def _gain(radius, d):
    """(volume, slope) at distance d: the Source 1 falloff, and at least a smooth fade from
    full volume to silence over the radius (what v2.13 and earlier did)."""
    full = max(full_volume_distance(radius), 1.0)
    s1, s1_slope = (1.0, 0.0) if d <= full else (full / d, -full / (d * d))
    t = d / radius
    fade, fade_slope = (1.0 - t * t * (3.0 - 2.0 * t), -6.0 * t * (1.0 - t) / radius) if t < 1.0 else (0.0, 0.0)
    return (s1, s1_slope) if s1 >= fade else (fade, fade_slope)


def distance_gain(radius, d):
    """Volume (0..1) of an ambient_generic sound at distance d."""
    full = max(full_volume_distance(radius), 1.0)
    end = max(min(full / SND_GAIN_MIN, SND_MAX_DIST), radius)
    return 0.0 if d >= end else _gain(radius, d)[0]


def _curve(radius):
    full = max(full_volume_distance(radius), 1.0)
    end = max(min(full / SND_GAIN_MIN, SND_MAX_DIST), radius)
    xs = {0.0, radius * 0.25, radius * 0.5, radius * 0.75, radius}
    x = full
    while x < end:
        t = x / radius
        # points of the Source 1 falloff only where it is the louder one
        if t >= 1.0 or full / x >= 1.0 - t * t * (3.0 - 2.0 * t):
            xs.add(x)
        x *= 2.0
    points = [(x,) + _gain(radius, x) for x in sorted(v for v in xs if v < end)] + [(end, 0.0, 0.0)]
    lines = ["distance_volume_mapping_curve = ", "["]
    for x, y, slope in points:
        lines += ["\t[", f"\t\t{x:.1f}, {y:.5f}, {slope:.7f}, {slope:.7f},", "\t\t2.0, 3.0,", "\t],"]
    lines.append("]")
    return lines


def _fade_in(seconds):
    """Source 1 sounds start at full volume unless the entity fades them in (the CS2 template
    fades every sound in over 0.3 seconds, which a short sound does not survive)."""
    if seconds <= 0:
        return ["use_time_volume_mapping_curve = false"]
    return ["time_volume_mapping_curve = ", "[", "\t[", "\t\t0.0, 0.0, 0.0, 0.0,", "\t\t2.0, 3.0,", "\t],",
            "\t[", f"\t\t{seconds:.3f}, 1.0, 0.0, 0.0,", "\t\t2.0, 3.0,", "\t],", "]"]


def ambient_events(ambients, found, map_name):
    """Soundevents for the ambient_generic sounds. The volume falls off with distance like in
    Source 1 (see distance_gain); "Play everywhere" sounds keep their full volume.
    Returns (blocks, {position: event name})."""
    prefix = re.sub(r"[^a-z0-9_]", "_", map_name.lower())
    names, blocks, table, stems = {}, [], {}, {}
    for a in ambients:
        if not found.get(a["rel"]):
            continue
        key = (a["rel"], round(a["radius"]), round(a["volume"], 3), round(a["pitch"], 3), a["everywhere"],
               round(a.get("fadein", 0.0), 2))
        if key not in names:
            stem = re.sub(r"[^a-z0-9_]", "_", os.path.splitext(os.path.basename(a["rel"]))[0])
            n = stems.get(stem, 0) + 1
            stems[stem] = n
            name = f"{prefix}.{stem}" + (f"_{n}" if n > 1 else "")
            names[key] = name
            extra = [f"volume = {a['volume']:.5f}", f"pitch = {a['pitch']:.6f}"]
            base = "amb.looping.stereo.base" if a["everywhere"] else "amb.base"
            lines = [f'\t"{name}" =', "\t{", f'\t\tbase = "{base}"']
            lines += [f"\t\t{x}" for x in extra]
            lines.append(f'\t\tvsnd_files_track_01 = "{vsnd_path(a["rel"])}"')
            if not a["everywhere"]:
                lines += [f"\t\t{x}" for x in _curve(a["radius"])]
            lines += [f"\t\t{x}" for x in _fade_in(a.get("fadein", 0.0))]
            lines.append("\t}")
            blocks.append("\n".join(lines))
        table[a["pos"]] = names[key]
    return blocks, table


def update_events_file(content_dir, blocks, template=None):
    """Adds the event blocks above "ambient_example.outdoors" (events that already exist are
    replaced). Returns the number of events written."""
    path = os.path.join(content_dir, *EVENTS_FILE.split("/"))
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    elif template and os.path.isfile(template):
        with open(template, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    else:
        text = _KV3_HEAD
    for b in blocks:
        name = re.match(r'\s*"([^"]+)"', b).group(1)
        text = _remove_event(text, name)
    new = "\n".join(blocks) + "\n"
    m = _ANCHOR_RE.search(text)
    if m:
        text = text[:m.start()] + new + text[m.start():]
    else:
        i = text.find("/////////////// BASE SOUNDEVENT")
        if i >= 0:
            i = text.rfind("\n", 0, i) + 1
        else:
            i = text.rstrip().rfind("}")
        text = text[:i] + new + "\n" + text[i:]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return len(blocks)


def _remove_event(text, name):
    m = re.search(r'^[ \t]*"%s"[ \t]*=[ \t]*\r?\n[ \t]*\{' % re.escape(name), text, re.M)
    if not m:
        return text
    depth, i = 0, m.end() - 1
    while i < len(text):
        c = text[i]
        if c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                end = text.find("\n", i)
                return text[:m.start()] + text[(end + 1) if end >= 0 else len(text):]
        i += 1
    return text
