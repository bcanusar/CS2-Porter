"""Blend (two layer) displacements.

The map importer keeps displacements as subdivided faces, but drops their vertex alphas, so
WorldVertexTransition materials only show their first layer. The alphas are read from the VMF
here and written into the .vmap as a VertexPaintBlendParams stream (red = second layer), the
same way Hammer stores blend paint on subdivided faces.

S2 face <-> VMF displacement: matched by material and by the displaced corner positions.
Subdivision vertex <-> displacement grid: through the texture coordinates, which are an affine
function of the position on the flat (undisplaced) face in both engines.
"""

import math
import re
import uuid

from .materials import out_name
from .vmf import normalize_material

BLEND_SHADERS = ("worldvertextransition",)
_EPS = 0.01


# ---------------------------------------------------------------------------
# VMF side
# ---------------------------------------------------------------------------

def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _mul(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a):
    ln = math.sqrt(_dot(a, a))
    return _mul(a, 1.0 / ln) if ln > 1e-9 else (0.0, 0.0, 0.0)


def _lerp(a, b, f):
    return _add(a, _mul(_sub(b, a), f))


_PT_RE = re.compile(r"\(([^)]*)\)")


def _plane_points(text):
    pts = []
    for m in _PT_RE.finditer(text or ""):
        try:
            pts.append(tuple(float(v) for v in m.group(1).split()))
        except ValueError:
            return None
    return pts if len(pts) == 3 and all(len(p) == 3 for p in pts) else None


