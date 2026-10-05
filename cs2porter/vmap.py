"""Edits the .vmap files written by the map import.

The importer splits the map into a main .vmap (text) and prefab .vmap files (binary DMX).
Binary files are turned into text with CS2's dmxconvert, edited, then the prefabs are
collapsed into the main map (like Hammer's "Collapse All Prefabs") and it is saved as binary.

- the stand-in sky material becomes skybox_moondome.vmat (with clip collision) or
  tools/toolsskybox when there is no skybox; env_sky gets skybox.vmat
- lights get brightness_lumens and range worked out from the original light (see lighting.py),
  default indirect light and falloff; light_environment gets sensible brightness /
  sky intensity values and default shadow softness / direct lighting type
- a visibility_hint (256 unit grid) is added over the whole map
- combined light probe volumes are added over the map, each with its white room (probes.py)
- !activator AddOutput basevelocity / gravity outputs (and the teleport outputs added to
  trigger_teleports with a landmark) are sent to script_bhop, and only then
  point_script + logic_case 'script_bhop' are added at the world origin
- !activator AddOutput targetname X becomes AddAttribute X plus RemoveAttribute for every
  other attribute used by the map's filters (a player had only one name in Source 1);
  "targetname default" only removes every attribute
- marked func_brush entities (no inputs / outputs) are replaced by their meshes, and the
  helper info_targets (!activator, !self, !caller) needed by the import are removed
"""

import math
import os
import random
import re
import shutil
import uuid

from . import blend as blendmod
from . import probes as probesmod
from .i18n import t

TOOLS_SKY_VMAT = "materials/tools/toolsskybox.vmat"
# texture scale of the sky brush faces (their shift becomes 0)
SKY_FACE_SCALE = 0.125
BHOP_SCRIPT = "scripts/bhop_script.vjs"
BHOP_CASE = "script_bhop"

_CLASS_RE = re.compile(r'^\s*"classname" "string" "([^"]*)"')
_NODE_RE = re.compile(r'("nodeID" "int" ")(\d+)"')
_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|[{}\[\],]')


HELPER_NAMES = ("!activator", "!self", "!caller")
TELEPORT_KEY = "cs2porter_teleport"


class Fixes:
    def __init__(self, sky_alias=None, sky_vmat=None, moondome_vmat=None, lights=None, lumens=True,
                 bhop=False, attributes=None, meshes=None, blends=None, fgd=None, collapse=True,
                 ropes=None, ambients=None, water=None, commands=False, uv_sizes=None,
                 effects=None, cable_missing=None, vis_bounds=None, light_rooms=None, particle_names=None,
                 surface_lights=None, underwater=None):
        self.sky_alias = sky_alias          # stand-in material used for toolsskybox faces
        self.sky_vmat = sky_vmat            # env_sky material (None: leave env_sky as it is)
        self.moondome_vmat = moondome_vmat  # material for the sky brushes (None: tools/toolsskybox)
        self.lights = lights or []          # [(cs2_class, (x, y, z), info)] from vmf.collect_lights
        self.lumens = lumens                # write brightness_lumens
        self.bhop = bhop
        self.attributes = attributes or {}  # filter attribute names: {'targetname'|'classname': [...]}
        self.meshes = meshes or {}          # marker targetname -> mesh settings (vmf.fix_for_cs2)
        self.blends = blends or {}          # blend displacements (blend.collect_disps)
        self.fgd = fgd                      # entity definitions: keys they do not have are removed
        self.collapse = collapse
        self.ropes = ropes or {}            # rope chains (vmf.collect_ropes)
        self.ambients = ambients or {}      # ambient_generic position -> soundevent name
        self.water = {w.lower() for w in (water or ())}     # water materials ('materials/x.vmat')
        self.commands = commands            # bhop_ map: add the server / client command entities
        self.uv_sizes = uv_sizes            # uvfix.TextureSizes of the addon (None: no scale fix)
        self.effects = effects or {}        # particles made from env_steam etc. (vmf.fix_for_cs2)
        self.cable_missing = set(cable_missing or ())   # rope materials that were not found
        self.vis_bounds = vis_bounds        # (mins, maxs) of the map: visibility_hint over it
        self.light_rooms = light_rooms      # probes.plan(): light probe volumes and their rooms
        # Source 1 particle system name (lower case) -> converted 'particles/x/y.vpcf'
        self.particle_names = particle_names or {}
        # light_rect entities for glowing surfaces whose material can not glow (texlights.rect_lights)
        self.surface_lights = surface_lights or []
        # water .vmat -> underwater .vpost (postfx.write_underwater)
        self.underwater = underwater or {}


class Result:
    def __init__(self):
        self.bhop_outputs = 0
        self.bhop_entities = None
        self.collapsed = 0
        self.prefabs_removed = 0
        self.stats = {}


def map_files(maps_dir, map_name):
    """Main .vmap first, then its prefabs."""
    out = []
    main = os.path.join(maps_dir, f"{map_name}.vmap")
    if os.path.isfile(main):
        out.append(main)
    pre = os.path.join(maps_dir, "prefabs", map_name)
    if os.path.isdir(pre):
        out += [os.path.join(pre, f) for f in sorted(os.listdir(pre)) if f.lower().endswith(".vmap")]
    return out


def _is_binary(path):
    with open(path, "rb") as f:
        head = f.read(64)
    return b"encoding binary" in head


def _pos_key(values):
    return tuple(round(float(v), 1) for v in values)


# ---------------------------------------------------------------------------
# Text structure helpers
# ---------------------------------------------------------------------------

def _close(text, open_pos):
    """Index just after the bracket/brace that closes the one at open_pos."""
    depth = 0
    for m in _TOKEN_RE.finditer(text, open_pos):
        tok = m.group(0)
        if tok in "{[":
            depth += 1
        elif tok in "}]":
            depth -= 1
            if depth == 0:
                return m.end()
    raise ValueError("unbalanced brackets")


def _world_children(text):
    """(start, end) of the inside of the world's children array."""
    w = text.find('"world" "CMapWorld"')
    if w < 0:
        raise ValueError("no world")
    m = re.compile(r'"children" "element_array" *\r?\n[ \t]*\[').search(text, w)
    if not m:
        raise ValueError("no world children")
    end = _close(text, m.end() - 1)
    return m.end(), end - 1


_REF_ELEM_RE = re.compile(r'"element"[ \t]+"([^"]*)"')
_ELEM_ID_RE = re.compile(r'"id" "elementid" "([^"]+)"')


def is_null_element(elem):
    """True for a reference to an element ('"element" "<id>"', or empty) instead of an element."""
    return _REF_ELEM_RE.match(elem.lstrip()) is not None


def drop_dangling_refs(text):
    """World children that reference an element which is not in the text any more (or no
    element at all) are removed. Returns (text, removed)."""
    start, end = _world_children(text)
    elems = _split_elements(text, start, end)
    if not any(is_null_element(e) for e in elems):
        return text, 0
    ids = set(_ELEM_ID_RE.findall(text))
    keep = [e for e in elems if not is_null_element(e) or _REF_ELEM_RE.match(e.lstrip()).group(1) in ids]
    if len(keep) == len(elems):
        return text, 0
    ind = re.match(r"[ \t]*", text[text.rfind("\n", 0, start) + 1:]).group(0)
    return _join_world(text, start, end, keep, ind), len(elems) - len(keep)


