"""Texture scale / shift values of the imported faces.

The importer works out the texture coordinates with the size of the Source 1 texture, so the
faces look right. But for materials that are not in CS2 yet at import time it writes the scale
and shift as if the texture were 8192 wide (0.25 -> 0.015625), so Hammer shows wrong values and
"Justify" or any texture edit moves the texture. Hammer uses the size of the material's color
texture (RepresentativeTextureWidth / Height).

For every face: the width the importer assumed follows from two corners,
    W_assumed = d(axis . position) / (scale * d(texcoord))
and with r = W_assumed / W_hammer the values Hammer needs are scale * r and shift / r. Faces
that already match (r = 1) are left alone.
"""

import math
import os
import re

from PIL import Image

from .blend import _MESH_RE, _array, _block, _close, _stream

_TEX_KEYS = ("TextureColor", "TextureLayer1Color", "TextureNormal", "TextureLayer1Normal")
_MATS_RE = re.compile(r'"materials" "string_array" *\r?\n[ \t]*\[(.*?)\]', re.S)


class TextureSizes:
    """Size of the color texture of the addon's materials: {'materials/x.vmat': (w, h)}."""

    def __init__(self, content_dir):
        self.content_dir = content_dir
        self.cache = {}

    def get(self, vmat):
        key = vmat.lower()
        if key not in self.cache:
            self.cache[key] = self._read(key)
        return self.cache[key]

    def animated(self, vmat):
        """True when the material plays a frame sheet (Texture Animation)."""
        key = "anim:" + vmat.lower()
        if key not in self.cache:
            text = self._text(vmat.lower()) or ""
            self.cache[key] = bool(re.search(r'^\s*"?F_TEXTURE_ANIMATION"?\s+"?1', text, re.M))
        return self.cache[key]

    def _text(self, vmat):
        path = os.path.join(self.content_dir, *vmat.split("/"))
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return None

    def _read(self, vmat):
        text = self._text(vmat)
        if text is None:
            return None
        for key in _TEX_KEYS:
            m = re.search(r'^\s*"?%s"?\s+"([^"]+)"' % key, text, re.M)
            if not m or m.group(1).startswith("["):
                continue
            img = os.path.join(self.content_dir, *m.group(1).replace("\\", "/").split("/"))
            try:
                with Image.open(img) as im:
                    return im.size
            except (OSError, ValueError):
                return None
        return None


def _fmt(v):
    v = round(v, 6)
    return str(int(v)) if v == int(v) else f"{v:.6g}"


def _replace_stream(mesh, span, name, values):
    """Replaces the data of stream 'name' in the data array block span; returns the mesh text."""
    i = mesh.find(f'"name" "string" "{name}"', span[0], span[1])
    if i < 0:
        return mesh
    m = re.compile(r'"data" "\w+_array" *\r?\n[ \t]*\[').search(mesh, i, span[1])
    if not m:
        return mesh
    close = _close(mesh, m.end() - 1) - 1
    ind = re.match(r"[ \t]*", mesh[mesh.rfind("\n", 0, m.start()) + 1:]).group(0)
    body = ",\n".join(f'{ind}\t"{" ".join(_fmt(c) for c in v)}"' for v in values)
    return mesh[:m.end()] + "\n" + body + "\n" + ind + mesh[close:]