def _polygon(planes, idx):
    """Face polygon of plane idx of a convex brush: a big square on the plane clipped by the
    other planes. planes: [(normal, dist)] with outward normals."""
    n, d = planes[idx]
    ref = (0.0, 0.0, 1.0) if abs(n[2]) < 0.9 else (1.0, 0.0, 0.0)
    u = _norm(_cross(n, ref))
    v = _cross(n, u)
    c = _mul(n, d)
    big = 131072.0
    poly = [_add(c, _add(_mul(u, su * big), _mul(v, sv * big)))
            for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    for k, (pn, pd) in enumerate(planes):
        if k == idx or not poly:
            continue
        out = []
        for i, a in enumerate(poly):
            b = poly[(i + 1) % len(poly)]
            da, db = _dot(pn, a) - pd, _dot(pn, b) - pd
            if da <= _EPS:
                out.append(a)
            if (da < -_EPS and db > _EPS) or (da > _EPS and db < -_EPS):
                out.append(_lerp(a, b, da / (da - db)))
        poly = out
    clean = []
    for p in poly:
        if not clean or max(abs(x - y) for x, y in zip(p, clean[-1])) > 0.05:
            clean.append(p)
    if len(clean) > 1 and max(abs(x - y) for x, y in zip(clean[0], clean[-1])) <= 0.05:
        clean.pop()
    return clean


def _rows(node, name, width):
    blk = node.child(name)
    if blk is None:
        return None
    out = []
    for i in range(10 ** 6):
        row = blk.get(f"row{i}")
        if row is None:
            break
        try:
            vals = [float(v) for v in row.split()]
        except ValueError:
            return None
        if len(vals) != width:
            return None
        out.append(vals)
    return out


class Disp:
    __slots__ = ("material", "n", "alphas", "corners", "centroid")

    def __init__(self, material, n, alphas, corners):
        self.material = material      # normalized material name
        self.n = n                    # segments per side
        self.alphas = alphas          # [(n+1) rows][(n+1)] 0..255, row = first index
        self.corners = corners        # [hypothesis] -> {(i, j): displaced corner position}
        c = list(corners[0].values())
        self.centroid = _mul((sum(p[0] for p in c), sum(p[1] for p in c), sum(p[2] for p in c)), 0.25)

    def alpha(self, gi, gj):
        n = self.n
        gi = min(max(gi, 0.0), n)
        gj = min(max(gj, 0.0), n)
        i0, j0 = min(int(gi), n - 1), min(int(gj), n - 1)
        fi, fj = gi - i0, gj - j0
        a = self.alphas
        top = a[i0][j0] * (1 - fj) + a[i0][j0 + 1] * fj
        bot = a[i0 + 1][j0] * (1 - fj) + a[i0 + 1][j0 + 1] * fj
        return top * (1 - fi) + bot * fi


def _solids(root):
    world = root.child("world")
    if world is not None:
        yield from world.children("solid")
        for h in world.children("hidden"):
            yield from h.children("solid")
    for ent in root.children("entity"):
        yield from ent.children("solid")
        for h in ent.children("hidden"):
            yield from h.children("solid")


def _outward_planes(sides):
    pts = [_plane_points(s.get("plane")) for s in sides]
    if any(p is None for p in pts):
        return None
    planes = []
    for p0, p1, p2 in pts:
        nrm = _norm(_cross(_sub(p2, p0), _sub(p1, p0)))
        planes.append([nrm, _dot(nrm, p0)])
    # outward normals: the middle of the plane points is inside the brush
    allp = [p for tri in pts for p in tri]
    mid = _mul((sum(p[0] for p in allp), sum(p[1] for p in allp), sum(p[2] for p in allp)), 1.0 / len(allp))
    for pl in planes:
        if _dot(pl[0], mid) - pl[1] > 0:
            pl[0] = _mul(pl[0], -1.0)
            pl[1] = -pl[1]
    return planes


def displacement_triangles(solid):
    """Triangles of the displaced surfaces of a brush (Source 1 positions)."""
    sides = solid.children("side")
    planes = _outward_planes(sides)
    if planes is None:
        return []
    tris = []
    for k, side in enumerate(sides):
        disp = side.child("dispinfo")
        if disp is None:
            continue
        try:
            n = 1 << int(disp.get("power", "3"))
            start = tuple(float(v) for v in disp.get("startposition", "").strip("[] ").split())
            elevation = float(disp.get("elevation", "0") or 0)
        except ValueError:
            continue
        w = n + 1
        normals = _rows(disp, "normals", 3 * w)
        dists = _rows(disp, "distances", w)
        offsets = _rows(disp, "offsets", 3 * w) or [[0.0] * (3 * w)] * w
        poly = _polygon(planes, k)
        if not normals or not dists or len(normals) != w or len(dists) != w or len(offsets) != w \
                or len(poly) != 4 or len(start) != 3:
            continue
        s = min(range(4), key=lambda i: _dot(_sub(poly[i], start), _sub(poly[i], start)))
        c = [poly[(s - q) % 4] for q in range(4)]        # same grid direction as _make_disp
        lift = _mul(planes[k][0], elevation)
        grid = {}
        for i in range(w):
            for j in range(w):
                nx, ny, nz = normals[i][3 * j:3 * j + 3]
                ox, oy, oz = offsets[i][3 * j:3 * j + 3]
                dd = dists[i][j]
                base = _lerp(_lerp(c[0], c[1], i / n), _lerp(c[3], c[2], i / n), j / n)
                grid[i, j] = _add(_add(base, (nx * dd + ox, ny * dd + oy, nz * dd + oz)), lift)
        for i in range(n):
            for j in range(n):
                a, b, cc, d = grid[i, j], grid[i + 1, j], grid[i + 1, j + 1], grid[i, j + 1]
                tris += [(a, b, cc), (a, cc, d)]
    return tris


def collect_disps(root):
    """Every displacement with non zero alphas: {material: [Disp]}."""
    out = {}
    for solid in _solids(root):
        sides = solid.children("side")
        if not any(s.child("dispinfo") is not None for s in sides):
            continue
        pts = [_plane_points(s.get("plane")) for s in sides]
        if any(p is None for p in pts):
            continue
        planes = []
        for p0, p1, p2 in pts:
            nrm = _norm(_cross(_sub(p2, p0), _sub(p1, p0)))
            planes.append([nrm, _dot(nrm, p0)])
        # outward normals: the middle of the plane points is inside the brush
        allp = [p for tri in pts for p in tri]
        mid = _mul((sum(p[0] for p in allp), sum(p[1] for p in allp), sum(p[2] for p in allp)), 1.0 / len(allp))
        for pl in planes:
            if _dot(pl[0], mid) - pl[1] > 0:
                pl[0] = _mul(pl[0], -1.0)
                pl[1] = -pl[1]
        for k, side in enumerate(sides):
            disp = side.child("dispinfo")
            if disp is None:
                continue
            d = _make_disp(side, disp, planes, k)
            if d is not None:
                out.setdefault(d.material, []).append(d)
    return out


def _make_disp(side, disp, planes, k):
    try:
        power = int(disp.get("power", "3"))
        start = tuple(float(v) for v in disp.get("startposition", "").strip("[] ").split())
        elevation = float(disp.get("elevation", "0") or 0)
    except ValueError:
        return None
    n = 1 << power
    w = n + 1
    alphas = _rows(disp, "alphas", w)
    if not alphas or len(alphas) != w or not any(v > 0.5 for r in alphas for v in r):
        return None
    normals = _rows(disp, "normals", 3 * w)
    dists = _rows(disp, "distances", w)
    offsets = _rows(disp, "offsets", 3 * w) or [[0.0] * (3 * w)] * w
    if not normals or not dists or len(normals) != w or len(dists) != w or len(offsets) != w:
        return None
    poly = _polygon(planes, k)
    if len(poly) != 4 or len(start) != 3:
        return None
    s = min(range(4), key=lambda i: _dot(_sub(poly[i], start), _sub(poly[i], start)))
    face_n = planes[k][0]

    def displaced(c, i, j):
        nx, ny, nz = normals[i][3 * j:3 * j + 3]
        ox, oy, oz = offsets[i][3 * j:3 * j + 3]
        dd = dists[i][j]
        base = _lerp(_lerp(c[0], c[1], i / n), _lerp(c[3], c[2], i / n), j / n)
        return _add(_add(base, (nx * dd + ox, ny * dd + oy, nz * dd + oz)), _mul(face_n, elevation))

    # the grid's first index runs from the start corner against the winding of _polygon
    # (checked on real maps); the other direction is only a fallback, ties go to the first
    hyps = []
    for step in (-1, 1):
        c = [poly[(s + step * q) % 4] for q in range(4)]
        hyps.append({(i, j): displaced(c, i, j) for i in (0, n) for j in (0, n)})
    return Disp(normalize_material(side.get("material", "")), n, alphas, hyps)


def only_blends(disps, sources):
    """Keeps the displacements whose material is a two layer blend; keys become the material
    names used in the .vmap (materials.out_name)."""
    from .materials import parse_vmt

    def load(rel):
        data, _src = sources.read(rel)
        return data

    out = {}
    for mat, lst in disps.items():
        rel = f"materials/{mat}.vmt"
        data = load(rel)
        if not data:
            continue
        try:
            shader = parse_vmt(data, rel, load).shader
        except Exception:  # noqa: BLE001
            continue
        if shader in BLEND_SHADERS:
            out.setdefault(out_name(mat), []).extend(lst)
    return out


# ---------------------------------------------------------------------------
# .vmap side
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|[{}\[\]]')
_MESH_RE = re.compile(r'"CMapMesh"\s*\r?\n[ \t]*\{')


def _close(text, open_pos):
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


def _array(text, name, start=0, end=None):
    """Values of '"name" "<type>_array" [ ... ]' as lists of floats, plus the span of the
    brackets. Returns (values, (open, close)) or (None, None)."""
    m = re.compile(r'"%s" "\w+_array" *\r?\n[ \t]*\[' % re.escape(name)).search(text, start, end or len(text))
    if not m:
        return None, None
    close = _close(text, m.end() - 1)
    vals = [[float(x) for x in s.split()] for s in re.findall(r'"([^"]*)"', text[m.end():close - 1])]
    return vals, (m.end() - 1, close)


def _block(text, header, start=0, end=None):
    """(start, end) of the '{...}' after header."""
    i = text.find(header, start, end or len(text))
    if i < 0:
        return None
    o = text.find("{", i)
    return o, _close(text, o)


def _stream(text, span, name):
    """Data of stream 'name' inside the data array block span."""
    i = text.find(f'"name" "string" "{name}"', span[0], span[1])
    if i < 0:
        return None
    vals, _sp = _array(text, "data", i, span[1])
    return vals


def _streams_end(text, span):
    """Index of the ']' closing the streams array of a data array block."""
    m = re.compile(r'"streams" "element_array" *\r?\n[ \t]*\[').search(text, span[0], span[1])
    if not m:
        return None
    return _close(text, m.end() - 1) - 1


def _stream_text(ind, name, sem, location, flags, values, binding):
    i1, i2, i3 = ind + "\t", ind + "\t\t", ind + "\t\t\t"
    lines = [f'{ind}"CDmePolygonMeshDataStream"', f"{ind}{{",
             f'{i1}"id" "elementid" "{uuid.uuid4()}"',
             f'{i1}"name" "string" "{name}"',
             f'{i1}"standardAttributeName" "string" ""',
             f'{i1}"semanticName" "string" "{sem}"',
             f'{i1}"semanticIndex" "int" "0"',
             f'{i1}"vertexBufferLocation" "int" "{location}"',
             f'{i1}"dataStateFlags" "int" "{flags}"']
    if binding:
        lines += [f'{i1}"subdivisionBinding" "CDmePolygonMeshSubdivisiondataBinding"', f"{i1}{{",
                  f'{i2}"id" "elementid" "{uuid.uuid4()}"',
                  f'{i2}"name" "string" "subdivisionBinding"',
                  f'{i2}"targetDataType" "int" "3"',
                  f'{i2}"targetStreamIndex" "int" "0"',
                  f'{i2}"streamSourceType" "int" "0"',
                  f"{i1}}}", ""]
    else:
        lines.append(f'{i1}"subdivisionBinding" "element" ""')
    body = ",\n".join(f'{i3}"{_v4(v)}"' for v in values)
    lines += [f'{i1}"data" "vector4_array" ', f"{i1}[", body, f"{i1}]", f"{ind}}}"]
    return "\n".join(x for x in lines if x is not None)


def _v4(a):
    return f"{a:.6g} 0 0 0" if a > 0 else "0 0 0 0"


def _inv_bilinear(p, q):
    """(s, t) with p = bilinear(q00, q10, q11, q01)."""
    s = t = 0.5
    for _ in range(12):
        x = [(1 - s) * (1 - t) * q[0][k] + s * (1 - t) * q[1][k] + s * t * q[2][k] + (1 - s) * t * q[3][k] - p[k]
             for k in (0, 1)]
        ds = [(1 - t) * (q[1][k] - q[0][k]) + t * (q[2][k] - q[3][k]) for k in (0, 1)]
        dt = [(1 - s) * (q[3][k] - q[0][k]) + s * (q[2][k] - q[1][k]) for k in (0, 1)]
        det = ds[0] * dt[1] - ds[1] * dt[0]
        if abs(det) < 1e-12:
            break
        s -= (x[0] * dt[1] - x[1] * dt[0]) / det
        t -= (ds[0] * x[1] - ds[1] * x[0]) / det
    return min(max(s, 0.0), 1.0), min(max(t, 0.0), 1.0)


class _Index:
    def __init__(self, disps, cell=32.0):
        self.cell = cell
        self.grid = {}
        for d in disps:
            self.grid.setdefault(self._key(d.centroid), []).append(d)

    def _key(self, p):
        return tuple(int(math.floor(v / self.cell)) for v in p)

    def near(self, p):
        kx, ky, kz = self._key(p)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    yield from self.grid.get((kx + dx, ky + dy, kz + dz), ())


def _match(index, corners, tol=2.0):
    """Best (disp, {face vertex k: (i, j)}) for the world positions of an S2 face's corners."""
    c = _mul((sum(p[0] for p in corners), sum(p[1] for p in corners), sum(p[2] for p in corners)),
             1.0 / len(corners))
    best = None
    for d in index.near(c):
        for hyp in d.corners:
            assign, err = {}, 0.0
            for k, p in enumerate(corners):
                ij, e = min(((ij, math.dist(p, q)) for ij, q in hyp.items()), key=lambda x: x[1])
                assign[k] = ij
                err = max(err, e)
            if err <= tol and len(set(assign.values())) == 4 and (best is None or err < best[0]):
                best = (err, d, assign)
    return (best[1], best[2]) if best else (None, None)


def _paint_mesh(mesh, materials, indexes, stats):
    """Returns the mesh text with blend paint streams, or None when nothing applies."""
    if '"displacement:0"' not in mesh or "VertexPaintBlendParams" in mesh:
        return None
    mats_vals = re.search(r'"materials" "string_array" *\r?\n[ \t]*\[(.*?)\]', mesh, re.S)
    if not mats_vals:
        return None
    mats = re.findall(r'"([^"]*)"', mats_vals.group(1))
    keys = [out_name(normalize_material(m[:-5] if m.lower().endswith(".vmat") else m)) for m in mats]
    if not any(k in materials for k in keys):
        return None
    md = _block(mesh, '"meshData" "CDmePolygonMesh"')
    if md is None:
        return None
    after = mesh[md[1]:]
    om = re.search(r'"origin" "vector3" "([^"]*)"', after)
    am = re.search(r'"angles" "qangle" "([^"]*)"', after)
    sm = re.search(r'"scales" "vector3" "([^"]*)"', after)
    if not om or (am and any(abs(float(v)) > 1e-4 for v in am.group(1).split())) \
            or (sm and any(abs(float(v) - 1) > 1e-4 for v in sm.group(1).split())):
        return None
    origin = tuple(float(v) for v in om.group(1).split())

    get = lambda name: [int(v[0]) for v in (_array(mesh, name, md[0], md[1])[0] or [])]  # noqa: E731
    vert_data = get("vertexDataIndices")
    edge_vert = get("edgeVertexIndices")
    edge_next = get("edgeNextIndices")
    edge_fvd = get("edgeVertexDataIndices")
    edge_face = get("edgeFaceIndices")
    face_edge = get("faceEdgeIndices")
    face_data = get("faceDataIndices")
    vd = _block(mesh, '"vertexData" "CDmePolygonMeshDataArray"', md[0], md[1])
    fvd = _block(mesh, '"faceVertexData" "CDmePolygonMeshDataArray"', md[0], md[1])
    fd = _block(mesh, '"faceData" "CDmePolygonMeshDataArray"', md[0], md[1])
    sd = _block(mesh, '"subdivisionData" "CDmePolygonMeshSubdivisionData"', md[0], md[1])
    if not (vd and fvd and fd and sd):
        return None
    pos = _stream(mesh, vd, "position:0")
    fv_tc = _stream(mesh, fvd, "texcoord:0")
    mat_idx = _stream(mesh, fd, "materialindex:0")
    sub_tc = _stream(mesh, sd, "texcoord:0")
    levels, _sp = _array(mesh, "subdivisionLevels", sd[0], sd[1])
    fv_size = re.search(r'"size" "int" "(\d+)"', mesh[fvd[0]:fvd[1]])
    if not (pos and fv_tc and mat_idx and sub_tc and levels and fv_size):
        return None

    fv_vals = [0.0] * int(fv_size.group(1))
    sub_vals = [0.0] * len(sub_tc)
    painted = 0
    levels_of, faces = {}, {}
    for f, e0 in enumerate(face_edge):
        loop, e = [], e0
        for _ in range(64):
            loop.append(e)
            e = edge_next[e]
            if e == e0:
                break
        levels_of[f] = max((int(levels[x][0]) for x in loop if x < len(levels)), default=0)
        if levels_of[f] <= 0 or len(loop) != 4:
            continue
        mi = int(mat_idx[face_data[f]][0]) if f < len(face_data) else 0
        key = keys[mi] if mi < len(keys) else None
        if key not in materials:
            continue
        corners = [_add(origin, tuple(pos[vert_data[edge_vert[e]]])) for e in loop]
        disp, assign = _match(indexes[key], corners)
        if disp is None:
            stats["blend_miss"] += 1
            continue
        n = disp.n
        tcs = [fv_tc[edge_fvd[e]][:2] for e in loop]
        by_ij = {assign[k]: tcs[k] for k in range(4)}
        faces[f] = (disp, [by_ij[(0, 0)], by_ij[(n, 0)], by_ij[(n, n)], by_ij[(0, n)]])
        for k, e in enumerate(loop):
            i, j = assign[k]
            fv_vals[edge_fvd[e]] = disp.alpha(i, j) / 255.0
        painted += 1
    # every face corner owns one patch of (2^(level-1) + 1)^2 subdivision vertices (a quad
    # has 4); the patches follow the order of the face vertex data, not the face order
    cursor = 0
    corners_in_order = sorted((e for e in range(len(edge_face)) if edge_face[e] >= 0), key=lambda e: edge_fvd[e])
    for e in corners_in_order:
        lvl = levels_of.get(edge_face[e], 0)
        if lvl <= 0:
            continue
        side = (1 << (lvl - 1)) + 1
        block = range(cursor, cursor + side * side)
        cursor += side * side
        face = faces.get(edge_face[e])
        if face is None or cursor > len(sub_tc):
            continue
        disp, q = face
        for r in block:
            s, t = _inv_bilinear(sub_tc[r][:2], q)
            sub_vals[r] = disp.alpha(s * disp.n, t * disp.n) / 255.0
    if not painted or cursor != len(sub_tc):
        if painted:
            stats["blend_miss"] += painted
        return None

    out = mesh
    # subdivision stream first (it lies after faceVertexData, so the faceVertexData span stays valid)
    for span, location, flags, vals, binding in ((sd, 0, 0, sub_vals, False), (fvd, 1, 1, fv_vals, True)):
        end = _streams_end(out, span)
        if end is None:
            return None
        line_start = out.rfind("\n", 0, end) + 1
        ind = re.match(r"[ \t]*", out[line_start:]).group(0) + "\t"
        prev = out[:line_start].rstrip()
        sep = "," if prev.endswith("}") else ""
        new = _stream_text(ind, "VertexPaintBlendParams:0", "VertexPaintBlendParams", location, flags, vals, binding)
        out = prev + sep + "\n" + new + "\n" + out[line_start:]
    stats["blend_faces"] += painted
    stats["blend_meshes"] += 1
    return out


def paint_blends(text, disps, stats):
    """disps: {out_name(material): [Disp]} for blend materials. Adds the paint streams to every
    subdivided mesh that uses them. Returns the new text."""
    if not disps:
        return text
    indexes = {m: _Index(ds) for m, ds in disps.items()}
    out, pos = [], 0
    for m in _MESH_RE.finditer(text):
        if m.start() < pos:
            continue
        end = _close(text, m.end() - 1)
        mesh = text[m.start():end]
        new = _paint_mesh(mesh, disps, indexes, stats)
        if new is not None:
            out.append(text[pos:m.start()])
            out.append(new)
            pos = end
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)