def _split_elements(text, start, end):
    """Top level elements ('"Type" { ... }') between start and end, as text slices. Element
    references ('"element" "<id>"') are kept as entries of their own."""
    out = []
    depth = 0
    elem_start = None
    pos = start
    while True:
        m = _TOKEN_RE.search(text, pos, end)
        if not m:
            break
        pos = m.end()
        tok = m.group(0)
        if depth == 0 and tok.startswith('"') and elem_start is None:
            line_start = max(text.rfind("\n", start, m.start()) + 1, start)
            null = _REF_ELEM_RE.match(text, m.start(), end)
            if null:
                out.append(text[line_start:null.end()])
                pos = null.end()
                continue
            elem_start = line_start
        elif tok in "{[":
            depth += 1
        elif tok in "}]":
            depth -= 1
            if depth == 0 and elem_start is not None and tok == "}":
                out.append(text[elem_start:m.end()])
                elem_start = None
    return out


# ---------------------------------------------------------------------------
# Text edits
# ---------------------------------------------------------------------------

_PLUS_RE = re.compile(r'"(materials/[^"]*?/\+[^"]*|materials/\+[^"]*)"')


def strip_plus(text):
    """Material paths with a name starting with '+' get the name the converted material has
    (materials.out_name). Returns (text, count)."""
    count = [0]

    def repl(m):
        count[0] += 1
        return '"' + "/".join(p.lstrip("+") or p for p in m.group(1).split("/")) + '"'

    if "/+" not in text:
        return text, 0
    text = _PLUS_RE.sub(repl, text)
    return text, count[0]


def replace_sky(text, alias, new_vmat):
    old = f"materials/{alias}.vmat"
    n = text.count(old)
    return (text.replace(old, new_vmat), n) if n else (text, 0)


def clip_meshes(text, vmat):
    """Sets physicsCollisionProperty to clip on every mesh that uses vmat."""
    lines = text.split("\n")
    in_mats = flagged = False
    count = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith('"materials" "string_array"'):
            in_mats, flagged = True, False
            continue
        if in_mats:
            if s.startswith("]"):
                in_mats = False
            elif vmat in s:
                flagged = True
            continue
        if flagged and s.startswith('"physicsCollisionProperty" "string"'):
            indent = line[:len(line) - len(line.lstrip())]
            lines[i] = f'{indent}"physicsCollisionProperty" "string" "clip"'
            flagged = False
            count += 1
    return "\n".join(lines), count


def set_env_sky(text, vmat):
    lines = text.split("\n")
    in_sky = False
    count = 0
    for i, line in enumerate(lines):
        m = _CLASS_RE.match(line)
        if m:
            in_sky = m.group(1) == "env_sky"
            continue
        if in_sky and line.strip().startswith('"skyname" "string"'):
            indent = line[:len(line) - len(line.lstrip())]
            lines[i] = f'{indent}"skyname" "string" "{vmat}"'
            in_sky = False
            count += 1
    return "\n".join(lines), count


_PROPS_RE = re.compile(r'"entity_properties" "EditGameClassProps"\s*\r?\n[ \t]*\{')
_PROP_RE = re.compile(r'^([ \t]*)"([^"]+)" "string" "((?:[^"\\]|\\.)*)"[ \t]*\r?$')
_ORIGIN_ANY_RE = re.compile(r'"origin" "vector3" "([^"]*)"')


def _parse_props(block):
    props = {}
    for line in block.split("\n"):
        m = _PROP_RE.match(line)
        if m:
            props[m.group(2).lower()] = m.group(3)
    return props


def _apply_props(block, changes):
    """Sets {key: value} in an EditGameClassProps block; missing keys are added at the end and
    keys with the value None are removed."""
    lines = block.split("\n")
    left = {k.lower(): (k, v) for k, v in changes.items()}
    indent = None
    drop = set()
    for i, line in enumerate(lines):
        m = _PROP_RE.match(line)
        if not m:
            continue
        indent = m.group(1)
        k = m.group(2).lower()
        if k in left:
            value = left.pop(k)[1]
            if value is None:
                drop.add(i)
            else:
                lines[i] = f'{indent}"{m.group(2)}" "string" "{value}"'
    if drop:
        lines = [line for i, line in enumerate(lines) if i not in drop]
    left = {k: kv for k, kv in left.items() if kv[1] is not None}
    if left:
        close = max(i for i, line in enumerate(lines) if line.strip() == "}")
        ind = indent if indent is not None else lines[close][:len(lines[close]) - len(lines[close].lstrip())] + "\t"
        lines[close:close] = [f'{ind}"{k}" "string" "{v}"' for k, v in left.values()]
    return "\n".join(lines)


def edit_entities(text, fn):
    """Calls fn(props, origin) for every entity; it returns {key: value} to set or None.
    props keys are lower case. Returns (text, changed)."""
    out, pos, changed = [], 0, 0
    for m in _PROPS_RE.finditer(text):
        start = m.start()
        end = _close(text, m.end() - 1)
        block = text[start:end]
        props = _parse_props(block)
        om = _ORIGIN_ANY_RE.search(text, end, end + 2000)
        origin = None
        if om:
            try:
                origin = _pos_key(om.group(1).split())
            except ValueError:
                origin = None
        changes = fn(props, origin)
        if changes:
            out.append(text[pos:start])
            out.append(_apply_props(block, changes))
            pos = end
            changed += 1
    if not changed:
        return text, 0
    out.append(text[pos:])
    return "".join(out), changed


def _num(value):
    """Number as text without needless decimals (512.0 -> 512, 0.5 -> 0.5)."""
    v = round(float(value), 4)
    return str(int(v)) if v == int(v) else f"{v:g}"


def remove_unused_props(text, fgd, stats):
    """Removes the keys an entity's class does not have in CS2's entity definitions (keys left
    over from Source 1). Classes the definitions do not know are left alone."""
    from .fgd import ALWAYS_KEEP

    def fn(p, _origin):
        keys = fgd.keys(p.get("classname", ""))
        if keys is None:
            return None
        drop = {k: None for k in p if k not in keys and k not in ALWAYS_KEEP}
        if drop:
            stats["props"] += len(drop)
        return drop or None

    text, n = edit_entities(text, fn)
    stats["props_ents"] += n
    return text


_POINT_LIGHTS = ("light_omni2", "light_barn", "light_rect", "light_spot")
MAX_LIGHT_RANGE = 30000
LIGHT_RANGE_STEP = 50
# range of a light whose range is far too short: x sqrt(lumens) (5272 lumens -> about 3000);
# "far too short" = under LIGHT_RANGE_SHORT of that
LIGHT_RANGE_PER_LUMEN = 41.0
LIGHT_RANGE_SHORT = 0.5
LIGHT_MATCH_DIST = 4.0


def light_range(rng, brightness):
    """Range rounded to LIGHT_RANGE_STEP. Ranges over MAX_LIGHT_RANGE (the importer works out
    127000 for some lights) become 5 x the brightness."""
    if rng > MAX_LIGHT_RANGE:
        try:
            b = float(brightness)
        except (TypeError, ValueError):
            b = 0.0
        rng = b * 5 if b > 0 else MAX_LIGHT_RANGE
    rng = round(rng / LIGHT_RANGE_STEP) * LIGHT_RANGE_STEP
    return str(int(min(max(rng, LIGHT_RANGE_STEP), MAX_LIGHT_RANGE)))


