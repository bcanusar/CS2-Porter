"""Brightness of the ported lights.

Source 1 and CS2 lights fall off differently. A Source 1 light gives
    intensity / (c + l * d + q * d^2)
at distance d (vrad works c, l, q out from the falloff keys: with _fifty_percent_distance a light
keeps half of its brightness that far away, so it reaches much further than a CS2 light), a CS2
light gives LUMEN_SCALE * lumens / d^2 (inverse square, up to its range).

For every light the lumens are picked so that the surfaces around it (found with line traces in
the compiled map) get the same light as in Source 1. The falloff values come from the light list
the compiler wrote into the .bsp; for a .vmf they are worked out from the keys.
"""

import math
import struct

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy is part of the build
    np = None

from . import vmf as vmfmod

LUMP_PLANES = 1
LUMP_NODES = 5
LUMP_LEAFS = 10
LUMP_MODELS = 14
LUMP_WORLDLIGHTS = 15
LUMP_LIGHTING_HDR = 53
LUMP_WORLDLIGHTS_HDR = 54
CONTENTS_SOLID = 0x1
EMIT_POINT = 1
EMIT_SPOT = 2

# Source 1 light units: 1 = a surface shows its texture at full brightness. The default Source 1
# light ("255 255 255 200", quadratic falloff: 200 / 255 at 100 units) becomes 1000 lumens, the
# value the CS2 importer gives such a light, so 1 Source 1 brightness = 5 lumens for those.
LUMEN_SCALE = (200.0 / 255.0) * 100.0 ** 2 / 1000.0
# range: where the CS2 light falls under about 1 / 200 (same as vmap.LIGHT_RANGE_PER_LUMEN)
RANGE_PER_LUMEN = 41.0
MIN_LUMENS = 1.0
MAX_LUMENS = 400000.0       # range 41 x sqrt(lumens) stays under vmap.MAX_LIGHT_RANGE

# line traces: surfaces closer than NEAR are lit to full white in both games and are left out
# (unless a light has almost nothing else around it); Source 1 light is counted up to CAP
RAYS = 64
TRACE_DIST = 4096.0
NEAR = 48.0
MIN_HITS = 8
CAP = 4.0
# a map compiled without HDR light is drawn from 8 bit lightmaps that hold at most 2 x full
# brightness (the overbright factor): any brighter Source 1 light looked the same as that
CAP_LDR = 2.0
# distances used when the map geometry is not known (a .vmf input)
DEFAULT_DISTANCES = (128.0, 256.0, 384.0)


def _lin(c):
    return (max(c, 0.0) / 255.0) ** 2.2


def light_color(text):
    """Linear (r, g, b) of a _light value, 0..1 (None when it can not be read)."""
    parts = (text or "").split()
    try:
        vals = [float(v) for v in parts[:3]]
    except ValueError:
        return None
    if len(vals) == 1:
        vals *= 3
    if len(vals) != 3:
        return None
    return tuple(_lin(v) for v in vals)


# --- compiled map: BSP tree, light list ------------------------------------------------

