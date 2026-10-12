"""Entity conversions made in the .vmap after the import.

- env_particle_glow (the importer's env_sprite) -> info_particle_system with glow_particle.vpcf
  (data control point 1 = alpha, radius, self illum; tint control point 2 = color)
- env_beam / env_laser crash CS2 -> info_particle_system with beam_particle.vpcf between the
  start and end entities, or removed when they have no end
- path_particle_rope (the importer's move_rope chain, excluded from CS2) -> cable_static with
  the rope's radius, material, collision and its slack as bezier tangents
- func_button with an OnDamaged output -> func_physbox that cannot move or break
- meshes with a water material -> func_water, plus a post processing volume of the same shape
  that gives the camera under the surface an underwater look
- ambient_generic -> plays the soundevent made for it (sounds.ambient_events)
"""

import math
import random
import re
import uuid

from .materials import BROKEN_CABLE_MATERIALS, CABLE_FALLBACK, out_name
from .vmf import color_text, cp_text
from .vmap import (_PROPS_RE, _apply_props, _children_span, _close, _entity_props, _num, _pos_key,
                   _split_elements, edit_entities, element_block, is_null_element, transform_world)

GLOW_VPCF = "particles/glow_particle.vpcf"
BEAM_VPCF = "particles/beam_particle.vpcf"
BEAM_SCROLL_UNITS = 10.0

PHYSBOX_FLAGS = 32768 | 1048576         # Motion Disabled, Start Asleep
PHYSBOX_PROPS = {"classname": "func_physbox", "spawnflags": str(PHYSBOX_FLAGS),
                 "material": "10",          # Material Type: None
                 "nodamageforces": "1",     # Damaging it Doesn't Push It
                 "Damagetype": "-1",        # Impact Damage Type: Disabled
                 "health": "0"}             # Strength 0: never breaks
BUTTON_DAMAGE_ACTIVATES = 512
_BUTTON_ONLY_OUTPUTS = ("onpressed", "onunpressed", "onin", "onout", "onuselocked")


def _float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _set_elem_props(e, changes):
    """Applies {key: value / None} to the entity's own properties block."""
    i = e.rfind('"entity_properties" "EditGameClassProps"')
    m = _PROPS_RE.match(e, i)
    if i < 0 or not m:
        return e
    end = _close(e, m.end() - 1)
    return e[:i] + _apply_props(e[i:end], changes) + e[end:]


_CONN_ARRAY_RE = re.compile(r'"connectionsData" "element_array" *\r?\n[ \t]*\[')
_CONN_BLOCK_RE = re.compile(r'[ \t]*"DmeConnectionData"\s*\{.*?\}', re.S)
_FIELD_RE = re.compile(r'("(\w+)" "\w+" ")((?:[^"\\]|\\.)*)(")')


def _conn_fields(block):
    return {m.group(2): m.group(3) for m in _FIELD_RE.finditer(block)}


def _set_conn_field(block, name, value):
    return re.sub(r'("%s" "\w+" ")((?:[^"\\]|\\.)*)(")' % re.escape(name),
                  lambda m: m.group(1) + value + m.group(3), block, count=1)


def _edit_own_conns(e, fn):
    """fn(block, fields) -> new block text or None to drop it; for the entity's own outputs."""
    starts = list(_CONN_ARRAY_RE.finditer(e))
    if not starts:
        return e
    m = starts[-1]
    end = _close(e, m.end() - 1) - 1
    body = e[m.end():end]
    blocks = [b.group(0) for b in _CONN_BLOCK_RE.finditer(body)]
    out = []
    for b in blocks:
        r = fn(b, _conn_fields(b))
        if r is not None:
            out.append(r)
    ind = re.match(r"[ \t]*", e[e.rfind("\n", 0, m.start()) + 1:]).group(0)
    new = ("\n" + ",\n".join(x.rstrip() for x in out) + "\n" + ind) if out else ("\n" + ind)
    return e[:m.end()] + new + e[end:]


def rename_inputs(text, names, mapping, stats=None, unsupported=()):
    """Connections whose target is in names: input renamed by mapping {old lower: new}.
    Inputs in unsupported are counted in stats['unsupported_inputs']."""
    if not names:
        return text

    def repl(m):
        b = m.group(0)
        f = _conn_fields(b)
        if f.get("targetName", "").lower() not in names:
            return b
        inp = f.get("inputName", "").lower()
        if inp in mapping:
            return _set_conn_field(b, "inputName", mapping[inp])
        if stats is not None and inp in unsupported:
            stats["unsupported_inputs"] += 1
        return b

    return _CONN_BLOCK_RE.sub(repl, text)