def fix_lights(text, table, lumens=True):
    """table: {(x, y, z): [(cs2_class, info), ...]} from vmf.collect_lights; matched entries are
    removed. Returns (text, lights changed, lumens set, ranges raised)."""
    counts = [0, 0, 0]

    def pick(cls, origin):
        cands = table.get(origin) or []
        hit = next((c for c in cands if c[0] == cls), cands[0] if cands else None)
        if hit is None and origin is not None:
            # the importer moves spot lights a little (light_barn 0.4 .. 1 unit away)
            near = [(sum((a - b) ** 2 for a, b in zip(pos, origin)), pos) for pos, lst in table.items()
                    if any(c[0] == cls for c in lst)]
            near = [x for x in near if x[0] <= LIGHT_MATCH_DIST ** 2]
            if near:
                cands = table[min(near)[1]]
                hit = next(c for c in cands if c[0] == cls)
        if hit is not None:
            cands.remove(hit)
            return hit[1]
        return {}

    def fn(p, origin):
        cls = p.get("classname", "")
        ch = {}
        if cls == "light_environment":
            info = pick(cls, origin)
            ch = {"angulardiameter": "1.0", "directlight": "3"}
            for k in ("brightness", "skyintensity"):
                if k in info:
                    ch[k] = info[k]
        elif cls in _POINT_LIGHTS:
            info = pick(cls, origin)
            ch = {"bouncelight": "-1", "skirt": "0.1"}
            if cls == "light_barn":
                ch["skirt_near"] = "0.05"
            if p.get("brightness", "").strip().lower() in ("nan", "-nan", "inf", "-inf"):
                ch["brightness"] = "0"      # the importer writes nan for some lights
            rng = info.get("range")
            if not rng:
                try:
                    rng = float(p.get("range") or 0)
                except ValueError:
                    rng = 0
            # some maps give very bright lights a tiny range (5272 lumens, range 850): CS2 cuts
            # the light off there. A range under half of LIGHT_RANGE_PER_LUMEN x sqrt(lumens)
            # (inverse square falloff) becomes that value; normal lights are left alone
            raw_lum = (info.get("lumens") if lumens else None) or p.get("brightness_lumens")
            try:
                lum = max(float(raw_lum), 0.0) if raw_lum else 0.0
            except (TypeError, ValueError):
                lum = 0.0
            floor = LIGHT_RANGE_PER_LUMEN * math.sqrt(lum)
            if rng and rng < floor * LIGHT_RANGE_SHORT:
                rng = floor
                counts[2] += 1
            if rng:
                bright = (info.get("lumens") if lumens else None) or p.get("brightness_lumens") \
                    or p.get("brightness")
                ch["range"] = light_range(rng, bright)
            if lumens and info.get("lumens"):
                ch["brightness_units"] = "1"
                ch["brightness_lumens"] = info["lumens"]
                counts[1] += 1
        if ch:
            counts[0] += 1
        return ch

    text, _n = edit_entities(text, fn)
    return text, counts[0], counts[1], counts[2]


# --- connections (outputs) ------------------------------------------------------

_CONN_RE = re.compile(r'([ \t]*)"DmeConnectionData"\r?\n[ \t]*\{\r?\n(.*?)\r?\n[ \t]*\}', re.S)
_FIELD_RE = re.compile(r'^\s*"([^"]+)" "([^"]+)" "((?:[^"\\]|\\.)*)"\s*$')
_BHOP_KEYS = {"basevelocity": "boost", "gravity": "gravity"}


def _conn_text(ind, fields):
    body = "\n".join(f'{ind}\t"{k}" "{typ}" "{v}"' for k, typ, v in fields)
    return f'{ind}"DmeConnectionData"\n{ind}{{\n{body}\n{ind}}}'


def _set(fields, key, value):
    return [(k, typ, value if k == key else v) for k, typ, v in fields]


def _attribute_lists(attributes):
    """{'targetname': [...], 'classname': [...]} (a plain list means name filters only)."""
    if isinstance(attributes, dict):
        return {k: list(v) for k, v in attributes.items() if v}
    return {"targetname": list(attributes)} if attributes else {}


def rewrite_connections(text, bhop, attributes):
    """attributes: filter attribute names by the key that set them in Source 1
    ({'targetname': [...], 'classname': [...]}).
    Returns (text, bhop_count, attribute_count, removes_added, teleport_count)."""
    counts = [0, 0, 0, 0]
    by_key = _attribute_lists(attributes)

    def repl(m):
        ind = m.group(1)
        fields = []
        for line in m.group(2).split("\n"):
            f = _FIELD_RE.match(line)
            if not f:
                return m.group(0)
            fields.append((f.group(1), f.group(2), f.group(3)))
        d = {k: v for k, _typ, v in fields}
        if d.get("targetName", "").lower() != "!activator" or d.get("inputName", "").lower() != "addoutput":
            return m.group(0)
        words = d.get("overrideParam", "").strip().split(None, 1)
        if not words:
            return m.group(0)
        key = words[0].lower()
        value = words[1].strip() if len(words) > 1 else ""
        if bhop and key in _BHOP_KEYS:
            counts[0] += 1
            new = _set(_set(_set(fields, "targetName", BHOP_CASE), "inputName", "InValue"),
                       "overrideParam", f"{_BHOP_KEYS[key]} {value}".strip())
            return _conn_text(ind, new)
        if key == TELEPORT_KEY:
            # added by vmf.fix_for_cs2 to trigger_teleports with a landmark
            counts[0] += 1
            counts[3] += 1
            new = _set(_set(_set(fields, "targetName", BHOP_CASE), "inputName", "InValue"),
                       "overrideParam", f"teleport {value}".strip())
            return _conn_text(ind, new)
        attrs = by_key.get(key)
        if attrs and value:
            counts[1] += 1
            # "default" (and "player" for the class) is what a player gets back when every
            # flag is cleared: no add, unless a filter of the map checks for it
            reset = value.lower() == "default" or (
                key == "classname" and value.lower() == "player"
                and "player" not in {a.lower() for a in attrs})
            adds = [] if reset else [value] + [
                a for a in attrs if a.endswith("*") and value.lower().startswith(a[:-1].lower())
                and a.lower() != value.lower()]
            add_set = {a.lower() for a in adds}
            blocks = []
            for a in attrs:
                if a.lower() in add_set:
                    continue
                rem = _set(_set(_set(fields, "id", str(uuid.uuid4())), "inputName", "RemoveAttribute"),
                           "overrideParam", a)
                blocks.append(_conn_text(ind, rem))
                counts[2] += 1
            # the removes come first with the same delay, so they run before the adds
            for i, a in enumerate(adds):
                add = _set(_set(fields, "inputName", "AddAttribute"), "overrideParam", a)
                if i:
                    add = _set(add, "id", str(uuid.uuid4()))
                blocks.append(_conn_text(ind, add))
            return ",\n".join(blocks) if blocks else m.group(0)
        return m.group(0)

    text = _CONN_RE.sub(repl, text)
    return text, counts[0], counts[1], counts[2], counts[3]


# --- bhop entities ----------------------------------------------------------------