def _fix_mesh(mesh, sizes, stats, fixed):
    m = _MATS_RE.search(mesh)
    if not m:
        return None
    mats = re.findall(r'"([^"]*)"', m.group(1))
    dims = [sizes.get(x) for x in mats]
    preset = [fixed.get(x.lower()) for x in mats]
    if not any(dims) and not any(p is not None for p in preset):
        return None
    md = _block(mesh, '"meshData" "CDmePolygonMesh"')
    if md is None:
        return None
    after = mesh[md[1]:]
    am = re.search(r'"angles" "qangle" "([^"]*)"', after)
    sm = re.search(r'"scales" "vector3" "([^"]*)"', after)
    if (am and any(abs(float(v)) > 1e-4 for v in am.group(1).split())) \
            or (sm and any(abs(float(v) - 1) > 1e-4 for v in sm.group(1).split())):
        return None         # positions are not world aligned
    get = lambda name: [int(v[0]) for v in (_array(mesh, name, md[0], md[1])[0] or [])]  # noqa: E731
    vert_data = get("vertexDataIndices")
    edge_vert = get("edgeVertexIndices")
    edge_next = get("edgeNextIndices")
    edge_fvd = get("edgeVertexDataIndices")
    face_edge = get("faceEdgeIndices")
    face_data = get("faceDataIndices")
    vd = _block(mesh, '"vertexData" "CDmePolygonMeshDataArray"', md[0], md[1])
    fvd = _block(mesh, '"faceVertexData" "CDmePolygonMeshDataArray"', md[0], md[1])
    fd = _block(mesh, '"faceData" "CDmePolygonMeshDataArray"', md[0], md[1])
    if not (vd and fvd and fd):
        return None
    pos = _stream(mesh, vd, "position:0")
    tc = _stream(mesh, fvd, "texcoord:0")
    mat_idx = _stream(mesh, fd, "materialindex:0")
    scale = _stream(mesh, fd, "textureScale:0")
    axis_u = _stream(mesh, fd, "textureAxisU:0")
    axis_v = _stream(mesh, fd, "textureAxisV:0")
    if not (pos and tc and mat_idx and scale and axis_u and axis_v):
        return None
    changed = 0
    done = set()
    for f, e0 in enumerate(face_edge):
        di = face_data[f] if f < len(face_data) else -1
        if di < 0 or di in done or di >= len(scale):
            continue
        done.add(di)
        mi = int(mat_idx[di][0])
        value = preset[mi] if 0 <= mi < len(preset) else None
        if value is not None:
            # faces whose texture place does not matter get one fixed scale and no shift
            if list(scale[di][:2]) != [value, value] or axis_u[di][3] != 0 or axis_v[di][3] != 0:
                scale[di] = [value, value] + list(scale[di][2:])
                axis_u[di] = list(axis_u[di][:3]) + [0.0]
                axis_v[di] = list(axis_v[di][:3]) + [0.0]
                changed += 1
                stats["preset_faces"] = stats.get("preset_faces", 0) + 1
            continue
        dim = dims[mi] if 0 <= mi < len(dims) else None
        if not dim:
            continue
        loop, e = [], e0
        for _ in range(256):
            loop.append(e)
            e = edge_next[e]
            if e == e0:
                break
        pts = [pos[vert_data[edge_vert[x]]][:3] for x in loop]
        uvs = [tc[edge_fvd[x]][:2] for x in loop]
        new_scale = list(scale[di])
        new_u, new_v = list(axis_u[di]), list(axis_v[di])
        fixed = False
        for k, axis, size in ((0, new_u, dim[0]), (1, new_v, dim[1])):
            s = scale[di][k]
            if abs(s) < 1e-9 or size <= 0:
                continue
            dots = [p[0] * axis[0] + p[1] * axis[1] + p[2] * axis[2] for p in pts]
            # the corner pair farthest apart along the axis gives the most exact ratio
            a = min(range(len(pts)), key=lambda i: dots[i])
            b = max(range(len(pts)), key=lambda i: dots[i])
            du = uvs[b][k] - uvs[a][k]
            if abs(dots[b] - dots[a]) < 1e-3 or abs(du) < 1e-9:
                continue
            r = (dots[b] - dots[a]) / (s * du * size)
            if r <= 0 or abs(r - 1.0) < 0.01:
                continue
            new_scale[k] = s * r
            axis[3] = axis[3] / r
            fixed = True
        if fixed:
            scale[di], axis_u[di], axis_v[di] = new_scale, new_u, new_v
            changed += 1
            stats["uv_faces"] += 1
    if not changed:
        return None
    out = mesh
    # later blocks first, so the earlier spans stay valid (all three streams are in faceData)
    for name, values in (("textureAxisV:0", axis_v), ("textureAxisU:0", axis_u), ("textureScale:0", scale)):
        fd = _block(out, '"faceData" "CDmePolygonMeshDataArray"', *_block(out, '"meshData" "CDmePolygonMesh"'))
        out = _replace_stream(out, fd, name, values)
    stats["uv_meshes"] += 1
    return out


