"""Texture lights: materials that light the map in Source 1 (a lights.rad entry when the map was
compiled), which CS2 gets as self illum materials whose light is baked.

The compiler marks the faces of such a material (texinfo flag LIGHT) and puts surface lights on
them into the light list of the .bsp. Their intensity per unit of face area is the same all over
a material: rho (r, g, b). A surface of them lights what faces it like
    lightmap = sum(intensity * cos(out) * cos(in) / d^2)   ->   pi * rho in front of a big one.
A CS2 self illum surface of brightness e (linear, 1 = the texture at full brightness, the same
units as a lightmap) lights what faces it with e in front of a big one, so e = pi * rho.
"""

import collections
import math
import re
import struct

LUMP_PLANES = 1
LUMP_TEXDATA = 2
LUMP_VERTEXES = 3
LUMP_TEXINFO = 6
LUMP_FACES = 7
LUMP_EDGES = 12
LUMP_SURFEDGES = 13
LUMP_WORLDLIGHTS = 15
LUMP_TEXDATA_STRING_DATA = 43
LUMP_TEXDATA_STRING_TABLE = 44
LUMP_LIGHTING_HDR = 53
LUMP_WORLDLIGHTS_HDR = 54
SURF_LIGHT = 0x1
EMIT_SURFACE = 0
GRID = 512.0
PLANE_EPS = 1.5
EDGE_EPS = 1.0
MIN_LIGHT = 0.05            # brightness below this is no light worth keeping


def _names(info):
    data, table = info.read_lump(LUMP_TEXDATA_STRING_DATA), info.read_lump(LUMP_TEXDATA_STRING_TABLE)
    out = []
    for o in range(0, len(table) - 3, 4):
        off = struct.unpack_from("<i", table, o)[0]
        end = data.find(b"\0", off)
        out.append(data[off:end if end >= 0 else len(data)].decode("latin-1").lower().replace("\\", "/"))
    return out


def _faces(info):
    """[(material, area, points, normal)] of the faces with the LIGHT flag."""
    names = _names(info)
    texdata, texinfo = info.read_lump(LUMP_TEXDATA), info.read_lump(LUMP_TEXINFO)
    tinfo = []
    for o in range(0, len(texinfo) - 71, 72):
        flags, td = struct.unpack_from("<ii", texinfo, o + 64)
        name = ""
        if 0 <= td * 32 + 16 <= len(texdata):
            idx = struct.unpack_from("<i", texdata, td * 32 + 12)[0]
            name = names[idx] if 0 <= idx < len(names) else ""
        tinfo.append((flags, name))
    if not any(f & SURF_LIGHT for f, _n in tinfo):
        return []
    vdata, edata, sdata = info.read_lump(LUMP_VERTEXES), info.read_lump(LUMP_EDGES), info.read_lump(LUMP_SURFEDGES)
    pdata, fdata = info.read_lump(LUMP_PLANES), info.read_lump(LUMP_FACES)
    out = []
    for o in range(0, len(fdata) - 55, 56):
        pn, side, _on, first, num, ti = struct.unpack_from("<HBBihh", fdata, o)
        if not 0 <= ti < len(tinfo) or not tinfo[ti][0] & SURF_LIGHT or not tinfo[ti][1]:
            continue
        area = struct.unpack_from("<f", fdata, o + 24)[0]
        pts = []
        for k in range(num):
            se = struct.unpack_from("<i", sdata, (first + k) * 4)[0]
            a, b = struct.unpack_from("<2H", edata, abs(se) * 4)
            pts.append(struct.unpack_from("<3f", vdata, (a if se >= 0 else b) * 12))
        n = struct.unpack_from("<3f", pdata, pn * 20)
        if side:
            n = (-n[0], -n[1], -n[2])
        if len(pts) >= 3 and area > 0:
            out.append((tinfo[ti][1], area, pts, n))
    return out


def _inside(p, pts, n):
    """p (on the plane) inside the convex polygon pts (up to EDGE_EPS). Either winding: the
    compiler winds water surfaces the other way round."""
    inner = outer = True
    for i, a in enumerate(pts):
        b = pts[(i + 1) % len(pts)]
        e = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        # edge normal n x e (inward for one winding, outward for the other)
        m = (n[1] * e[2] - n[2] * e[1], n[2] * e[0] - n[0] * e[2], n[0] * e[1] - n[1] * e[0])
        ln = math.sqrt(m[0] * m[0] + m[1] * m[1] + m[2] * m[2])
        if ln < 1e-6:
            continue
        d = ((p[0] - a[0]) * m[0] + (p[1] - a[1]) * m[1] + (p[2] - a[2]) * m[2]) / ln
        inner = inner and d >= -EDGE_EPS
        outer = outer and d <= EDGE_EPS
        if not (inner or outer):
            return False
    return True


def _surface_lights(info):
    idx = LUMP_WORLDLIGHTS
    if info.lumps[LUMP_LIGHTING_HDR][1] > 0 and info.lumps[LUMP_WORLDLIGHTS_HDR][1] > 0:
        idx = LUMP_WORLDLIGHTS_HDR
    data = info.read_lump(idx)
    if not data:
        return []
    size = 100 if info.lumps[idx][2] >= 1 else 88
    if len(data) % size:
        size = 88 if len(data) % 88 == 0 else 100
        if len(data) % size:
            return []
    off = 12 if size == 100 else 0
    out = []
    for o in range(0, len(data), size):
        if struct.unpack_from("<i", data, o + 40 + off)[0] == EMIT_SURFACE:
            out.append((struct.unpack_from("<3f", data, o), struct.unpack_from("<3f", data, o + 12)))
    return out