# ---------------------------------------------------------------------------
# glow sprites
# ---------------------------------------------------------------------------

def glow_vpcf(mat):
    """Glow particle that draws with the texture of the sprite material mat."""
    return f"particles/cs2porter/glow_{re.sub(r'[^a-z0-9_]+', '_', out_name(mat).lower()).strip('_')}.vpcf"


def _sprite_material(name):
    n = (name or "").strip().replace("\\", "/").lower()
    if n.startswith("materials/"):
        n = n[len("materials/"):]
    return n[:-4] if n.endswith((".vmt", ".spr")) else n


GLOW_INPUTS = {"showsprite": "Start", "hidesprite": "DestroyImmediately", "stop": "DestroyImmediately"}
BEAM_INPUTS = {"turnon": "Start", "turnoff": "DestroyImmediately"}


def convert_glows(text, stats, sprite_textures=None, glow_particles=None, renames=None):
    """sprite_textures: {sprite material: texture}; glow_particles receives {vpcf: texture} for
    the glows that got a particle drawing with their own sprite texture; renames receives
    (names, input mapping) for the inputs sent to them from the map's other files."""
    if '"env_particle_glow"' not in text:
        return text
    names = set()
    if renames is not None:
        renames.append((names, GLOW_INPUTS))

    def fn(p, _origin):
        if p.get("classname") != "env_particle_glow":
            return None
        stats["glows"] += 1
        if p.get("targetname"):
            names.add(p["targetname"].lower())
        alpha = _float(p.get("alphascale"), 1.0)
        scale = _float(p.get("scale"), 1.0)
        illum = _float(p.get("selfillumscale"), 1.0)
        effect = GLOW_VPCF
        mat = _sprite_material(p.get("s1 env_sprite material"))
        if sprite_textures and mat in sprite_textures:
            effect = glow_vpcf(mat)
            if glow_particles is not None:
                glow_particles[effect] = sprite_textures[mat]
        return {"classname": "info_particle_system", "effect_name": effect,
                "start_active": p.get("start_active") or "1",
                "data_cp": "1", "data_cp_value": cp_text(f"{_num(alpha)} {_num(scale)} {_num(illum)}"),
                "tint_cp": "2", "tint_cp_color": color_text(p.get("colortint")),
                "scale": None, "colortint": None, "selfillumscale": None, "alphascale": None,
                "effect_textureoverride": None}

    text, _n = edit_entities(text, fn)
    # the glow particle never dies by itself: hiding it means removing it
    text = rename_inputs(text, names, GLOW_INPUTS, stats, ("togglesprite",))
    return text


# ---------------------------------------------------------------------------
# element conversions, all in one walk over the map
# ---------------------------------------------------------------------------

def convert_elements(text, fx, stats):
    """Beams, ropes, buttons and water in one pass over the world (each step only when the
    text has what it looks for)."""
    steps, after = [], []
    if '"env_beam"' in text or '"env_laser"' in text:
        fn, names = _beam_step(stats)
        steps.append(fn)
        after.append(lambda tx: rename_inputs(tx, names, BEAM_INPUTS, stats, ("toggle",)))
        renames = getattr(fx, "renames", None)
        if renames is not None:
            renames.append((names, BEAM_INPUTS))
    if "path_particle_rope" in text:
        steps.append(_rope_step(fx.ropes, stats, fx.cable_missing))
    if '"func_button"' in text and "OnDamaged" in text:
        steps.append(_button_step(stats))
    water = _water_step(fx.water, stats, getattr(fx, "underwater", None)) \
        if fx.water and any(w in text.lower() for w in fx.water) else None
    if water:
        steps.append(water)
    if '"CMapGroup"' in text:
        steps.append(_group_step(stats, water))
    if not steps:
        return text

    def fn(e):
        for step in steps:
            r = step(e)
            if r is not None:
                return r
        return None

    text, _ch = transform_world(text, fn)
    for step in after:
        text = step(text)
    return text


# ---------------------------------------------------------------------------
# env_beam / env_laser
# ---------------------------------------------------------------------------

def convert_beams(text, stats):
    fn, names = _beam_step(stats)
    text, _ch = transform_world(text, fn)
    return rename_inputs(text, names, BEAM_INPUTS, stats, ("toggle",))