def kv2_escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def element_block(ind, props, kind="CMapEntity", origin="0 0 0", children=(), conns=(), tail=(), angles="0 0 0"):
    """Text of a map node element (entity, cable, path node ...).
    props: [(key, value)], children: element texts, conns: [{output, target, input, param,
    delay, times}], tail: [(name, type, value)] attributes written after randomSeed."""
    def eid():
        return str(uuid.uuid4())

    i1, i2, i3 = ind + "\t", ind + "\t\t", ind + "\t\t\t"
    prop_lines = [f'{i2}"{k}" "string" "{kv2_escape(str(v))}"' for k, v in props]
    conn_blocks = []
    for c in conns:
        conn_blocks.append("\n".join([
            f'{i2}"DmeConnectionData"', f"{i2}{{",
            f'{i3}"id" "elementid" "{eid()}"',
            f'{i3}"outputName" "string" "{c["output"]}"',
            f'{i3}"targetType" "int" "7"',
            f'{i3}"targetName" "string" "{kv2_escape(c["target"])}"',
            f'{i3}"inputName" "string" "{c["input"]}"',
            f'{i3}"overrideParam" "string" "{kv2_escape(c.get("param", ""))}"',
            f'{i3}"delay" "float" "{_num(c.get("delay", 0))}"',
            f'{i3}"timesToFire" "int" "{c.get("times", -1)}"',
            f"{i2}}}"]))
    child_text = ",\n".join(ch.rstrip() for ch in children)
    lines = [
        f'{ind}"{kind}"', f"{ind}{{",
        f'{i1}"id" "elementid" "{eid()}"',
        f'{i1}"nodeID" "int" "0"',
        f'{i1}"referenceID" "uint64" "0x{random.getrandbits(64):016x}"',
        f'{i1}"children" "element_array" ', f"{i1}["] + ([child_text] if children else []) + [f"{i1}]",
        f'{i1}"variableTargetKeys" "string_array" ', f"{i1}[", f"{i1}]",
        f'{i1}"variableNames" "string_array" ', f"{i1}[", f"{i1}]",
        f'{i1}"relayPlugData" "DmePlugList"', f"{i1}{{",
        f'{i2}"id" "elementid" "{eid()}"',
        f'{i2}"names" "string_array" ', f"{i2}[", f"{i2}]",
        f'{i2}"dataTypes" "int_array" ', f"{i2}[", f"{i2}]",
        f'{i2}"plugTypes" "int_array" ', f"{i2}[", f"{i2}]",
        f'{i2}"descriptions" "string_array" ', f"{i2}[", f"{i2}]",
        f"{i1}}}", "",
        f'{i1}"connectionsData" "element_array" ', f"{i1}["] + ([",\n".join(conn_blocks)] if conn_blocks else []) + [
        f"{i1}]",
        f'{i1}"entity_properties" "EditGameClassProps"', f"{i1}{{",
        f'{i2}"id" "elementid" "{eid()}"'] + prop_lines + [
        f"{i1}}}", "",
        f'{i1}"hitNormal" "vector3" "0 0 1"',
        f'{i1}"isProceduralEntity" "bool" "0"',
        f'{i1}"origin" "vector3" "{origin}"',
        f'{i1}"angles" "qangle" "{angles}"',
        f'{i1}"scales" "vector3" "1 1 1"',
        f'{i1}"transformLocked" "bool" "0"',
        f'{i1}"transformPin" "DmElement"', f"{i1}{{",
        f'{i2}"id" "elementid" "{eid()}"',
        f'{i2}"name" "string" "transformPin"',
        f'{i2}"referenceName" "string" ""',
        f'{i2}"targetReferenceID" "uint64" "0x0"',
        f'{i2}"offsetOrigin" "vector3" "0 0 0"',
        f'{i2}"offsetAngles" "qangle" "0 0 0"',
        f'{i2}"pinAngles" "bool" "1"',
        f'{i2}"twoWay" "bool" "0"',
        f"{i1}}}", "",
        f'{i1}"force_hidden" "bool" "0"',
        f'{i1}"editorOnly" "bool" "0"',
        f'{i1}"customVisGroup" "string" ""',
        f'{i1}"randomSeed" "int" "{random.randint(1, 2 ** 31 - 1)}"']
    lines += [f'{i1}"{n}" "{typ}" "{v}"' for n, typ, v in tail]
    lines.append(f"{ind}}}")
    return "\n".join(lines)


def _entity_block(ind, props, origin="0 0 0", conns=()):
    return element_block(ind, props, origin=origin, conns=conns)


# bhop_ maps get these at the world origin, like the server settings of a bhop server
SERVER_SETTINGS = ("sv_enablebunnyhopping 1;\nsv_autobunnyhopping 1;\nsv_staminamax 0;\nsv_staminajumpcost 0;\n"
                   "sv_staminalandcost 0;\nsv_staminarecoveryrate 0;\nsv_airaccelerate 2000;\n"
                   "sv_accelerate_use_weapon_speed 0;\nsv_falldamage_scale 0;\nsv_maxvelocity 3500;\n"
                   "sv_ladder_scale_speed 1;\nsv_legacy_jump 1\nmp_freezetime 0;\nmp_team_intro_time 0;\n"
                   "mp_roundtime 60;\nmp_solid_teammates 0;\nmp_ignore_round_win_conditions 1;\n"
                   "mp_drop_knife_enable 1; weapon_accuracy_nospread 1;")
ENTITY_SPACING = 16


def _has_entity(texts, cls, name=None):
    needle = f'"classname" "string" "{cls}"'
    for tx in texts:
        if needle not in tx:
            continue
        for m in _PROPS_RE.finditer(tx):
            p = _parse_props(tx[m.start():_close(tx, m.end() - 1)])
            if p.get("classname") == cls and (name is None or p.get("targetname", "").lower() == name):
                return True
    return False


def world_entity_blocks(texts, ind, bhop_script=False, commands=False):
    """Blocks for the entities added at the world origin, side by side ENTITY_SPACING apart:
    point_servercommand 'server', point_clientcommand 'client' and a logic_auto that sends the
    bhop server settings to both (bhop_ maps), then the bhop script entities. Entities the
    map already has are not added. Returns (blocks, names of the added classes)."""
    joined_has = lambda pat: any(re.search(pat, tx) for tx in texts)  # noqa: E731
    items = []
    if commands:
        for cls, name in (("point_servercommand", "server"), ("point_clientcommand", "client")):
            if not _has_entity(texts, cls, name):
                items.append(([("classname", cls), ("targetname", name)], ()))
        items.append(([("classname", "logic_auto")],
                      [{"output": "OnMapSpawn", "target": tgt, "input": "Command", "param": SERVER_SETTINGS}
                       for tgt in ("client", "server")]))
    if bhop_script:
        if not joined_has(re.escape(f'"cs_script" "string" "{BHOP_SCRIPT}"')):
            items.append(([("classname", "point_script"), ("targetname", ""), ("cs_script", BHOP_SCRIPT)], ()))
        if not joined_has(f'"targetname" "string" "{BHOP_CASE}"'):
            items.append(([("classname", "logic_case"), ("targetname", BHOP_CASE), ("vscripts", "")]
                          + [(f"Case{n:02d}", "") for n in range(1, 33)], ()))
    blocks = [_entity_block(ind, props, origin=f"{k * ENTITY_SPACING} 0 0", conns=conns)
              for k, (props, conns) in enumerate(items)]
    return blocks, [props[0][1] for props, _c in items]


VIS_HINT_CLASS = "visibility_hint"
VIS_HINT_TYPE = "9"         # "Use Lowest Resolution (256 unit grid, fewest initial clusters)"


def _vis_hint_box(bounds):
    """(origin, box_mins, box_maxs) texts of a box around bounds, the origin in its center."""
    lo = [math.floor(v + 0.01) for v in bounds[0]]
    hi = [math.ceil(v - 0.01) for v in bounds[1]]
    center = [(a + b) / 2.0 for a, b in zip(lo, hi)]
    half = [(b - a) / 2.0 for a, b in zip(lo, hi)]
    return (" ".join(_num(c) for c in center), " ".join(f"{-h:.6f}" for h in half),
            " ".join(f"{h:.6f}" for h in half))


def set_visibility_hint(text, bounds, ind):
    """A visibility_hint that covers the whole map (bounds = (mins, maxs)) with the 256 unit grid.
    A visibility_hint the map already has gets the new box. Returns (text, added)."""
    origin, mins, maxs = _vis_hint_box(bounds)
    props = {"box_mins": mins, "box_maxs": maxs, "hintType": VIS_HINT_TYPE}
    text, n = edit_entities(text, lambda p, _o: props if p.get("classname") == VIS_HINT_CLASS else None)
    if n:
        out, pos = [], 0
        for m in re.finditer(r'"classname" "string" "%s"' % VIS_HINT_CLASS, text):
            om = _ORIGIN_ANY_RE.search(text, m.end(), m.end() + 2000)
            if om and om.start() >= pos:
                out.append(text[pos:om.start(1)])
                out.append(origin)
                pos = om.end(1)
        out.append(text[pos:])
        return "".join(out), False
    block = element_block(ind, [("classname", VIS_HINT_CLASS), ("box_mins", mins), ("box_maxs", maxs),
                                ("hintType", VIS_HINT_TYPE)], origin=origin)
    return add_to_world(text, [block]), True