class BspTracer:
    """Line traces against the solid world of a compiled map (BSP tree) and the displacements
    of its decompiled VMF."""

    def __init__(self, info, root=None):
        planes = info.read_lump(LUMP_PLANES)
        self.planes = [struct.unpack_from("<4f", planes, o) for o in range(0, len(planes) - 19, 20)]
        nodes = info.read_lump(LUMP_NODES)
        self.nodes = [struct.unpack_from("<3i", nodes, o) for o in range(0, len(nodes) - 31, 32)]
        leafs = info.read_lump(LUMP_LEAFS)
        size = 56 if info.lumps[LUMP_LEAFS][2] == 0 else 32
        self.solid = [bool(struct.unpack_from("<i", leafs, o)[0] & CONTENTS_SOLID)
                      for o in range(0, len(leafs) - size + 1, size)]
        # the compiler fills the space outside the sealed map as solid leaves without brushes
        self.void = [s and struct.unpack_from("<H", leafs, o + 26)[0] == 0
                     for s, o in zip(self.solid, range(0, len(leafs) - size + 1, size))]
        self.void_boxes = [(struct.unpack_from("<3h", leafs, o + 8), struct.unpack_from("<3h", leafs, o + 14))
                           for v, o in zip(self.void, range(0, len(leafs) - size + 1, size)) if v]
        # inside brushes
        self.brush_boxes = [(struct.unpack_from("<3h", leafs, o + 8), struct.unpack_from("<3h", leafs, o + 14))
                            for s, v, o in zip(self.solid, self.void, range(0, len(leafs) - size + 1, size))
                            if s and not v]
        # the open space inside the sealed map (where players can be)
        self.air_boxes = [(struct.unpack_from("<3h", leafs, o + 8), struct.unpack_from("<3h", leafs, o + 14))
                          for s, o in zip(self.solid, range(0, len(leafs) - size + 1, size)) if not s]
        models = info.read_lump(LUMP_MODELS)
        self.head = struct.unpack_from("<i", models, 36)[0] if len(models) >= 48 else 0
        # the tree only describes the space inside the world's bounds
        self.world = ((struct.unpack_from("<3f", models, 0), struct.unpack_from("<3f", models, 12))
                      if len(models) >= 48 else None)
        self.disp = _disp_arrays(root) if root is not None else None

    def _hit(self, node, f0, f1, p0, p1):
        planes, nodes = self.planes, self.nodes
        while node >= 0:
            pn, c0, c1 = nodes[node]
            nx, ny, nz, dist = planes[pn]
            d0 = nx * p0[0] + ny * p0[1] + nz * p0[2] - dist
            d1 = nx * p1[0] + ny * p1[1] + nz * p1[2] - dist
            if d0 >= 0 and d1 >= 0:
                node = c0
                continue
            if d0 < 0 and d1 < 0:
                node = c1
                continue
            frac = d0 / (d0 - d1)
            fm = f0 + (f1 - f0) * frac
            pm = (p0[0] + (p1[0] - p0[0]) * frac, p0[1] + (p1[1] - p0[1]) * frac,
                  p0[2] + (p1[2] - p0[2]) * frac)
            near, far = (c0, c1) if d0 >= 0 else (c1, c0)
            hit = self._hit(near, f0, fm, p0, pm)
            if hit is not None:
                return hit
            node, f0, p0 = far, fm, pm
        leaf = -node - 1
        if 0 <= leaf < len(self.solid) and self.solid[leaf]:
            return f0
        return None

    def trace(self, start, direction, length=TRACE_DIST):
        """Distance to the first solid surface along the line, None when nothing is hit."""
        end = tuple(start[k] + direction[k] * length for k in range(3))
        try:
            frac = self._hit(self.head, 0.0, 1.0, tuple(start), end)
        except RecursionError:
            frac = None
        best = frac * length if frac is not None else None
        return best

    def box_in(self, lo, hi, void_only=True):
        """True when the box only touches the space outside the map (void_only) or only solid
        space (outside the map or inside brushes)."""
        planes, nodes = self.planes, self.nodes
        ok = self.void if void_only else self.solid
        c = [(lo[k] + hi[k]) * 0.5 for k in range(3)]
        h = [(hi[k] - lo[k]) * 0.5 for k in range(3)]
        stack = [self.head]
        while stack:
            node = stack.pop()
            if node < 0:
                leaf = -node - 1
                if not (0 <= leaf < len(ok) and ok[leaf]):
                    return False
                continue
            pn, c0, c1 = nodes[node]
            nx, ny, nz, dist = planes[pn]
            d = nx * c[0] + ny * c[1] + nz * c[2] - dist
            r = abs(nx) * h[0] + abs(ny) * h[1] + abs(nz) * h[2]
            if d >= r:
                stack.append(c0)
            elif d <= -r:
                stack.append(c1)
            else:
                stack.append(c0)
                stack.append(c1)
        return True

    def trace_all(self, start, directions, length=TRACE_DIST):
        """trace() for every direction, displacements included."""
        out = [self.trace(start, d, length) for d in directions]
        if self.disp is not None and np is not None:
            disp = _disp_hits(self.disp, start, directions, length)
            if disp is not None:
                out = [h if (dh is None or (h is not None and h <= dh)) else dh for h, dh in zip(out, disp)]
        return out