def _beam_step(stats):
    names = set()

    def fn(e):
        if not e.lstrip().startswith('"CMapEntity"'):
            return None
        p = _entity_props(e)
        cls = p.get("classname", "")
        if cls not in ("env_beam", "env_laser"):
            return None
        stats["beams_found"] += 1
        if cls == "env_beam":
            start, end, width = p.get("lightningstart", ""), p.get("lightningend", ""), p.get("boltwidth")
        else:
            start, end, width = "", p.get("lasertarget", ""), p.get("width")
        if not end.strip():
            stats["beams_removed"] += 1
            return []
        try:
            flags = int(float(p.get("spawnflags") or 0))
        except ValueError:
            flags = 0
        radius = max(0.25, _float(width, 2.0) / 2.0)
        alpha = min(max(_float(p.get("renderamt"), 255.0) / 255.0, 0.0), 1.0)
        # Source 1 beams (beamdraw.cpp) repeat their texture every 100 units and scroll it
        # TextureScroll / 10 times a second: TextureScroll * 10 units a second
        scroll = round(_float(p.get("texturescroll"), 35.0) * BEAM_SCROLL_UNITS, 2)
        changes = {"classname": "info_particle_system", "effect_name": BEAM_VPCF,
                   "start_active": "1" if flags & 1 else "0",
                   "cpoint1": end.strip(), "data_cp": "3",
                   "data_cp_value": cp_text(f"{_num(radius)} {_num(alpha)} {_num(scroll)}"),
                   "tint_cp": "2", "tint_cp_color": color_text(p.get("rendercolor"))}
        if start.strip():
            changes["cpoint0"] = start.strip()
        if p.get("targetname"):
            names.add(p["targetname"].lower())
        stats["beams"] += 1
        return [_set_elem_props(e, changes)]

    return fn, names


# ---------------------------------------------------------------------------
# ropes -> cable_static
# ---------------------------------------------------------------------------

_ORIGIN_RE = re.compile(r'"origin" "vector3" "([^"]*)"')


def _own_origin(e):
    found = _ORIGIN_RE.findall(e)
    if not found:
        return None
    try:
        return tuple(float(v) for v in found[-1].split())
    except ValueError:
        return None


def _vfmt(v):
    return " ".join(_num(c) for c in v)


# ---------------------------------------------------------------------------
# infodecal quads back onto their surface
# ---------------------------------------------------------------------------

# the importer puts the quad of an infodecal this far in front of the face it picked
DECAL_LIFT = 16.0
_FACES_RE = re.compile(r'"faceEdgeIndices" "int_array"\s*\[([^\]]*)\]')


def _up_vector(angles):
    """Where the element's +Z points (Source angles: pitch, yaw, roll)."""
    p, y, r = (math.radians(a) for a in angles)
    sp, cp, sy, cy, sr, cr = math.sin(p), math.cos(p), math.sin(y), math.cos(y), math.sin(r), math.cos(r)
    return (cr * sp * cy + sr * sy, cr * sp * sy - sr * cy, cr * cp)


def _angles_for(n, old):
    """Angles (pitch 0) that turn +Z to n; a floor or ceiling keeps the old yaw."""
    roll = math.degrees(math.acos(max(-1.0, min(1.0, n[2]))))
    yaw = old[1] if math.hypot(n[0], n[1]) < 1e-3 else math.degrees(math.atan2(n[0], -n[1]))
    return (0.0, yaw, roll)


def _replace_last(e, regex, value):
    found = list(regex.finditer(e))
    if not found:
        return e
    m = found[-1]
    return e[:m.start(1)] + value + e[m.end(1):]