LIGHT_ROOM_NAME = "cs2porter_light_room"
ROOM_MATERIAL = "materials/dev/primary_white.vmat"
ROOM_MESH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "light_room_mesh.txt")
# the light of a room, as in a hand made example map (only the range is made to fit the room)
ROOM_LIGHT = (("clientSideEntity", "1"), ("brightness_units", "1"), ("brightness_candelas", "12"),
              ("brightness_nits", "1472"), ("brightness", "-2.73828"), ("brightness_legacy", "0.148438"),
              ("enabled", "1"), ("directlight", "1"), ("colormode", "1"), ("color", "255 255 255"),
              ("colortemperature", "6600"), ("brightness_lumens", "150"), ("range", "512"), ("skirt", "0.1"),
              ("bouncelight", "-1"), ("bouncescale", "1.0"), ("shape", "0"), ("size_params", "2.0 24.0 0.15"),
              ("outer_angle", "180.0"), ("inner_angle", "180.0"), ("lightcookie", ""),
              ("bakespeculartocubemaps", "0"), ("bakespeculartocubemaps_scale", "1.0"), ("minroughness", "0"),
              ("castshadows", "2"), ("shadowmapsize", "-1"), ("shadowpriority", "-1"), ("pvs_modify_entity", "0"),
              ("shadowfade_size_start", ".10"), ("shadowfade_size_end", ".05"), ("brightnessscale", "1.0"),
              ("rendertocubemaps", "1"), ("showlight", "0"), ("fade_size_start", ".05"),
              ("fade_size_end", ".025"), ("transmit_always", "0"))


def _room_mesh(ind, origin):
    with open(ROOM_MESH_FILE, "r", encoding="utf-8") as f:
        text = f.read().rstrip("\n")
    text = re.sub(r"\$ID", lambda _m: str(uuid.uuid4()), text)
    text = text.replace("$REF", f"0x{random.getrandbits(64):016x}").replace("$ORIGIN", origin)
    text = text.replace("$SEED", str(random.randint(1, 2 ** 31 - 1)))
    return "\n".join(ind + line if line else line for line in text.split("\n"))


def light_room_blocks(ind, rooms):
    """Elements of the light probe volumes (probes.plan()): every volume bakes its cubemap in its
    own white room (a mesh with a light); a volume without a room bakes it in its middle. The
    volumes fade out at their edges, where they overlap their neighbours."""
    blocks = []
    fade = " ".join([_num(probesmod.EDGE_FADE)] * 3)
    for n, (lo, hi, room) in enumerate(rooms, 1):
        name = f"{LIGHT_ROOM_NAME}_{n}"
        c = room or tuple(round((lo[k] + hi[k]) / 2.0) for k in range(3))
        origin = " ".join(_num(v) for v in c)
        mins = " ".join(f"{lo[k] - c[k]:.6f}" for k in range(3))
        maxs = " ".join(f"{hi[k] - c[k]:.6f}" for k in range(3))
        blocks.append(element_block(ind, [
            ("classname", "env_combined_light_probe_volume"), ("targetname", name), ("StartDisabled", "0"),
            ("cubemaptexture", ""), ("bakefarz", "4096.0"), ("box_mins", mins), ("box_maxs", maxs),
            ("voxel_size", probesmod.VOXEL_SIZE), ("flood_fill", "0"), ("voxelize", "0"),
            ("indoor_outdoor_level", "0"), ("edge_fade_dists", fade), ("clientSideEntity", "1")], origin=origin))
        if room:
            blocks.append(element_block(ind, [("classname", "light_omni2"), ("targetname", name + "_light")]
                                        + list(ROOM_LIGHT), origin=origin))
            blocks.append(_room_mesh(ind, origin))
    return blocks


def remove_light_rooms(text):
    """Drops the light probe volumes and rooms of an earlier port. Returns (text, count)."""
    try:
        start, end = _world_children(text)
    except ValueError:
        return text, 0
    elems = _split_elements(text, start, end)
    places, keep = set(), []
    for e in elems:
        m = re.search(r'"targetname" "string" "%s_\d+(?:_light)?"' % LIGHT_ROOM_NAME, e)
        if m and e.lstrip().startswith('"CMapEntity"'):
            om = _ORIGIN_ANY_RE.search(e, m.end())
            if om:
                places.add(om.group(1))
            continue
        keep.append(e)
    if len(keep) == len(elems):
        return text, 0
    out = []
    for e in keep:
        if e.lstrip().startswith('"CMapMesh"') and ROOM_MATERIAL in e and e.count("-128 -127.9999923706 128"):
            om = re.search(r'\n[ \t]*"origin" "vector3" "([^"]*)"\s*\n[ \t]*"angles"', e)
            if om and om.group(1) in places:
                continue
        out.append(e)
    m = re.match(r"[ \t]*", text[text.rfind("\n", 0, start) + 1:])
    return _join_world(text, start, end, out, m.group(0) if m else "\t\t"), len(elems) - len(out)


SURFACE_LIGHT_NAME = "cs2porter_glow"


def surface_light_blocks(ind, lights):
    """light_rect entities in front of glowing surfaces (texlights.rect_lights)."""
    blocks = []
    for n, L in enumerate(lights, 1):
        size = _num(round(L["size"], 1))
        blocks.append(element_block(ind, [
            ("classname", "light_rect"), ("targetname", f"{SURFACE_LIGHT_NAME}_{n}"),
            ("brightness_units", "1"), ("brightness_lumens", _num(round(L["lumens"], 1))),
            ("enabled", "1"), ("directlight", "1"), ("colormode", "0"), ("color", L["color"]),
            ("range", _num(L["range"])), ("skirt", "0.1"), ("bouncelight", "-1"), ("bouncescale", "1.0"),
            ("shape", "0"), ("size_params", f"{size} {size} 0.15"), ("bakespeculartocubemaps", "0"),
            ("castshadows", "2"), ("rendertocubemaps", "1")],
            origin=" ".join(_num(round(v, 2)) for v in L["origin"]), angles=L["angles"]))
    return blocks


def remove_named(text, prefix):
    """Drops the world entities named prefix_<n> (left by an earlier port). Returns (text, count)."""
    try:
        start, end = _world_children(text)
    except ValueError:
        return text, 0
    elems = _split_elements(text, start, end)
    pat = re.compile(r'"targetname" "string" "%s_\d+"' % re.escape(prefix))
    keep = [e for e in elems if not (e.lstrip().startswith('"CMapEntity"') and pat.search(e))]
    if len(keep) == len(elems):
        return text, 0
    m = re.match(r"[ \t]*", text[text.rfind("\n", 0, start) + 1:])
    return _join_world(text, start, end, keep, m.group(0) if m else "\t\t"), len(elems) - len(keep)


def bhop_entity_blocks(texts, ind):
    """Blocks for the bhop entities that are not in the map yet."""
    return world_entity_blocks(texts, ind, bhop_script=True)[0]


def add_to_world(text, blocks):
    start, end = _world_children(text)
    elems = _split_elements(text, start, end)
    m = re.match(r"[ \t]*", text[text.rfind("\n", 0, start) + 1:])
    return _join_world(text, start, end, elems + blocks, m.group(0) if m else "\t\t")


def _join_world(text, start, end, elems, ind):
    body = ",\n".join(e.rstrip() for e in elems)
    close_ind = ind
    return text[:start] + ("\n" + body + "\n" + close_ind if elems else "\n" + close_ind) + text[end:]


def renumber_nodes(text):
    counter = iter(range(1, 10 ** 9))
    return _NODE_RE.sub(lambda m: f'{m.group(1)}{next(counter)}"', text)


# --- element tree (meshes from func_brush, helper entities) ------------------------

_CHILDREN_RE = re.compile(r'"children" "element_array" *\r?\n[ \t]*\[')