def _anim_mesh(mesh, sizes, stats):
    """Moves the texture coordinates of faces with a frame sheet material into 0..1.

    The shader finds the frame by adding the cell to the texture coordinate and lets the
    texture wrap, so a face at u = -89..-88 shows the cell 89 columns further on: the frames
    play in the wrong order. Whole numbers do not change how a face looks otherwise."""
    m = _MATS_RE.search(mesh)
    if not m:
        return None
    mats = re.findall(r'"([^"]*)"', m.group(1))
    anim = [sizes.animated(x) for x in mats]
    if not any(anim):
        return None
    md = _block(mesh, '"meshData" "CDmePolygonMesh"')
    if md is None:
        return None
    get = lambda name: [int(v[0]) for v in (_array(mesh, name, md[0], md[1])[0] or [])]  # noqa: E731
    edge_next = get("edgeNextIndices")
    edge_fvd = get("edgeVertexDataIndices")
    face_edge = get("faceEdgeIndices")
    face_data = get("faceDataIndices")
    fvd = _block(mesh, '"faceVertexData" "CDmePolygonMeshDataArray"', md[0], md[1])
    fd = _block(mesh, '"faceData" "CDmePolygonMeshDataArray"', md[0], md[1])
    if not (fvd and fd):
        return None
    tc = _stream(mesh, fvd, "texcoord:0")
    mat_idx = _stream(mesh, fd, "materialindex:0")
    if not (tc and mat_idx):
        return None
    geo = _anim_geometry(mesh, md, fd, get)
    owner = {}
    changed = 0
    for f, e0 in enumerate(face_edge):
        di = face_data[f] if f < len(face_data) else -1
        if di < 0 or di >= len(mat_idx):
            continue
        mi = int(mat_idx[di][0])
        if not (0 <= mi < len(anim) and anim[mi]):
            continue
        edges, e = [], e0
        for _ in range(256):
            edges.append(e)
            e = edge_next[e]
            if e == e0:
                break
        loop = [edge_fvd[x] for x in edges]
        if any(owner.get(x, f) != f for x in loop):
            continue        # corner data shared with another face
        for x in loop:
            owner[x] = f
        moved = False
        for k in (0, 1):
            vals = [tc[x][k] for x in loop]
            lo, hi = min(vals), max(vals)
            # a face that shows one frame starts at a whole number (up to rounding)
            n = round(lo) if abs(lo - round(lo)) < 0.02 else math.floor(lo)
            span = hi - lo
            # one frame, a little off (0.01 .. 1.01): it is fitted to 0 .. 1 exactly, or a
            # thin line of the next frame shows at the edge
            fit = 0.97 <= span <= 1.03 and (abs(lo - n) > 1e-6 or abs(span - 1.0) > 1e-6)
            if not n and not fit:
                continue
            for x in set(loop):
                uv = list(tc[x])
                v = (uv[k] - lo) / span if fit else uv[k] - n
                uv[k] = float(round(v)) if abs(v - round(v)) < 0.002 else v
                tc[x] = uv
            moved = True
        fitted = bool(geo) and _fit_axes(geo, edges, di, loop, tc, sizes.get(mats[mi]))
        if moved or fitted:
            changed += 1
    if not changed:
        return None
    stats["anim_faces"] = stats.get("anim_faces", 0) + changed
    if geo and geo["dirty"]:
        # later blocks first, so the earlier spans stay valid (all three streams are in faceData)
        for name, values in (("textureAxisV:0", geo["axis_v"]), ("textureAxisU:0", geo["axis_u"]),
                             ("textureScale:0", geo["scale"])):
            fd = _block(mesh, '"faceData" "CDmePolygonMeshDataArray"', *_block(mesh, '"meshData" "CDmePolygonMesh"'))
            mesh = _replace_stream(mesh, fd, name, values)
        md = _block(mesh, '"meshData" "CDmePolygonMesh"')
        fvd = _block(mesh, '"faceVertexData" "CDmePolygonMeshDataArray"', md[0], md[1])
    return _replace_stream(mesh, fvd, "texcoord:0", tc)