def place_decals(text, decals, stats):
    """decals: vmf.decal_surfaces(). Each infodecal's quad (a one face overlay with its material,
    DECAL_LIFT units from the decal) is put onto the decal's surface, facing out of it."""
    if not decals:
        return text
    by_mat = {}
    for point, mat, normal, origin in decals:
        by_mat.setdefault(f"materials/{out_name(mat)}.vmat", []).append([point, normal, origin, False])

    def fn(e):
        if not e.lstrip().startswith(('"CMapStaticOverlay"', '"CMapMesh"')):
            return None
        m = _MATS_RE.search(e)
        mats = [x.lower() for x in re.findall(r'"([^"]*)"', m.group(1))] if m else []
        if len(mats) != 1 or mats[0] not in by_mat:
            return None
        faces = _FACES_RE.search(e)
        if not faces or len(re.findall(r'"\d+"', faces.group(1))) != 1:
            return None
        q = _own_origin(e)
        if q is None:
            return None
        cands = [d for d in by_mat[mats[0]] if not d[3]]
        if not cands:
            return None
        d = min(cands, key=lambda c: abs(math.dist(q, c[2]) - DECAL_LIFT))
        if abs(math.dist(q, d[2]) - DECAL_LIFT) > 1.5:
            return None
        d[3] = True
        point, normal = d[0], d[1]
        ang = _ANGLES_RE.findall(e)
        try:
            old = tuple(float(v) for v in ang[-1].split()) if ang else (0.0, 0.0, 0.0)
        except ValueError:
            old = (0.0, 0.0, 0.0)
        e = _replace_last(e, _ORIGIN_RE, _vfmt(point))
        if normal is not None:
            up = _up_vector(old)
            if sum(up[k] * normal[k] for k in range(3)) < 0.999:
                e = _replace_last(e, _ANGLES_RE, _vfmt(_angles_for(normal, old)))
        stats["decals"] = stats.get("decals", 0) + 1
        return [e]

    try:
        text, _ch = transform_world(text, fn)
    except ValueError:
        pass                # a file without a world: only its top level elements
    # the importer writes these overlays at the top of the file, the world only names their id
    from .vmap import _ROOT_ELEM_RE, _close
    out, pos = [], 0
    for m in _ROOT_ELEM_RE.finditer(text):
        if m.group(1) not in ('"CMapStaticOverlay"', '"CMapMesh"') or m.start() < pos:
            continue
        end = _close(text, text.find("{", m.end() - 1))
        new = fn(text[m.start() + 1:end])
        if new:
            out += [text[pos:m.start() + 1], new[0]]
            pos = end
    if out:
        text = "".join(out) + text[pos:]
    return text


# Source 1 rope physics (rope_shared.h, rope_physics.cpp, c_rope.cpp): spring length =
# (distance + Slack + ROPESLACK_FUDGEFACTOR) / (nodes - 1), fudge factor -100, gravity 1500, 50
# steps a second and 3 constraint passes a step. The passes cannot fully undo gravity, so even a
# rope shorter than its span sags; the rope is run until it rests and the cable follows its nodes.
ROPE_SLACK_OFFSET = -100.0
ROPE_GRAVITY = 1500.0
ROPE_TIME_STEP = 1.0 / 50
ROPE_PASSES = 3
ROPE_DAMP = 0.98


def _rope_rest(a, b, slack, count):
    """Node positions of a Source 1 rope from a to b at rest."""
    dist = math.dist(a, b)
    if count <= 2 or dist < 1e-3:
        return [tuple(a), tuple(b)]
    spring = max(0.0, dist + slack + ROPE_SLACK_OFFSET) / (count - 1)
    fall = ROPE_GRAVITY * ROPE_TIME_STEP * ROPE_TIME_STEP
    pos = [[a[k] + (b[k] - a[k]) * i / (count - 1) for k in range(3)] for i in range(count)]
    prev = [p[:] for p in pos]
    for step in range(3000):
        for i in range(count):
            p, q = pos[i], prev[i]
            prev[i] = p[:]
            for k in range(3):
                p[k] += (p[k] - q[k]) * ROPE_DAMP
            p[2] -= fall
        for _ in range(ROPE_PASSES):
            for i in range(count - 1):
                n1, n2 = pos[i], pos[i + 1]
                v = (n1[0] - n2[0], n1[1] - n2[1], n1[2] - n2[2])
                d2 = v[0] * v[0] + v[1] * v[1] + v[2] * v[2]
                if d2 > spring * spring:
                    f = (1.0 - spring / math.sqrt(d2)) * 0.5
                    for k in range(3):
                        n1[k] -= v[k] * f
                        n2[k] += v[k] * f
            pos[0][:] = a
            pos[-1][:] = b
        if step > 200 and step % 50 == 0:
            moved = max(abs(pos[i][k] - prev[i][k]) for i in range(count) for k in range(3))
            if moved < 1e-4:
                break
    return [tuple(p) for p in pos]