def texture_lights(info):
    """{material: (r, g, b)} self illum brightness (linear, see the module text) of the texture
    lights of the .bsp. Empty when it has none."""
    faces = _faces(info)
    if not faces:
        return {}
    grid = collections.defaultdict(list)
    for i, (_m, _a, pts, _n) in enumerate(faces):
        lo = [int(math.floor((min(p[k] for p in pts) - PLANE_EPS) / GRID)) for k in range(3)]
        hi = [int(math.floor((max(p[k] for p in pts) + PLANE_EPS) / GRID)) for k in range(3)]
        for x in range(lo[0], hi[0] + 1):
            for y in range(lo[1], hi[1] + 1):
                for z in range(lo[2], hi[2] + 1):
                    grid[(x, y, z)].append(i)
    power = collections.defaultdict(lambda: [0.0, 0.0, 0.0])
    lit = set()
    for org, inten in _surface_lights(info):
        cell = tuple(int(math.floor(org[k] / GRID)) for k in range(3))
        for i in grid.get(cell, ()):
            mat, _a, pts, n = faces[i]
            if abs(sum((org[k] - pts[0][k]) * n[k] for k in range(3))) > PLANE_EPS or not _inside(org, pts, n):
                continue
            acc = power[mat]
            for k in range(3):
                acc[k] += inten[k]
            lit.add(i)
            break
    area = collections.defaultdict(float)
    for i in lit:
        area[faces[i][0]] += faces[i][1]
    out = {}
    for mat, acc in power.items():
        if area[mat] <= 0:
            continue
        e = tuple(math.pi * v / area[mat] for v in acc)
        if max(e) >= MIN_LIGHT:
            out.setdefault(_unpatched(mat), e)
    return out


def _unpatched(mat):
    """The material a compiler made copy (maps/<map>/name_x_y_z, name_wvt_patch) is made from."""
    m = re.match(r"maps/[^/]+/(.+)$", mat)
    if not m:
        return mat
    name = re.sub(r"(_wvt_patch|_-?\d+_-?\d+_-?\d+)$", "", m.group(1))
    return name


# CS2 lights: a point light of L lumens lights a surface d away like a Source 1 light of
# L * LUMEN_SCALE / d^2 (lighting.py), so 1 lumen per square unit is lightmap brightness
# 4 pi LUMEN_SCALE. A big surface glowing with e gives e in front of it: e / that per unit area.
LIGHTMAP_PER_LUX = 4.0 * math.pi * 7.843
RECT_TILE = 2048.0          # surfaces are covered with squares about this big at most
RECT_MAX = 256              # lights for all surfaces together
RECT_MIN_GLOW = 0.02        # a light reaches as far as it lights brighter than this
_FACING = {(2, 1): "-90 0 0", (2, -1): "90 0 0", (0, 1): "0 0 0", (0, -1): "0 180 0",
           (1, 1): "0 90 0", (1, -1): "0 270 0"}


def rect_lights(rects, glow):
    """Rectangle lights in front of glowing surfaces whose material can not glow in CS2.
    rects: vmf.surface_rects(); glow: {material: (r, g, b)} (texture_lights).
    Returns [{origin, angles, size, color, lumens, range}]."""
    out = []
    for mat, lo, hi, axis, sign in rects:
        e = glow.get(mat)
        if not e or max(e) < MIN_LIGHT:
            continue
        peak = max(e)
        color = " ".join(str(int(round(255 * max(v, 0.0) / peak))) for v in e)
        u, v = [k for k in range(3) if k != axis]
        width, height = hi[u] - lo[u], hi[v] - lo[v]
        if width < 1 or height < 1:
            continue
        side = min(width, height, RECT_TILE)
        nu, nv = max(1, round(width / side)), max(1, round(height / side))
        w, h = width / nu, height / nv
        lumens = peak * w * h / LIGHTMAP_PER_LUX
        # on its axis a small lambertian light of L lumens gives L / (pi d^2) per unit area
        reach = math.sqrt(LIGHTMAP_PER_LUX * lumens / (math.pi * RECT_MIN_GLOW))
        rng = max(256.0, min(8192.0, round(reach / 50.0) * 50.0))
        for i in range(nu):
            for j in range(nv):
                c = list(lo)
                c[u] = lo[u] + (i + 0.5) * w
                c[v] = lo[v] + (j + 0.5) * h
                c[axis] = lo[axis] + 2.0 * sign
                out.append({"origin": c, "angles": _FACING[(axis, sign)], "size": math.sqrt(w * h),
                            "color": color, "lumens": lumens, "range": rng})
                if len(out) >= RECT_MAX:
                    return out
    return out


def self_illum(e):
    """(tint (r, g, b) with its largest part 1, Self Illum Brightness) that make a surface glow
    with brightness e: CS2 multiplies the tint by 2 to the power of the brightness."""
    peak = max(e)
    if peak <= 0:
        return (1.0, 1.0, 1.0), 0.0
    return tuple(v / peak for v in e), max(-10.0, min(10.0, math.log2(peak)))