def _disp_arrays(root):
    """Triangles of every displacement (world and func_detail) as numpy arrays."""
    if np is None:
        return None
    from .blend import displacement_triangles
    tris = []
    holders = [root.child("world")] + [e for e in root.children("entity") if e.classname == "func_detail"]
    for holder in holders:
        if holder is None:
            continue
        for solid in holder.children("solid"):
            if any(s.child("dispinfo") is not None for s in solid.children("side")):
                tris += displacement_triangles(solid)
    if not tris:
        return None
    a = np.array(tris, dtype=np.float64)          # (n, 3, 3)
    return {"v0": a[:, 0], "e1": a[:, 1] - a[:, 0], "e2": a[:, 2] - a[:, 0],
            "lo": a.min(axis=1), "hi": a.max(axis=1)}


def _disp_hits(disp, start, directions, length):
    o = np.array(start, dtype=np.float64)
    near = np.all((disp["hi"] >= o - length) & (disp["lo"] <= o + length), axis=1)
    if not near.any():
        return None
    v0, e1, e2 = disp["v0"][near], disp["e1"][near], disp["e2"][near]
    s = o - v0
    out = []
    for d in directions:
        dv = np.array(d, dtype=np.float64)
        p = np.cross(dv, e2)
        det = np.einsum("ij,ij->i", e1, p)
        ok = np.abs(det) > 1e-9
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        u = np.einsum("ij,ij->i", s, p) * inv
        q = np.cross(s, e1)
        v = (q @ dv) * inv
        tt = np.einsum("ij,ij->i", e2, q) * inv
        hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (tt > 0.5) & (tt < length)
        out.append(float(tt[hit].min()) if hit.any() else None)
    return out


LUMP_LIGHTING = 8


def _lightmap_lit(data):
    """True when a lightmap lump (ColorRGBExp32 luxels) has a luxel brighter than black."""
    n = len(data) - len(data) % 4
    if np is not None:
        return bool(np.frombuffer(data, dtype=np.uint8, count=n).reshape(-1, 4)[:, :3].any())
    return any(data[i] or data[i + 1] or data[i + 2] for i in range(0, n, 4))


def fullbright_map(info):
    """True when the map has no baked light at all (no lightmaps, or only black ones): such a
    map was played fully bright in Source 1, every lit world surface showing its own texture."""
    try:
        for idx in (LUMP_LIGHTING, LUMP_LIGHTING_HDR):
            if info.lumps[idx][1] > 0 and _lightmap_lit(info.read_lump(idx)):
                return False
    except Exception:  # noqa: BLE001
        return False
    return True


def world_lights(info):
    """{position: light} of the light list in the .bsp (the HDR one when the map has HDR
    lighting). light: {"type", "intensity" (r, g, b), "c", "l", "q", "radius", "normal",
    "stopdot2"}."""
    idx = LUMP_WORLDLIGHTS
    if info.lumps[LUMP_LIGHTING_HDR][1] > 0 and info.lumps[LUMP_WORLDLIGHTS_HDR][1] > 0:
        idx = LUMP_WORLDLIGHTS_HDR
    data = info.read_lump(idx)
    if not data:
        return {}
    size = 100 if info.lumps[idx][2] >= 1 else 88
    if len(data) % size:
        size = 88 if len(data) % 88 == 0 else 100
        if len(data) % size:
            return {}
    off = 12 if size == 100 else 0             # version 1 has a shadow offset after the normal
    out = {}
    for o in range(0, len(data), size):
        org = struct.unpack_from("<3f", data, o)
        inten = struct.unpack_from("<3f", data, o + 12)
        normal = struct.unpack_from("<3f", data, o + 24)
        _cluster, typ, _style = struct.unpack_from("<3i", data, o + 36 + off)
        _stopdot, stopdot2, _exp, radius, c, l, q = struct.unpack_from("<7f", data, o + 48 + off)
        if typ not in (EMIT_POINT, EMIT_SPOT):
            continue
        out.setdefault(vmfmod.pos_key(org), {"type": typ, "intensity": inten, "c": c, "l": l, "q": q,
                                             "radius": radius, "normal": normal, "stopdot2": stopdot2})
    return out