def _rope_nodes(points, slacks, counts, widths):
    """Cable nodes that follow the Source 1 rope: [(position, in handle, out handle, width)].
    Source 1 draws a Catmull-Rom spline through the simulated nodes; the handles are the same
    curve as bezier handles (a third of the spline tangent)."""
    nodes = []
    for i in range(len(points) - 1):
        slack = slacks[i] if i < len(slacks) else 25.0
        count = counts[i] if i < len(counts) else 10
        rest = _rope_rest(points[i], points[i + 1], slack, count)
        n = len(rest)
        for j, p in enumerate(rest):
            nxt = rest[min(j + 1, n - 1)]
            prv = rest[max(j - 1, 0)]
            # Catmull-Rom tangent; at the ends the end node counts as its own neighbour
            tan = tuple((nxt[k] - prv[k]) / 2.0 for k in range(3))
            t_in = tuple(-x / 3.0 for x in tan) if j > 0 else (0.0, 0.0, 0.0)
            t_out = tuple(x / 3.0 for x in tan) if j < n - 1 else (0.0, 0.0, 0.0)
            if j == 0 and nodes:
                nodes[-1][2] = t_out           # joint with the previous rope
                continue
            nodes.append([p, t_in, t_out, widths[i]])
    return nodes


def convert_ropes(text, ropes, stats, missing=()):
    text, _ch = transform_world(text, _rope_step(ropes, stats, missing))
    return text


def _rope_step(ropes, stats, missing=()):
    """path_particle_rope (made by the importer from move_rope / keyframe_rope) -> cable_static.
    ropes: vmf.collect_ropes, keyed by the position of the first point. Ropes whose material
    is in missing get materials.CABLE_FALLBACK. A path with a single point (a rope that has no
    next key draws nothing) is removed."""
    def fn(e):
        head = e.lstrip()
        if not (head.startswith('"CMapPath"') or head.startswith('"CMapEntity"')):
            return None
        if _entity_props(e).get("classname", "") not in ("path_particle_rope", "path_particle_rope_clientside"):
            return None
        if head.startswith('"CMapEntity"'):
            # a rope entity without path nodes draws nothing in CS2
            stats["ropes_removed"] += 1
            return []
        span = _children_span(e)
        kids_in = _split_elements(e, *span) if span else []
        points = [o for o in (_own_origin(nd) for nd in kids_in) if o is not None]
        info = (ropes.get(_pos_key(points[0])) or {}) if points else {}
        if len(points) < 2 and len(info.get("points") or ()) >= 2:
            points = list(info["points"])
        if len(points) < 2:
            stats["ropes_removed"] += 1
            return []
        widths = info.get("width") or [2.0] * len(points)
        if len(widths) < len(points):
            widths = widths + [widths[-1]] * (len(points) - len(widths))
        radius = max(0.25, widths[0] / 2.0)
        nodes = _rope_nodes(points, info.get("slack") or [], info.get("segments") or [], widths)
        ind = re.match(r"[ \t]*", e).group(0)
        kids = []
        for k, (pt, t_in, t_out, width) in enumerate(nodes):
            kids.append(element_block(
                ind + "\t\t", [("classname", "path_node_cable"), ("radius_scale", _num(max(width / 2.0 / radius, 0.05))),
                               ("color_tint", "255 255 255")],
                kind="CMapPathNode", origin=_vfmt(pt),
                tail=[("inTangent", "vector3", _vfmt(t_in)), ("outTangent", "vector3", _vfmt(t_out)),
                      ("inTangentType", "int", "3" if k > 0 else "0"),
                      ("outTangentType", "int", "3" if k < len(nodes) - 1 else "0")]))
        src_mat = info.get("material") or "cable/cable"
        if src_mat in missing or src_mat in BROKEN_CABLE_MATERIALS:
            src_mat = CABLE_FALLBACK
            stats["cables_black"] += 1
        mat = f"materials/{out_name(src_mat)}.vmat"
        tail = [("interpolationType", "int", "2"), ("closedLoop", "bool", "0"),
                ("particleSnapshotSpacing", "float", "16"), ("materialName", "string", mat),
                ("tintColor", "color", "255 255 255 255"), ("lightingOriginName", "string", ""),
                ("numSides", "int", "8"), ("tessellationSpacing", "float", "16"),
                ("radius", "float", _num(radius)), ("flipFaces", "bool", "0"),
                ("textureOrientation", "int", "0"), ("textureScale", "float", "0.25"),
                ("textureRepeatsCircumference", "float", "1"), ("textureOffsetAlongPath", "float", "0"),
                ("textureOffsetCircumference", "float", "0"),
                ("collisionEnabled", "bool", "1" if info.get("collide") else "0"),
                ("physicsSimplificationError", "float", "2"), ("visOccluder", "bool", "0")]
        stats["cables"] += 1
        return [element_block(ind, [("classname", "cable_static")], kind="CMapCable",
                              origin=_vfmt(points[0]), children=kids, tail=tail)]

    return fn