def _children_span(elem):
    m = _CHILDREN_RE.search(elem)
    if not m:
        return None
    return m.end(), _close(elem, m.end() - 1) - 1


def _transform_list(elems, fn):
    out, changed = [], False
    for e in elems:
        r = fn(e)
        if r is not None:
            out += r
            changed = True
            continue
        if e.lstrip().startswith('"CMapGroup"'):
            span = _children_span(e)
            if span:
                sub = _split_elements(e, *span)
                new, ch = _transform_list(sub, fn)
                if ch:
                    ind = re.match(r"[ \t]*", e[e.rfind("\n", 0, span[0]) + 1:]).group(0)
                    e = _join_world(e, span[0], span[1], new, ind)
                    changed = True
        out.append(e)
    return out, changed


def transform_world(text, fn):
    """fn(element_text) -> list of elements to put in its place, or None to keep it.
    Groups are walked too. Returns (text, changed)."""
    start, end = _world_children(text)
    elems = _split_elements(text, start, end)
    new, changed = _transform_list(elems, fn)
    if not changed:
        return text, False
    ind = re.match(r"[ \t]*", text[text.rfind("\n", 0, start) + 1:]).group(0)
    return _join_world(text, start, end, new, ind), True


def _entity_props(elem):
    i = elem.rfind('"entity_properties" "EditGameClassProps"')
    if i < 0:
        return {}
    m = _PROPS_RE.match(elem, i)
    if not m:
        return {}
    return _parse_props(elem[i:_close(elem, m.end() - 1)])


def _set_mesh_attr(mesh, name, typ, value):
    pat = re.compile(r'("%s" "%s" ")[^"]*(")' % (re.escape(name), re.escape(typ)))
    return pat.sub(lambda m: m.group(1) + value + m.group(2), mesh, count=1)


def unwrap_entities(text, meshes, stats):
    """Replaces the marked func_brush entities with their meshes (mesh origins are already in
    world space) and removes the helper info_targets."""
    def fn(e):
        if not e.lstrip().startswith('"CMapEntity"'):
            return None
        p = _entity_props(e)
        cls = p.get("classname", "")
        name = p.get("targetname", "")
        if cls == "info_target" and name.lower() in HELPER_NAMES:
            stats["helpers"] += 1
            return []
        opts = meshes.get(name) if cls == "func_brush" else None
        if opts is None:
            return None
        span = _children_span(e)
        if not span:
            return None
        out = []
        for mesh in _split_elements(e, *span):
            if is_null_element(mesh):
                out.append(mesh)        # reference to an element defined elsewhere
                continue
            if opts.get("physics_none"):
                mesh = _set_mesh_attr(mesh, "physicsType", "string", "none")
            if opts.get("fade"):
                mesh = _set_mesh_attr(mesh, "fademindist", "float", _num(opts["fade"][0]))
                mesh = _set_mesh_attr(mesh, "fademaxdist", "float", _num(opts["fade"][1]))
            if opts.get("no_shadows"):
                mesh = _set_mesh_attr(mesh, "disableShadows", "int", "1")
            out.append(mesh)
        stats["meshes"] += 1
        return out

    return transform_world(text, fn)


# ---------------------------------------------------------------------------
# Collapse
# ---------------------------------------------------------------------------

_TARGET_RE = re.compile(r'"targetMapPath" "string" "([^"]*)"')


_ROOT_ELEM_RE = re.compile(r'\n("[A-Za-z_]\w*")\r?\n\{')


def _extra_root_elements(text):
    """Elements written next to the root element at the top of the file (overlays the world
    only references by id are written there)."""
    out = []
    for m in _ROOT_ELEM_RE.finditer(text):
        if m.group(1) == '"CMapRootElement"':
            continue
        o = text.find("{", m.end() - 1)
        out.append(text[m.start() + 1:_close(text, o)])
    return out


def collapse(main_text, prefab_texts):
    """prefab_texts: {'maps/prefabs/x/y.vmap': text}. Returns (text, collapsed_count)."""
    start, end = _world_children(main_text)
    ind_m = re.match(r"[ \t]*", main_text[main_text.rfind("\n", 0, start) + 1:])
    elems = _split_elements(main_text, start, end)
    out, extra, n = [], [], 0
    for e in elems:
        if e.lstrip().startswith('"CMapPrefab"'):
            m = _TARGET_RE.search(e)
            key = m.group(1).replace("\\", "/").lower() if m else ""
            if key in prefab_texts:
                ptext = prefab_texts[key]
                ps, pe = _world_children(ptext)
                out += _split_elements(ptext, ps, pe)
                # the elements its world only references come along
                extra += _extra_root_elements(ptext)
                n += 1
                continue
        out.append(e)
    text = _join_world(main_text, start, end, out, ind_m.group(0) if ind_m else "\t\t")
    if extra:
        text = text.rstrip() + "\n\n" + "\n\n".join(extra) + "\n"
    if n:
        text = _drop_asset_refs(text, set(prefab_texts))
    return renumber_nodes(text), n


_ASSET_REFS_RE = re.compile(r'"map_asset_references" "string_array" *\r?\n[ \t]*\[')


def _drop_asset_refs(text, paths):
    """Removes the collapsed prefabs from map_asset_references."""
    m = _ASSET_REFS_RE.search(text)
    if not m:
        return text
    end = _close(text, m.end() - 1) - 1
    items = re.findall(r'"((?:[^"\\]|\\.)*)"', text[m.end():end])
    keep = [i for i in items if i.replace("\\", "/").lower() not in paths]
    if len(keep) == len(items):
        return text
    ind = re.match(r"[ \t]*", text[text.rfind("\n", 0, m.start()) + 1:]).group(0)
    body = ",\n".join(f'{ind}\t"{i}"' for i in keep)
    return text[:m.end()] + ("\n" + body + "\n" + ind if keep else "\n" + ind) + text[end:]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

_EFFECT_NAME_RE = re.compile(r'("effect_name" "string" ")([^"]*)(")')


def set_particle_names(text, names):
    """info_particle_system effect names of Source 1 systems -> their converted .vpcf."""
    count = 0

    def fn(m):
        nonlocal count
        new = names.get(m.group(2).strip().lower())
        if not new:
            return m.group(0)
        count += 1
        return m.group(1) + new + m.group(3)

    return _EFFECT_NAME_RE.sub(fn, text), count


def _edit_text(text, fx, table, stats):
    if fx.sky_alias:
        text, n = replace_sky(text, fx.sky_alias, fx.moondome_vmat or TOOLS_SKY_VMAT)
        stats["sky"] += n
    if fx.moondome_vmat and fx.moondome_vmat in text:
        text, n = clip_meshes(text, fx.moondome_vmat)
        stats["clip"] += n
    if fx.sky_vmat and "env_sky" in text:
        text, n = set_env_sky(text, fx.sky_vmat)
        stats["envsky"] += n
    if "light_" in text:
        text, n, lum, raised = fix_lights(text, table, fx.lumens)
        stats["lights"] += n
        stats["lumens"] += lum
        stats["ranges"] = stats.get("ranges", 0) + raised
    if fx.bhop or fx.attributes:
        text, b, a, r, tp = rewrite_connections(text, fx.bhop, fx.attributes)
        stats["bhop"] += b
        stats["attr"] += a
        stats["removes"] += r
        stats["teleports"] += tp
    # the importer writes the post processing file to lighting/postprocessing/<map>/;
    # it is moved to postprocess/ (see move_postprocess)
    text = _VPOST_RE.sub(r"postprocess/\1", text)
    text, n = strip_plus(text)
    stats["plus"] += n
    if fx.particle_names and '"effect_name"' in text:
        text, n = set_particle_names(text, fx.particle_names)
        stats["particle_names"] = stats.get("particle_names", 0) + n
    from .materials import BLACK_MATERIALS, BLACK_VMAT
    for name in BLACK_MATERIALS:
        old = f'"materials/{name}.vmat"'
        if old in text:
            stats["black"] = stats.get("black", 0) + text.count(old)
            text = text.replace(old, f'"materials/{BLACK_VMAT}.vmat"')
    try:
        text, _ch = unwrap_entities(text, fx.meshes, stats)
    except ValueError:
        pass
    from . import vmap_ents as ents
    for name, step in (("glows", lambda tx: ents.convert_glows(tx, stats)),
                       ("effects", lambda tx: ents.set_effects(tx, fx.effects, stats)),
                       ("entities", lambda tx: ents.convert_elements(tx, fx, stats)),
                       ("ambients", lambda tx: ents.set_ambient_events(tx, fx.ambients, stats))):
        try:
            text = step(text)
        except (ValueError, IndexError, KeyError) as e:
            stats.setdefault("errors", []).append(f"{name}: {e}")
    if fx.fgd is not None:
        text = remove_unused_props(text, fx.fgd, stats)
    return text