# --- .vmf keys -> falloff (what vrad does) -----------------------------------------------

def _solve(x1, y1, x2, y2, x3, y3):
    det = (x1 - x2) * (x1 - x3) * (x2 - x3)
    if det == 0:
        return None
    a = (x3 * (-y1 + y2) + x2 * (y1 - y3) + x1 * (-y2 + y3)) / det
    b = (x3 * x3 * (y1 - y2) + x1 * x1 * (y2 - y3) + x2 * x2 * (-y1 + y3)) / det
    c = (x1 * x3 * (-x1 + x3) * y2 + x2 * x2 * (x3 * y1 - x1 * y3) + x2 * (-(x3 * x3 * y1) + x1 * x1 * y3)) / det
    return a, b, c


def entity_light(ent):
    """The light of a Source 1 light entity worked out like vrad does (for a .vmf input or a light
    the compiler did not keep). Same form as world_lights()."""
    parts = (ent.get("_light") or "").split()
    try:
        vals = [float(v) for v in parts]
    except ValueError:
        return None
    if not vals:
        return None
    rgb = vals[:3] if len(vals) >= 3 else vals[:1] * 3
    scale = vals[3] / 255.0 if len(vals) >= 4 else 1.0
    inten = tuple(_lin(v) * scale for v in rgb)
    num = lambda k, d=0.0: vmfmod._float(ent.get(k), d)  # noqa: E731
    d50 = num("_fifty_percent_distance")
    radius = 0.0
    if d50 > 0:
        d0 = num("_zero_percent_distance")
        if d0 < d50:
            d0 = 2.0 * d50
        # 1 at the light, 1/2 at d50, 1/256 at d0; vrad moves the middle point until the curve
        # only falls (the light gets brighter near its center, half brightness stays at d50)
        y2 = 2.0
        abc = _solve(0.0, 1.0, d50, y2, d0, 256.0)
        while abc and abc[0] > 1e-12 and 0 < -abc[1] / (2 * abc[0]) < d0 and y2 < 256:
            y2 *= 1.05
            abc = _solve(0.0, 1.0, d50, y2, d0, 256.0)
        if not abc:
            return None
        a, b, c = abc
        v50 = c + d50 * (b + d50 * a)
        k = 2.0 / v50 if v50 > 0 else 1.0
        c, l, q = c * k, b * k, a * k
    else:
        c, l, q = num("_constant_attn"), num("_linear_attn"), num("_quadratic_attn")
        radius = num("_distance")
        c, l, q = max(c, 0.0), max(l, 0.0), max(q, 0.0)
        if c < 1e-4 and l < 1e-4 and q < 1e-4:
            c = 1.0
        # intensity is given for 100 units away
        inten = tuple(v * (c + 100 * l + 100 * 100 * q) for v in inten)
    typ = EMIT_SPOT if ent.classname == "light_spot" else EMIT_POINT
    cone = vmfmod._float(ent.get("_cone"), 45.0)
    return {"type": typ, "intensity": inten, "c": c, "l": l, "q": q, "radius": radius,
            "normal": vmfmod._forward(_angles(ent)), "stopdot2": math.cos(math.radians(cone))}


def _angles(ent):
    ang = vmfmod._vec(ent.get("angles")) or (0.0, 0.0, 0.0)
    pitch = vmfmod._float(ent.get("pitch"))
    if pitch is not None:
        ang = (-pitch, ang[1], ang[2])
    return ang


# --- lumens ----------------------------------------------------------------------------------

def _sphere(n):
    """n directions spread evenly over the sphere."""
    out = []
    golden = math.pi * (3.0 - math.sqrt(5.0))
    for i in range(n):
        z = 1.0 - 2.0 * (i + 0.5) / n
        r = math.sqrt(max(0.0, 1.0 - z * z))
        a = golden * i
        out.append((r * math.cos(a), r * math.sin(a), z))
    return out