# ---------------------------------------------------------------------------
# func_button with OnDamaged -> func_physbox
# ---------------------------------------------------------------------------

def convert_buttons(text, stats):
    text, _ch = transform_world(text, _button_step(stats))
    return text


def _button_step(stats):
    """func_button only fires OnDamaged unreliably in CS2; a frozen func_physbox does it well."""
    def fn(e):
        if not e.lstrip().startswith('"CMapEntity"'):
            return None
        p = _entity_props(e)
        if p.get("classname") != "func_button":
            return None
        if not any(_conn_fields(b.group(0)).get("outputName", "").lower() == "ondamaged"
                   for b in _CONN_BLOCK_RE.finditer(e)):
            return None
        try:
            damage_presses = int(float(p.get("spawnflags") or 0)) & BUTTON_DAMAGE_ACTIVATES
        except ValueError:
            damage_presses = 0

        seen = set()

        def conn(block, f):
            out = f.get("outputName", "").lower()
            if out == "onpressed" and damage_presses:
                # a shot pressed the button in Source 1: the same output now comes from OnDamaged
                block = _set_conn_field(block, "outputName", "OnDamaged")
                out = "ondamaged"
            elif out in _BUTTON_ONLY_OUTPUTS:
                return None
            key = (out, f.get("targetName", "").lower(), f.get("inputName", "").lower(),
                   f.get("overrideParam", ""), f.get("delay", ""), f.get("timesToFire", ""))
            if out == "ondamaged" and key in seen:
                return None     # OnPressed and OnDamaged often send the same thing
            seen.add(key)
            return block

        e = _edit_own_conns(e, conn)
        stats["buttons"] += 1
        return [_set_elem_props(e, dict(PHYSBOX_PROPS))]

    return fn


# ---------------------------------------------------------------------------
# func_detail groups -> plain meshes
# ---------------------------------------------------------------------------

_ANGLES_RE = re.compile(r'"angles" "qangle" "([^"]*)"')
_SCALES_RE = re.compile(r'"scales" "vector3" "([^"]*)"')


def _group_step(stats, water=None):
    """The importer puts every func_detail into a group of its own; its meshes (already in
    world space, marked visexclude like func_detail) are taken out of the group."""
    def is_identity(e):
        o, a, s = _own_origin(e), _ANGLES_RE.findall(e), _SCALES_RE.findall(e)
        try:
            return (o is not None and all(abs(v) < 1e-4 for v in o)
                    and (not a or all(abs(float(v)) < 1e-4 for v in a[-1].split()))
                    and (not s or all(abs(float(v) - 1) < 1e-4 for v in s[-1].split())))
        except ValueError:
            return False

    def fn(e):
        if not e.lstrip().startswith('"CMapGroup"'):
            return None
        span = _children_span(e)
        every = _split_elements(e, *span) if span else []
        kids = [k for k in every if not is_null_element(k)]
        if not kids or not all(k.lstrip().startswith('"CMapMesh"') for k in kids) or not is_identity(e):
            return None
        ind = re.match(r"[ \t]*", e).group(0)
        out = []
        for k in kids:
            k = _reindent(k, ind)
            out.extend((water(k) if water else None) or [k])
        # references to elements defined elsewhere stay in the world
        out += [_reindent(k, ind) for k in every if is_null_element(k)]
        stats["detail_groups"] += 1
        return out

    return fn


def _reindent(block, ind):
    """block moved to indentation ind (its own first line sets the old indentation)."""
    old = re.match(r"[ \t]*", block).group(0)
    if old == ind:
        return block
    lines = block.split("\n")
    return "\n".join(ind + ln[len(old):] if ln.startswith(old) else ln for ln in lines)


# ---------------------------------------------------------------------------
# env_steam / func_dustmotes / point_spotlight (made info_particle_system in the VMF)
# ---------------------------------------------------------------------------