PORT_MARK_KEY = "ported_with"
_WORLDSPAWN_RE = re.compile(r'^([ \t]*)"classname" "string" "worldspawn"\r?\n', re.M)


def add_port_mark(text):
    """Map Properties get a key that is not used by anything (Hammer lists it under the unused
    keys): which program and version made the map."""
    from . import __version__
    text = re.sub(r'^[ \t]*"%s" "string" "[^"]*"\r?\n' % PORT_MARK_KEY, "", text, flags=re.M)
    m = _WORLDSPAWN_RE.search(text)
    if not m:
        return text
    nl = "\r\n" if m.group(0).endswith("\r\n") else "\n"
    line = f'{m.group(1)}"{PORT_MARK_KEY}" "string" "Ported with CS2 Porter {__version__} (made by tehlikeli91)"{nl}'
    return text[:m.end()] + line + text[m.end():]


def _paint(text, fx, stats):
    """Blend paint on the displacements (needs world space meshes, so after the collapse) and
    the texture scale values Hammer needs. References to elements the edits removed go too."""
    try:
        text, _n = drop_dangling_refs(text)
    except ValueError:
        pass
    text = add_port_mark(text)
    if fx.blends:
        try:
            text = blendmod.paint_blends(text, fx.blends, stats)
        except (ValueError, IndexError, KeyError) as e:
            stats["blend_error"] = str(e)
    if fx.uv_sizes is not None:
        from . import uvfix
        try:
            text = uvfix.fix_texture_scales(text, fx.uv_sizes, stats,
                                            {fx.moondome_vmat: SKY_FACE_SCALE} if fx.moondome_vmat else None)
        except (ValueError, IndexError, KeyError) as e:
            stats.setdefault("errors", []).append(f"uv: {e}")
        try:
            text = uvfix.fix_animated_uvs(text, fx.uv_sizes, stats)
        except (ValueError, IndexError, KeyError) as e:
            stats.setdefault("errors", []).append(f"anim uv: {e}")
    return text


_VPOST_RE = re.compile(r"lighting/postprocessing/[^\"/]+/([^\"/]+\.vpost)")


def move_postprocess(content_dir, overwrite=False):
    """lighting/postprocessing/<map>/*.vpost -> postprocess/; the lighting folder is removed
    when nothing else is left in it. Returns the number of files moved."""
    base = os.path.join(content_dir, "lighting")
    pp = os.path.join(base, "postprocessing")
    if not os.path.isdir(pp):
        return 0
    dst_dir = os.path.join(content_dir, "postprocess")
    moved = 0
    for dp, _dn, fn in os.walk(pp):
        for f in fn:
            if not f.lower().endswith(".vpost"):
                continue
            src = os.path.join(dp, f)
            dst = os.path.join(dst_dir, f)
            os.makedirs(dst_dir, exist_ok=True)
            if overwrite or not os.path.isfile(dst):
                shutil.copyfile(src, dst)
            os.remove(src)
            moved += 1
    for dp, _dn, _fn in sorted(os.walk(base), key=lambda x: -len(x[0])):
        try:
            os.rmdir(dp)
        except OSError:
            pass
    return moved