_DIRS = _sphere(RAYS)
_DIRS_DENSE = _sphere(RAYS * 8)


def _directions(light):
    if light["type"] != EMIT_SPOT:
        return _DIRS
    n = light["normal"]
    cos_out = light["stopdot2"]
    dirs = [d for d in _DIRS_DENSE if d[0] * n[0] + d[1] * n[1] + d[2] * n[2] >= cos_out]
    return dirs or [tuple(n)]


def source1_light(light, color, d):
    """Brightness of the light d units away in Source 1 units, for a white color."""
    if light["radius"] and d > light["radius"]:
        return 0.0
    peak = max(color) if color else 1.0
    if light.get("linear"):
        # dynamic light (convert_dynamic_lights): falls off linearly to 0 at its radius
        return max(light["intensity"]) * max(0.0, 1.0 - d / light["radius"]) / max(peak, 1e-3)
    att = light["c"] + light["l"] * d + light["q"] * d * d
    if att <= 0:
        return 0.0
    return max(light["intensity"]) / att / max(peak, 1e-3)


def match_lumens(light, color, distances, cap=CAP):
    """(lumens, range) of a CS2 light that gives the surfaces at the distances the same light as
    the Source 1 light (counted up to cap)."""
    hits = [d for d in distances if d is not None]
    use = [d for d in hits if d >= NEAR]
    if len(use) < MIN_HITS:
        use = [max(d, 16.0) for d in hits] or list(DEFAULT_DISTANCES)
    logs = []
    for d in use:
        s = min(source1_light(light, color, d), cap)
        if s > 0:
            logs.append(math.log(s * d * d))
    if not logs:
        return None
    lumens = math.exp(sum(logs) / len(logs)) / LUMEN_SCALE
    lumens = min(max(lumens, MIN_LUMENS), MAX_LUMENS)
    rng = RANGE_PER_LUMEN * math.sqrt(lumens)
    if light["radius"]:
        rng = min(rng, light["radius"])
    return lumens, rng


def light_values(root, info=None, tracer=None):
    """{position: {"lumens", "range"}} for the point and spot lights of the VMF.
    info: the .bsp (BSPInfo) when the map is ported from one."""
    compiled = world_lights(info) if info is not None else {}
    cap = CAP_LDR if info is not None and info.lumps[LUMP_LIGHTING_HDR][1] <= 0 else CAP
    out = {}
    for ent in root.children("entity"):
        if ent.classname not in ("light", "light_spot", "light_dynamic"):
            continue
        pos = vmfmod._vec(ent.get("origin"))
        if pos is None:
            continue
        key = vmfmod.pos_key(pos)
        light = compiled.get(key) or entity_light(ent)
        color = light_color(ent.get("_light"))
        if light is None or not color or max(color) <= 0:
            continue
        if tracer is not None:
            dists = tracer.trace_all(pos, _directions(light))
        else:
            dists = list(DEFAULT_DISTANCES)
        res = match_lumens(light, color, dists, cap)
        if res is None:
            continue
        lumens, rng = res
        # (no cone factor for spot lights: CS2 gives a light_barn the same intensity as a
        # light_omni2 of the same lumens, checked in a compiled map)
        out[key] = {"lumens": lumens, "range": rng}
    return out


# --- light_dynamic -----------------------------------------------------------------------------

DLIGHT_DISTANCE = 120.0         # light_dynamic defaults (Source 1 FGD)
DLIGHT_SPOT_RADIUS = 80.0
DLIGHT_CONE = 45.0
DLIGHT_NO_WORLD = 1             # spawnflag: does not light the world
DLIGHT_MIN_OFFSET = 4.0
_DLIGHT_KEYS = ("brightness", "distance", "spotlight_radius", "_inner_cone", "_cone", "pitch", "spawnflags")


def _first_hit(tracer, start, direction, length):
    d = tracer.trace(start, direction, length)
    return None if d is None or d >= length - 1e-3 else d