def set_effects(text, effects, stats):
    """Sets the particle keys again on the info_particle_systems made from env_steam etc.
    (vmf._effects_to_particles) and renames the inputs sent to them."""
    table = (effects or {}).get("props") or {}
    if not table:
        return text

    def fn(p, origin):
        # glows and Source 1 particle systems can stand at the same place: the effect name
        # written in the VMF tells them apart
        if p.get("classname") != "info_particle_system" or origin not in table \
                or p.get("effect_name", "").lower() != table[origin]["effect_name"]:
            return None
        stats["effects"] += 1
        return dict(table[origin])

    if '"info_particle_system"' in text:
        text, _n = edit_entities(text, fn)
    # the outputs sent to them can be in another file of the map (a prefab) than the particles
    names = effects.get("names") or {}
    from .vmf import EFFECT_INPUTS
    for kind, mapping in EFFECT_INPUTS.items():
        group = {n for n, k in names.items() if k == kind}
        if group:
            text = rename_inputs(text, group, mapping, stats, ("toggle",))
    return text


# ---------------------------------------------------------------------------
# water meshes -> func_water
# ---------------------------------------------------------------------------

_MATS_RE = re.compile(r'"materials" "string_array" *\r?\n[ \t]*\[(.*?)\]', re.S)


def wrap_water(text, water_vmats, stats):
    if not water_vmats:
        return text
    text, _ch = transform_world(text, _water_step(water_vmats, stats))
    return text


UNDERWATER_TOOL = "materials/tools/toolstrigger.vmat"
UNDERWATER_MARK = "cs2porter_underwater"
# the underwater look fades in this fast when the camera goes under the surface
UNDERWATER_FADE = "0.2"
# the volume starts this far under the surface: it is on while the player's box touches it,
# so the box has to be this deep (eye height) before the head is under water
UNDERWATER_DEPTH = 64.0
UNDERWATER_MIN_HEIGHT = 8.0


def _fresh_copy(elem):
    """The same element with new element ids (one id may be in the map only once)."""
    elem = re.sub(r'"elementid" "[0-9a-fA-F-]+"', lambda _m: f'"elementid" "{uuid.uuid4()}"', elem)
    return re.sub(r'"referenceID" "uint64" "0x[0-9a-fA-F]+"',
                  lambda _m: f'"referenceID" "uint64" "0x{random.getrandbits(64):016x}"', elem)


_POSITIONS_RE = re.compile(r'"name" "string" "position:0".*?"data" "vector3_array" *\r?\n[ \t]*\[(.*?)\]', re.S)


def _lower_top(mesh, depth):
    """mesh with its top pulled down by depth units, None when the water is not that deep."""
    m = _POSITIONS_RE.search(mesh)
    if not m:
        return None
    try:
        verts = [[float(v) for v in s.split()] for s in re.findall(r'"([^"]*)"', m.group(1))]
    except ValueError:
        return None
    if not verts or any(len(v) != 3 for v in verts):
        return None
    # every column of corners (same x, y) gets its top corner lowered, so a sloped or uneven
    # surface stays the same shape (cutting at one height would fold its faces onto each other)
    cols = {}
    for i, (x, y, _z) in enumerate(verts):
        cols.setdefault((round(x, 1), round(y, 1)), []).append(i)
    new_z = [v[2] for v in verts]
    for idx in cols.values():
        zs = [verts[i][2] for i in idx]
        top, bottom = max(zs), min(zs)
        if len(idx) < 2 or top - depth - bottom < UNDERWATER_MIN_HEIGHT:
            return None
        for i in idx:
            if verts[i][2] > top - 0.05:
                new_z[i] = top - depth
            elif verts[i][2] > top - depth:
                return None     # a corner between the old and the new top
    it = iter(zip(verts, new_z))

    def lowered(_m):
        (x, y, _z), z = next(it)
        return f'"{_num(x)} {_num(y)} {_num(z)}"'

    return mesh[:m.start(1)] + re.sub(r'"[^"]*"', lowered, m.group(1)) + mesh[m.end(1):]


def _num(v):
    return f"{v:.6f}".rstrip("0").rstrip(".") or "0"


def _underwater_volume(mesh, ind, origin, vpost):
    """post_processing_volume in the water: a copy of its mesh (tool material, so it is not
    drawn) with the top lowered, since the volume works while the player touches it. None when
    the water is too shallow to put the head under."""
    mesh = _lower_top(mesh, UNDERWATER_DEPTH)
    if mesh is None:
        return None
    m = _MATS_RE.search(mesh)
    copy = mesh[:m.start(1)] + re.sub(r'"[^"]*"', f'"{UNDERWATER_TOOL}"', m.group(1)) + mesh[m.end(1):]
    return element_block(ind, [("classname", "post_processing_volume"), ("targetname", ""),
                               ("StartDisabled", "0"), ("spawnflags", "4097"), ("postprocessing", vpost),
                               ("master", "0"), ("enableexposure", "0"), ("minexposure", "0.25"),
                               ("maxexposure", "8"), ("exposurespeedup", "1"), ("exposurespeeddown", "1"),
                               ("fadetime", UNDERWATER_FADE)],
                         origin=_vfmt(origin), children=[_fresh_copy(copy)])