def post_process(valve, work_dir, maps_dir, map_name, fx, log=None):
    """Applies every .vmap edit and collapses the prefabs. Returns a Result."""
    log = log or (lambda m, tag="info": None)
    res = Result()
    files = map_files(maps_dir, map_name)
    main = os.path.join(maps_dir, f"{map_name}.vmap")
    if main not in files:
        return res
    table = {}
    for cls, pos, info in fx.lights:
        table.setdefault(_pos_key(pos), []).append((cls, info))
    total_lumens = sum(1 for v in table.values() for _c, i in v if i.get("lumens")) if fx.lumens else 0
    tmp = os.path.join(work_dir, "vmap_edit")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)

    # --- load every file as text --------------------------------------------------
    texts, binary = {}, {}
    for i, path in enumerate(files):
        binary[path] = _is_binary(path)
        src = path
        if binary[path]:
            if not valve.has_dmxconvert:
                log(t("vm_no_tool"), "warn")
                return res
            src = os.path.join(tmp, f"in{i}.vmap")
            ok, _out = valve.dmxconvert(path, src, "keyvalues2")
            if not ok:
                log(t("vm_fail", file=os.path.basename(path), e="dmxconvert"), "warn")
                return res
        with open(src, "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
            texts[path] = f.read()

    # --- edits ----------------------------------------------------------------------
    stats = dict.fromkeys(("sky", "clip", "envsky", "lights", "lumens", "bhop", "attr", "removes",
                           "teleports", "meshes", "helpers", "plus", "blend_faces", "blend_meshes",
                           "blend_miss", "props", "props_ents", "glows", "beams", "beams_found",
                           "beams_removed", "unsupported_inputs", "cables", "buttons", "water",
                           "ambients", "uv_faces", "uv_meshes", "ropes_removed", "cables_black",
                           "detail_groups", "effects", "preset_faces"), 0)
    res.stats = stats
    orig = dict(texts)
    for path in files:
        texts[path] = _edit_text(texts[path], fx, table, stats)
    res.bhop_outputs = stats["bhop"]
    want_script = bool(fx.bhop and stats["bhop"])
    added = []
    if want_script or fx.commands:
        try:
            start, _end = _world_children(texts[main])
            ind = re.match(r"[ \t]*", texts[main][texts[main].rfind("\n", 0, start) + 1:]).group(0) + "\t"
            blocks, added = world_entity_blocks(texts.values(), ind, want_script, fx.commands)
            if blocks:
                texts[main] = renumber_nodes(add_to_world(texts[main], blocks))
            if want_script:
                res.bhop_entities = sum(1 for c in added if c in ("point_script", "logic_case"))
        except ValueError as e:
            log(t("vm_fail", file=os.path.basename(main), e=e), "warn")
    if fx.vis_bounds:
        try:
            start, _end = _world_children(texts[main])
            ind = re.match(r"[ \t]*", texts[main][texts[main].rfind("\n", 0, start) + 1:]).group(0) + "\t"
            texts[main], added_hint = set_visibility_hint(texts[main], fx.vis_bounds, ind)
            if added_hint:
                texts[main] = renumber_nodes(texts[main])
            stats["vis_hint"] = 1
        except ValueError as e:
            log(t("vm_fail", file=os.path.basename(main), e=e), "warn")
    if fx.surface_lights or stats.get("underwater"):
        try:
            texts[main], _old = remove_named(texts[main], SURFACE_LIGHT_NAME)
            if fx.surface_lights:
                start, _end = _world_children(texts[main])
                ind = re.match(r"[ \t]*", texts[main][texts[main].rfind("\n", 0, start) + 1:]).group(0) + "\t"
                texts[main] = add_to_world(texts[main], surface_light_blocks(ind, fx.surface_lights))
                stats["surface_lights"] = len(fx.surface_lights)
            texts[main] = renumber_nodes(texts[main])
        except ValueError as e:
            log(t("vm_fail", file=os.path.basename(main), e=e), "warn")
    if fx.light_rooms:
        try:
            texts[main], _old = remove_light_rooms(texts[main])
            start, _end = _world_children(texts[main])
            ind = re.match(r"[ \t]*", texts[main][texts[main].rfind("\n", 0, start) + 1:]).group(0) + "\t"
            texts[main] = renumber_nodes(add_to_world(texts[main], light_room_blocks(ind, fx.light_rooms)))
            stats["light_volumes"] = len(fx.light_rooms)
            stats["light_rooms"] = sum(1 for r in fx.light_rooms if r[2])
        except (ValueError, OSError) as e:
            log(t("vm_fail", file=os.path.basename(main), e=e), "warn")

    # --- collapse the prefabs into the main map -------------------------------------
    rel = {}
    for path in files[1:]:
        key = os.path.relpath(path, os.path.dirname(maps_dir)).replace("\\", "/").lower()
        rel[key] = texts[path]
    written = False
    if fx.collapse and rel:
        try:
            text, n = collapse(texts[main], rel)
            text = _paint(text, fx, stats)
            edited = os.path.join(tmp, "collapsed.vmap")
            out_bin = os.path.join(tmp, "collapsed_bin.vmap")
            with open(edited, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
                f.write(text)
            ok, _out = valve.dmxconvert(edited, out_bin, "binary")
            if not ok:
                raise ValueError("dmxconvert")
            shutil.copyfile(out_bin, main)
            res.collapsed = n
            written = True
        except (ValueError, OSError) as e:
            log(t("vm_collapse_fail", e=e), "warn")
        if written:
            # the prefab files are not used by the map any more
            for path in files[1:]:
                try:
                    os.remove(path)
                    res.prefabs_removed += 1
                except OSError:
                    pass
            pre = os.path.join(maps_dir, "prefabs")
            for d in (os.path.join(pre, map_name), pre):
                try:
                    os.rmdir(d)
                except OSError:
                    pass

    if not written:
        # without the collapse only the main map is in world space
        texts[main] = _paint(texts[main], fx, stats)
        for path in files:
            if texts[path] == orig[path]:
                continue
            try:
                if binary[path]:
                    edited = os.path.join(tmp, "edited.vmap")
                    out_bin = os.path.join(tmp, "out.vmap")
                    with open(edited, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
                        f.write(texts[path])
                    ok, _out = valve.dmxconvert(edited, out_bin, "binary")
                    if not ok:
                        raise OSError("dmxconvert")
                    shutil.copyfile(out_bin, path)
                else:
                    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
                        f.write(texts[path])
            except OSError as e:
                log(t("vm_fail", file=os.path.basename(path), e=e), "warn")
    shutil.rmtree(tmp, ignore_errors=True)

    # --- log --------------------------------------------------------------------------
    if stats["sky"]:
        log(t("vm_moondome", n=stats["clip"]) if fx.moondome_vmat else t("vm_sky", n=stats["sky"]), "ok")
    if stats["envsky"]:
        log(t("vm_envsky", vmat=fx.sky_vmat), "ok")
    if stats["lights"]:
        log(t("vm_lights_fixed", n=stats["lights"]), "ok")
    if total_lumens:
        log(t("vm_lights", n=stats["lumens"]), "ok")
        if stats.get("ranges"):
            log(t("vm_light_ranges", n=stats["ranges"]), "ok")
        if total_lumens - stats["lumens"]:
            log(t("vm_lights_left", n=total_lumens - stats["lumens"]), "dim")
    if stats.get("vis_hint"):
        lo, hi = fx.vis_bounds
        log(t("vm_vis_hint", size=" x ".join(_num(round(hi[k] - lo[k])) for k in range(3))), "ok")
    if stats.get("light_volumes"):
        log(t("vm_light_rooms", n=stats["light_volumes"], rooms=stats["light_rooms"]), "ok")
        if stats["light_rooms"] < stats["light_volumes"]:
            log(t("vm_light_rooms_left", n=stats["light_volumes"] - stats["light_rooms"]), "dim")
    if stats["meshes"]:
        log(t("vm_meshes", n=stats["meshes"]), "ok")
    if stats["helpers"]:
        log(t("vm_helpers", n=stats["helpers"]), "dim")
    if stats["plus"]:
        log(t("vm_plus", n=stats["plus"]), "ok")
    if stats.get("black"):
        log(t("vm_black", n=stats["black"]), "ok")
    if stats.get("particle_names"):
        log(t("vm_particle_names", n=stats["particle_names"]), "ok")
    if stats["blend_faces"]:
        log(t("vm_blend", n=stats["blend_faces"], m=stats["blend_meshes"]), "ok")
    if stats.get("blend_error"):
        log(t("vm_blend_fail", e=stats["blend_error"]), "warn")
    if stats["glows"]:
        log(t("vm_glows", n=stats["glows"]), "ok")
    if stats["beams_found"]:
        log(t("vm_beams", n=stats["beams"], r=stats["beams_removed"]), "warn")
    if stats["unsupported_inputs"]:
        log(t("vm_inputs_left", n=stats["unsupported_inputs"]), "warn")
    if stats["cables"]:
        log(t("vm_cables", n=stats["cables"]), "ok")
    if stats["cables_black"]:
        log(t("vm_cables_black", n=stats["cables_black"]), "warn")
    if stats["ropes_removed"]:
        log(t("vm_ropes_removed", n=stats["ropes_removed"]), "dim")
    if stats["detail_groups"]:
        log(t("vm_detail_groups", n=stats["detail_groups"]), "ok")
    if stats["effects"]:
        log(t("vm_effects", n=stats["effects"]), "ok")
    if stats["buttons"]:
        log(t("vm_buttons", n=stats["buttons"]), "ok")
    if stats["water"]:
        log(t("vm_water", n=stats["water"]), "ok")
    if stats.get("underwater"):
        log(t("vm_underwater", n=stats["underwater"]), "ok")
    if stats.get("surface_lights"):
        log(t("vm_surface_lights", n=stats["surface_lights"]), "ok")
    if stats["ambients"]:
        log(t("vm_ambients", n=stats["ambients"]), "ok")
    if stats["uv_faces"]:
        log(t("vm_uv", n=stats["uv_faces"], m=stats["uv_meshes"]), "ok")
    if stats["preset_faces"]:
        log(t("vm_sky_faces", n=stats["preset_faces"]), "ok")
    if stats.get("anim_faces"):
        log(t("vm_anim_faces", n=stats["anim_faces"]), "ok")
    for err in stats.get("errors", []):
        log(t("vm_step_fail", e=err), "warn")
    if stats["props"]:
        log(t("vm_props", n=stats["props"], e=stats["props_ents"]), "dim")
    if stats["attr"]:
        log(t("vm_attr", n=stats["attr"], r=stats["removes"]), "ok")
    if stats["teleports"]:
        log(t("vm_teleports", n=stats["teleports"]), "ok")
    if fx.bhop:
        if stats["bhop"]:
            log(t("vm_outputs", n=stats["bhop"]), "ok")
            if res.bhop_entities:
                log(t("vm_entities"), "ok")
            elif res.bhop_entities == 0:
                log(t("vm_entities_exist"), "dim")
        else:
            log(t("vm_no_bhop"), "dim")
    cmds = [c for c in added if c not in ("point_script", "logic_case")]
    if cmds:
        log(t("vm_commands", list=", ".join(cmds)), "ok")
    if res.collapsed:
        log(t("vm_collapse", n=res.collapsed), "ok")
    if res.prefabs_removed:
        log(t("vm_prefabs_removed", n=res.prefabs_removed), "dim")
    return res