def _anim_geometry(mesh, md, fd, get):
    """World positions and texture axes of a mesh, for _fit_axes; None when the mesh is
    turned or scaled."""
    after = mesh[md[1]:]
    am = re.search(r'"angles" "qangle" "([^"]*)"', after)
    sm = re.search(r'"scales" "vector3" "([^"]*)"', after)
    om = re.search(r'"origin" "vector3" "([^"]*)"', after)
    if (am and any(abs(float(v)) > 1e-4 for v in am.group(1).split())) \
            or (sm and any(abs(float(v) - 1) > 1e-4 for v in sm.group(1).split())):
        return None
    org = [float(v) for v in om.group(1).split()] if om else [0.0, 0.0, 0.0]
    vd = _block(mesh, '"vertexData" "CDmePolygonMeshDataArray"', md[0], md[1])
    pos = _stream(mesh, vd, "position:0") if vd else None
    scale = _stream(mesh, fd, "textureScale:0")
    axis_u = _stream(mesh, fd, "textureAxisU:0")
    axis_v = _stream(mesh, fd, "textureAxisV:0")
    if not (pos and scale and axis_u and axis_v):
        return None
    return {"pos": [[p[0] + org[0], p[1] + org[1], p[2] + org[2]] for p in pos],
            "vert_data": get("vertexDataIndices"), "edge_vert": get("edgeVertexIndices"),
            "scale": scale, "axis_u": axis_u, "axis_v": axis_v, "dirty": False}


def _fit_axes(geo, face, di, loop_fvd, tc, dim):
    """Scale and shift Hammer shows for the face, worked out again from its new texture
    coordinates: texcoord = (position . axis) / (scale * W) + shift / W. The shift is kept
    within one texture (0 .. W), the whole textures are in the texture coordinates.
    face: the edges of the face, loop_fvd: their corner data indices."""
    if not dim or di >= len(geo["scale"]):
        return False
    corners = [geo["pos"][geo["vert_data"][geo["edge_vert"][e]]] for e in face]
    new_scale = list(geo["scale"][di])
    done = False
    for k, axes, size in ((0, geo["axis_u"], dim[0]), (1, geo["axis_v"], dim[1])):
        ax = axes[di]
        if size <= 0:
            continue
        dots = [p[0] * ax[0] + p[1] * ax[1] + p[2] * ax[2] for p in corners]
        vals = [tc[x][k] for x in loop_fvd]
        a = min(range(len(dots)), key=lambda i: dots[i])
        b = max(range(len(dots)), key=lambda i: dots[i])
        if abs(dots[b] - dots[a]) < 1e-3 or abs(vals[b] - vals[a]) < 1e-6:
            continue
        s = (dots[b] - dots[a]) / ((vals[b] - vals[a]) * size)
        shift = (vals[a] - dots[a] / (s * size)) * size
        # every corner has to follow the same plane mapping, or the values are left alone
        if max(abs(d / (s * size) + shift / size - v) for d, v in zip(dots, vals)) > 0.01:
            continue
        axis = list(ax[:3])
        if s < 0:
            # a flipped texture: positive scale, the axis turned around (same mapping)
            axis, s = [-c for c in axis], -s
        new = axis + [round(shift % size, 4)]
        if abs(new_scale[k] - s) > 1e-6 or any(abs(x - y) > 1e-3 for x, y in zip(new, ax)):
            new_scale[k] = s
            axes[di] = new
            done = True
    if done:
        geo["scale"][di] = new_scale
        geo["dirty"] = True
    return done


def fix_animated_uvs(text, sizes, stats):
    """Texture coordinates of faces with a frame sheet material start at 0 (see _anim_mesh)."""
    out, pos = [], 0
    for m in _MESH_RE.finditer(text):
        if m.start() < pos:
            continue
        end = _close(text, m.end() - 1)
        new = _anim_mesh(text[m.start():end], sizes, stats)
        if new is not None:
            out.append(text[pos:m.start()])
            out.append(new)
            pos = end
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


def fix_texture_scales(text, sizes, stats, fixed=None):
    """sizes: TextureSizes of the addon. fixed: {'materials/x.vmat': scale} for materials whose
    faces get that scale and shift 0. Returns the new text."""
    fixed = {k.lower(): float(v) for k, v in (fixed or {}).items()}
    out, pos = [], 0
    for m in _MESH_RE.finditer(text):
        if m.start() < pos:
            continue
        end = _close(text, m.end() - 1)
        new = _fix_mesh(text[m.start():end], sizes, stats, fixed)
        if new is not None:
            out.append(text[pos:m.start()])
            out.append(new)
            pos = end
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)