def _water_step(water_vmats, stats, underwater=None):
    """World meshes that use a water material become func_water entities (and get an underwater
    post processing volume when underwater has a .vpost for their material)."""
    underwater = underwater or {}

    def entity(e):
        # a map ported before keeps its func_water entities (and loses the volumes): the
        # volumes are made again from them, old ones of the program are dropped
        if '"post_processing_volume"' in e and UNDERWATER_MARK in e:
            return []
        if '"classname" "string" "func_water"' not in e or not underwater:
            return None
        span = _children_span(e)
        if span is None:
            return None
        for ch in _split_elements(e, *span):
            if not ch.lstrip().startswith('"CMapMesh"'):
                continue
            m = _MATS_RE.search(ch)
            mats = {x.lower() for x in re.findall(r'"([^"]*)"', m.group(1))} if m else set()
            vpost = next((underwater[w] for w in sorted(mats & water_vmats) if w in underwater), None)
            if vpost:
                ind = re.match(r"[ \t]*", e).group(0)
                vol = _underwater_volume(ch.strip("\n"), ind, _own_origin(e) or (0.0, 0.0, 0.0), vpost)
                if vol is None:
                    return None
                stats["underwater"] = stats.get("underwater", 0) + 1
                return [e, vol]
        return None

    def fn(e):
        if e.lstrip().startswith('"CMapEntity"'):
            return entity(e)
        if not e.lstrip().startswith('"CMapMesh"'):
            return None
        m = _MATS_RE.search(e)
        if not m:
            return None
        mats = {x.lower() for x in re.findall(r'"([^"]*)"', m.group(1))}
        if not mats & water_vmats:
            return None
        origin = _own_origin(e) or (0.0, 0.0, 0.0)
        ind = re.match(r"[ \t]*", e).group(0)
        stats["water"] += 1
        out = [element_block(ind, [("classname", "func_water"), ("targetname", "")],
                             origin=_vfmt(origin), children=[e])]
        vpost = next((underwater[w] for w in sorted(mats & water_vmats) if w in underwater), None)
        vol = _underwater_volume(e, ind, origin, vpost) if vpost else None
        if vol is not None:
            out.append(vol)
            stats["underwater"] = stats.get("underwater", 0) + 1
        return out

    return fn


# ---------------------------------------------------------------------------
# see-through meshes are no vis contributors
# ---------------------------------------------------------------------------

def see_through_meshes(text, sizes, stats):
    """World meshes whose drawn faces can all be seen through (glass, grates, water ...) get
    "Not a Vis Contributor": in Source 1 translucent brushes did not block visibility either.
    Tool faces (nodraw, clip ...) do not count; sizes is the addon's uvfix.TextureSizes."""
    from .vmap import _set_mesh_attr

    def fn(e):
        if not e.lstrip().startswith('"CMapMesh"') or '"visexclude" "bool" "0"' not in e:
            return None
        m = _MATS_RE.search(e)
        if not m:
            return None
        drawn = [x.lower() for x in re.findall(r'"([^"]*)"', m.group(1))
                 if not x.lower().startswith("materials/tools/")]
        if not drawn or not all(sizes.see_through(x) for x in drawn):
            return None
        stats["see_through"] = stats.get("see_through", 0) + 1
        return [_set_mesh_attr(e, "visexclude", "bool", "1")]

    text, _ch = transform_world(text, fn)
    return text


# ---------------------------------------------------------------------------
# ambient_generic -> its soundevent
# ---------------------------------------------------------------------------

def set_ambient_events(text, table, stats):
    """table: {position: soundevent name} from sounds.ambient_events."""
    if not table or '"ambient_generic"' not in text:
        return text

    def fn(p, origin):
        if p.get("classname") != "ambient_generic" or origin not in table:
            return None
        stats["ambients"] += 1
        return {"message": table[origin]}

    text, _n = edit_entities(text, fn)
    return text