def convert_dynamic_lights(root, tracer=None):
    """CS2 has no light_dynamic (the importer keeps the class, Hammer shows it as obsolete). It
    becomes a static light where it lights the world in Source 1:
    with a cone (_cone > 0) only its spotlight end lights the world: a light of radius
    spotlight_radius where a line from the entity along its direction hits a surface within
    distance, its color times (1 - hit fraction)^2. Without a cone it lights the world from its
    own place up to distance. The light is color / 255 x 2^brightness at its place and falls off
    linearly to its radius (an approximation of the Source 1 dynamic light on lightmaps).
    Lights that only light models are removed.
    Returns ({pos_key: {"lumens", "range"}} of the new lights, removed count)."""
    dyn = [e for e in root.children("entity") if e.classname == "light_dynamic"]
    if not dyn:
        return {}, 0
    if tracer is None:
        tracer = vmfmod.BrushTracer(root)
    values, drop = {}, []
    for ent in dyn:
        num = lambda k, d: vmfmod._float(ent.get(k), d)  # noqa: E731
        pos = vmfmod._vec(ent.get("origin"))
        parts = (ent.get("_light") or "").split()
        try:
            rgb = [float(v) for v in parts[:3]]
        except ValueError:
            rgb = []
        if len(rgb) == 1:
            rgb *= 3
        color = light_color(ent.get("_light"))
        if (pos is None or not all(math.isfinite(v) for v in pos) or len(rgb) != 3 or not color
                or max(color) <= 0 or int(num("spawnflags", 0.0)) & DLIGHT_NO_WORLD):
            drop.append(ent)
            continue
        radius = num("distance", DLIGHT_DISTANCE)
        peak = max(rgb) / 255.0 * 2.0 ** min(max(num("brightness", 0.0), -8.0), 8.0)
        fwd = vmfmod._forward(_angles(ent))
        hit, back, dist = pos, None, 0.0
        if num("_cone", DLIGHT_CONE) > 0:
            dist = _first_hit(tracer, pos, fwd, radius) if radius > 0 else None
            if dist is None:
                drop.append(ent)
                continue
            peak *= (1.0 - dist / radius) ** 2
            radius = num("spotlight_radius", DLIGHT_SPOT_RADIUS)
            hit = tuple(pos[k] + fwd[k] * dist for k in range(3))
            back = tuple(-v for v in fwd)
        if radius <= 0 or peak <= 0:
            drop.append(ent)
            continue
        light = {"type": EMIT_POINT, "intensity": (peak,) * 3, "c": 1.0, "l": 0.0, "q": 0.0,
                 "radius": radius, "linear": True}
        start = hit if back is None else tuple(hit[k] + back[k] for k in range(3))
        if hasattr(tracer, "trace_all"):
            dists = tracer.trace_all(start, _DIRS)
        else:
            dists = [_first_hit(tracer, start, d, TRACE_DIST) for d in _DIRS]
        res = match_lumens(light, color, dists)
        if res is None:
            drop.append(ent)
            continue
        lumens, rng = res
        new = pos
        if back is not None:
            # as far off the surface as the CS2 light has to be to give its middle the Source 1
            # brightness (CS2: lumens x LUMEN_SCALE / d^2), between the entity and the surface
            off = math.sqrt(lumens * LUMEN_SCALE * max(color) / peak)
            off = min(max(off, DLIGHT_MIN_OFFSET), radius / 2.0, max(dist, DLIGHT_MIN_OFFSET))
            new = tuple(hit[k] + back[k] * off for k in range(3))
        for k in _DLIGHT_KEYS:
            ent.remove_key(k)
        ent.set("classname", "light")
        ent.set("origin", " ".join(vmfmod._fmt(v) for v in new))
        # 4th value: the importer makes 5 lumens of 1
        ent.set("_light", " ".join(vmfmod._fmt(v) for v in rgb) + " " + vmfmod._fmt(lumens / 5.0))
        ent.set("_distance", vmfmod._fmt(rng))
        values[vmfmod.pos_key(new)] = {"lumens": lumens, "range": rng}
    if drop:
        ids = {id(e) for e in drop}
        root.items = [i for i in root.items if id(i) not in ids]
    return values, len(drop)
