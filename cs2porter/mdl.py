"""Reader for compiled Source 1 models (MDL v44-49 + VVD + VTX + PHY).

Decompiles the model back into QC + SMD, so embedded models and models from game
VPKs can be converted with tehlikeli91's VMDL template too.
"""

import math
import os
import re
import struct

import numpy as np

STUDIOHDR_FLAGS_STATIC_PROP = 0x10
IVP_TO_INCH = 1.0 / 0.0254


class MDLError(Exception):
    pass


def _cstr(data, off, maxlen=None):
    if off <= 0 or off >= len(data):
        return ""
    end = data.find(b"\0", off, off + maxlen if maxlen else len(data))
    if end < 0:
        end = off + maxlen if maxlen else len(data)
    return data[off:end].decode("latin-1")


def _i(data, off):
    return struct.unpack_from("<i", data, off)[0]


# ---------------------------------------------------------------------------
# MDL
# ---------------------------------------------------------------------------

class Bone:
    __slots__ = ("name", "parent", "pos", "quat", "rot", "pose_to_bone")


class Mesh:
    __slots__ = ("material", "num_vertices", "vertex_offset")


class SubModel:
    __slots__ = ("name", "meshes", "vertex_index", "num_vertices")


class BodyPart:
    __slots__ = ("name", "models")


class MDL:
    def __init__(self, data):
        if len(data) < 408 or data[:4] != b"IDST":
            raise MDLError("No MDL signature")
        self.version = _i(data, 4)
        if not 44 <= self.version <= 49:
            raise MDLError(f"Unsupported MDL version: {self.version}")
        self.checksum = _i(data, 8)
        self.name = _cstr(data, 12, 64)
        self.flags = _i(data, 152)
        self.hull = struct.unpack_from("<6f", data, 104)
        self.illum = struct.unpack_from("<3f", data, 92)

        # bones
        n, idx = struct.unpack_from("<ii", data, 156)
        self.bones = []
        for i in range(n):
            b = idx + i * 216
            bone = Bone()
            bone.name = _cstr(data, b + _i(data, b))
            bone.parent = _i(data, b + 4)
            bone.pos = struct.unpack_from("<3f", data, b + 32)
            bone.quat = struct.unpack_from("<4f", data, b + 44)
            bone.rot = struct.unpack_from("<3f", data, b + 60)
            bone.pose_to_bone = struct.unpack_from("<12f", data, b + 96)
            self.bones.append(bone)

        # animations (frame counts only, used to detect animated models)
        n, idx = struct.unpack_from("<ii", data, 180)
        self.anim_frames = []
        for i in range(min(n, 4096)):
            a = idx + i * 100
            if a + 20 <= len(data):
                self.anim_frames.append(_i(data, a + 16))
        self.num_sequences = _i(data, 188)

        # textures
        n, idx = struct.unpack_from("<ii", data, 204)
        self.textures = []
        for i in range(n):
            t = idx + i * 64
            self.textures.append(_cstr(data, t + _i(data, t)))
        n, idx = struct.unpack_from("<ii", data, 212)
        self.cdmaterials = [_cstr(data, _i(data, idx + i * 4)) for i in range(n)]

        # skin families
        nref, nfam, idx = struct.unpack_from("<iii", data, 220)
        self.skins = []
        for f in range(nfam):
            row = struct.unpack_from(f"<{nref}h", data, idx + f * nref * 2) if nref else ()
            self.skins.append(list(row))

        # body parts
        n, idx = struct.unpack_from("<ii", data, 232)
        self.bodyparts = []
        for i in range(n):
            bp = idx + i * 16
            part = BodyPart()
            part.name = _cstr(data, bp + _i(data, bp))
            nmodels, _base, midx = struct.unpack_from("<iii", data, bp + 4)
            part.models = []
            for j in range(nmodels):
                m = bp + midx + j * 148
                sub = SubModel()
                sub.name = _cstr(data, m, 64)
                nmesh, meshidx, nverts, vindex = struct.unpack_from("<iiii", data, m + 72)
                sub.num_vertices = nverts
                sub.vertex_index = vindex // 48
                sub.meshes = []
                for k in range(nmesh):
                    me = m + meshidx + k * 116
                    mesh = Mesh()
                    mesh.material, _mi, mesh.num_vertices, mesh.vertex_offset = struct.unpack_from("<iiii", data, me)
                    sub.meshes.append(mesh)
                part.models.append(sub)
            self.bodyparts.append(part)

        self.surfaceprop = _cstr(data, _i(data, 308))
        kvi, kvs = struct.unpack_from("<ii", data, 312)
        self.keyvalues = data[kvi:kvi + kvs].decode("latin-1", "replace").rstrip("\0") if kvs > 0 else ""

    @property
    def is_static(self):
        return bool(self.flags & STUDIOHDR_FLAGS_STATIC_PROP)

    @property
    def is_animated(self):
        return any(f > 1 for f in self.anim_frames)


# ---------------------------------------------------------------------------
# VVD
# ---------------------------------------------------------------------------

_VVD_VERTEX = np.dtype([("w", "<f4", 3), ("b", "u1", 3), ("nb", "u1"),
                        ("pos", "<f4", 3), ("n", "<f4", 3), ("uv", "<f4", 2)])


def read_vvd(data, checksum=None):
    if data[:4] != b"IDSV":
        raise MDLError("No VVD signature")
    _ver, chk, nlods = struct.unpack_from("<iii", data, 4)
    lod_verts = struct.unpack_from("<8i", data, 16)
    nfix, fixstart, vstart, tstart = struct.unpack_from("<iiii", data, 48)
    if checksum is not None and chk != checksum:
        raise MDLError("VVD checksum does not match the MDL")
    count = lod_verts[0]
    if tstart > vstart:
        count = max(count, (tstart - vstart) // 48)
    count = min(count, (len(data) - vstart) // 48)
    verts = np.frombuffer(data, _VVD_VERTEX, count, vstart)
    if nfix:
        idx = []
        for i in range(nfix):
            lod, src, num = struct.unpack_from("<iii", data, fixstart + i * 12)
            if lod >= 0:
                idx.extend(range(src, src + num))
        verts = verts[np.asarray(idx, dtype=np.int64)]
    return verts


# ---------------------------------------------------------------------------
# VTX
# ---------------------------------------------------------------------------

def _vtx_meshes(data, sg_size):
    """[(bodypart, model) -> [mesh -> [tri index list (orig mesh vert ids)]]]"""
    n_bp, bp_off = struct.unpack_from("<ii", data, 28)
    out = []
    size = len(data)
    for i in range(n_bp):
        bp = bp_off + i * 8
        n_models, m_off = struct.unpack_from("<ii", data, bp)
        models = []
        for j in range(n_models):
            m = bp + m_off + j * 8
            n_lods, lod_off = struct.unpack_from("<ii", data, m)
            meshes = []
            if n_lods > 0:
                lod = m + lod_off
                n_mesh, mesh_off = struct.unpack_from("<ii", data, lod)
                for k in range(n_mesh):
                    me = lod + mesh_off + k * 9
                    n_sg, sg_off = struct.unpack_from("<ii", data, me)
                    tris = []
                    for g in range(n_sg):
                        sg = me + sg_off + g * sg_size
                        nv, voff, ni, ioff = struct.unpack_from("<iiii", data, sg)
                        if nv < 0 or ni < 0 or ni % 3 or sg + voff + nv * 9 > size or sg + ioff + ni * 2 > size:
                            raise MDLError("VTX yapisi gecersiz")
                        if ni == 0:
                            continue
                        vraw = np.frombuffer(data, np.uint8, nv * 9, sg + voff).reshape(nv, 9)
                        orig = vraw[:, 4].astype(np.int64) | (vraw[:, 5].astype(np.int64) << 8)
                        ind = np.frombuffer(data, "<u2", ni, sg + ioff).astype(np.int64)
                        if ind.max() >= nv:
                            raise MDLError("VTX indeksi aralik disi")
                        tris.append(orig[ind])
                    meshes.append(np.concatenate(tris) if tris else np.zeros(0, np.int64))
            models.append(meshes)
        out.append(models)
    return out


def read_vtx(data, mdl_version):
    if len(data) < 36 or _i(data, 0) != 7:
        raise MDLError("Unsupported VTX version")
    sizes = (33, 25) if mdl_version >= 49 else (25, 33)
    last = None
    for s in sizes:
        try:
            return _vtx_meshes(data, s)
        except (MDLError, struct.error, ValueError) as e:
            last = e
    raise MDLError(f"VTX okunamadi: {last}")


# ---------------------------------------------------------------------------
# PHY
# ---------------------------------------------------------------------------

class PhysSolid:
    __slots__ = ("pieces",)        # [ (points Nx3 in source units, tris Mx3) ]


def read_phy(data):
    if len(data) < 16:
        raise MDLError("PHY cok kisa")
    hsize, _id, nsolids, _chk = struct.unpack_from("<iiii", data, 0)
    pos = hsize
    solids = []
    for _s in range(nsolids):
        size = _i(data, pos)
        start = pos + 4
        end = start + size
        if data[start:start + 4] == b"VPHY":
            surf = start + 28
        else:
            surf = start
        solid = PhysSolid()
        solid.pieces = []
        ledge = surf + 48
        points_at = None
        guard = 0
        while ledge + 16 <= end and guard < 100000:
            guard += 1
            if points_at is not None and ledge >= points_at:
                break
            p_off, _client, flags, ntri = struct.unpack_from("<iiIh", data, ledge)
            if ntri <= 0:
                break
            pts_base = ledge + p_off
            if points_at is None or pts_base < points_at:
                points_at = pts_base
            # ledges with child nodes are container shells built for the tree, skip them
            if not flags & 0x3:
                tris = []
                t = ledge + 16
                for _k in range(ntri):
                    e = struct.unpack_from("<4I", data, t)
                    tris.append((e[1] & 0xFFFF, e[2] & 0xFFFF, e[3] & 0xFFFF))
                    t += 16
                tris = np.asarray(tris, dtype=np.int64)
                used = int(tris.max()) + 1
                if pts_base < 0 or pts_base + used * 16 > len(data):
                    raise MDLError("PHY point array is outside the file")
                pts = np.frombuffer(data, "<f4", used * 4, pts_base).reshape(used, 4)[:, :3].astype(np.float64)
                solid.pieces.append((pts, tris))
            ledge += 16 + ntri * 16
        solids.append(solid)
        pos = end
    text = data[pos:].decode("latin-1", "replace").rstrip("\0")
    return solids, text


def _phy_solid_bones(text, bones):
    """Solid index -> bone index from the PHY text section."""
    names = {b.name.lower(): i for i, b in enumerate(bones)}
    out = {}
    for m in re.finditer(r"solid\s*\{(.*?)\}", text, re.S | re.I):
        body = m.group(1)
        im = re.search(r'"index"\s*"(\d+)"', body)
        nm = re.search(r'"name"\s*"([^"]*)"', body)
        if im and nm:
            out[int(im.group(1))] = names.get(nm.group(1).lower(), 0)
    return out


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------

def _angle_matrix(rx, ry, rz):
    """RadianEuler (x=roll, y=pitch, z=yaw) -> 3x3 (Source AngleMatrix)."""
    sr, cr = math.sin(rx), math.cos(rx)
    sp, cp = math.sin(ry), math.cos(ry)
    sy, cy = math.sin(rz), math.cos(rz)
    return np.array([
        [cp * cy, sr * sp * cy - cr * sy, cr * sp * cy + sr * sy],
        [cp * sy, sr * sp * sy + cr * cy, cr * sp * sy - sr * cy],
        [-sp, sr * cp, cr * cp],
    ])


def _bone_to_world(bones):
    """4x4 model space matrix of every bone (bind pose)."""
    mats = []
    for b in bones:
        m = np.eye(4)
        m[:3, :3] = _angle_matrix(*b.rot)
        m[:3, 3] = b.pos
        if 0 <= b.parent < len(mats):
            m = mats[b.parent] @ m
        mats.append(m)
    return mats


def _matrix_to_euler(m):
    """3x3 -> RadianEuler (x=roll, y=pitch, z=yaw); inverse of _angle_matrix."""
    sp = -m[2][0]
    sp = max(-1.0, min(1.0, sp))
    pitch = math.asin(sp)
    if abs(math.cos(pitch)) > 1e-6:
        yaw = math.atan2(m[1][0], m[0][0])
        roll = math.atan2(m[2][1], m[2][2])
    else:
        yaw = math.atan2(-m[0][1], m[1][1])
        roll = 0.0
    return roll, pitch, yaw


# Compiled S1 model space -> SMD space. CS2 ModelDoc, like studiomdl, rotates the
# SMD +90 degrees around Z on import, so this transform is needed.
ROT_SMD = np.array([[0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])


def _f(v):
    s = f"{v:.6f}"
    return "0.000000" if s == "-0.000000" else s


# ---------------------------------------------------------------------------
# SMD writing
# ---------------------------------------------------------------------------

def _skeleton_pose(mdl, rotate):
    """(pos, rot) for the SMD skeleton. Static props keep the bone as it is,
    other models also get their root bones rotated."""
    out = []
    for b in mdl.bones:
        if rotate is not None and b.parent < 0 and not mdl.is_static:
            m = rotate @ _angle_matrix(*b.rot)
            out.append((tuple(rotate @ np.asarray(b.pos)), _matrix_to_euler(m)))
        else:
            out.append((b.pos, b.rot))
    return out


def _smd_header(mdl, rotate):
    lines = ["version 1", "nodes"]
    bones = mdl.bones or []
    if not bones:
        lines.append('  0 "root" -1')
    for i, b in enumerate(bones):
        lines.append(f'  {i} "{b.name}" {b.parent}')
    lines += ["end", "skeleton", "  time 0"]
    if not bones:
        lines.append("    0 0.000000 0.000000 0.000000 0.000000 0.000000 0.000000")
    for i, (p, r) in enumerate(_skeleton_pose(mdl, rotate)):
        lines.append(f"    {i} {_f(p[0])} {_f(p[1])} {_f(p[2])} {_f(r[0])} {_f(r[1])} {_f(r[2])}")
    lines += ["end", "triangles"]
    return lines


def write_ref_smd(path, mdl, verts, sub, vtx_meshes, rotate=ROT_SMD):
    """Writes the LOD0 visual mesh of a sub model as SMD. Returns the triangle count."""
    pos = verts["pos"].astype(np.float64)
    nrm = verts["n"].astype(np.float64)
    if rotate is not None:
        pos = pos @ rotate.T
        nrm = nrm @ rotate.T
    lines = _smd_header(mdl, rotate)
    skin0 = mdl.skins[0] if mdl.skins else []
    ntri = 0
    for mesh, ind in zip(sub.meshes, vtx_meshes):
        if not len(ind):
            continue
        tex_i = skin0[mesh.material] if mesh.material < len(skin0) else mesh.material
        mat = mdl.textures[tex_i] if 0 <= tex_i < len(mdl.textures) else f"material{tex_i}"
        ids = ind + (sub.vertex_index + mesh.vertex_offset)
        uniq = np.unique(ids)
        if uniq.size and (uniq[0] < 0 or uniq[-1] >= len(verts)):
            raise MDLError("VVD/VTX vertex index out of range")
        vtext = {}
        for i, p, n, uv, w, b, nb in zip(uniq.tolist(), pos[uniq].tolist(), nrm[uniq].tolist(),
                                         verts["uv"][uniq].tolist(), verts["w"][uniq].tolist(),
                                         verts["b"][uniq].tolist(), verts["nb"][uniq].tolist()):
            nb = nb or 1
            links = " ".join(f"{b[j]} {_f(w[j])}" for j in range(min(nb, 3)))
            vtext[i] = (f"  {b[0]} {_f(p[0])} {_f(p[1])} {_f(p[2])} {_f(n[0])} {_f(n[1])} {_f(n[2])} "
                        f"{_f(uv[0])} {_f(1.0 - uv[1])} {min(nb, 3)} {links}")
        idl = ids.tolist()
        for t in range(0, len(idl) - 2, 3):
            lines.append(mat)
            lines.append(vtext[idl[t]])
            lines.append(vtext[idl[t + 2]])
            lines.append(vtext[idl[t + 1]])
            ntri += 1
    lines.append("end")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return ntri


def write_phys_smd(path, mdl, solids, solid_bones, rotate=ROT_SMD):
    """Writes the PHY collision parts as SMD (each convex part stays separate)."""
    mats = _bone_to_world(mdl.bones) if mdl.bones else [np.eye(4)]
    lines = _smd_header(mdl, rotate)
    ntri = 0
    for si, solid in enumerate(solids):
        bi = solid_bones.get(si, 0)
        if bi >= len(mats):
            bi = 0
        bm = mats[bi]
        for pts, tris in solid.pieces:
            # IVP (meters, y down) -> Source (inches)
            p = np.stack([pts[:, 0], pts[:, 2], -pts[:, 1]], axis=1) * IVP_TO_INCH
            # to model space: as it is for static props, -90 degrees for single part physics,
            # bind pose of the matching bone for multi part physics such as ragdolls
            if not mdl.is_static:
                if len(solids) == 1:
                    p = p @ ROT_SMD.T
                else:
                    p = p @ bm[:3, :3].T + bm[:3, 3]
            if rotate is not None:
                p = p @ rotate.T
            pl = p.tolist()
            for a, b, c in tris.tolist():
                pa, pb, pc = pl[a], pl[b], pl[c]
                n = np.cross(np.subtract(pb, pa), np.subtract(pc, pa))
                ln = float(np.linalg.norm(n))
                n = (n / ln).tolist() if ln > 1e-12 else [0.0, 0.0, 1.0]
                ns = f"{_f(n[0])} {_f(n[1])} {_f(n[2])}"
                lines.append("phy")
                for q in (pa, pb, pc):
                    lines.append(f"  {bi} {_f(q[0])} {_f(q[1])} {_f(q[2])} {ns} 0 0 1 {bi} 1")
                ntri += 1
    lines.append("end")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return ntri


ROTATED_MARK = "// CS2 Porter: rotated for CS2"


def rotate_smd_file(src, dst, rotate=ROT_SMD):
    """Rotates SMDs written in compiled space (non-static models) for CS2."""
    with open(src, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    if any(ln.startswith(ROTATED_MARK) for ln in lines[:3]):
        # already rotated (do not rotate the same file twice)
        if os.path.normcase(os.path.abspath(src)) != os.path.normcase(os.path.abspath(dst)):
            with open(dst, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines) + "\n")
        return False
    out = [ROTATED_MARK]
    section = None
    roots = set()
    for line in lines:
        s = line.strip()
        low = s.lower()
        if section is None and low in ("nodes", "skeleton", "triangles", "vertexanimation"):
            section = low
            out.append(line)
            continue
        if low == "end":
            section = None
            out.append(line)
            continue
        parts = s.split()
        try:
            if section == "nodes" and len(parts) >= 3 and parts[-1].lstrip("-").isdigit() and int(parts[-1]) < 0:
                roots.add(int(parts[0]))
            elif section == "skeleton" and len(parts) == 7 and int(parts[0]) in roots:
                p = rotate @ np.array([float(x) for x in parts[1:4]])
                r = _matrix_to_euler(rotate @ _angle_matrix(*(float(x) for x in parts[4:7])))
                line = f"    {parts[0]} {_f(p[0])} {_f(p[1])} {_f(p[2])} {_f(r[0])} {_f(r[1])} {_f(r[2])}"
            elif section == "triangles" and len(parts) >= 7:
                p = rotate @ np.array([float(x) for x in parts[1:4]])
                n = rotate @ np.array([float(x) for x in parts[4:7]])
                rest = " ".join(parts[7:])
                line = f"  {parts[0]} {_f(p[0])} {_f(p[1])} {_f(p[2])} {_f(n[0])} {_f(n[1])} {_f(n[2])} {rest}".rstrip()
        except ValueError:
            pass
        out.append(line)
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    return True


# ---------------------------------------------------------------------------
# Decompile a model to QC + SMD
# ---------------------------------------------------------------------------

VTX_EXTS = (".dx90.vtx", ".vtx", ".dx80.vtx", ".sw.vtx")


class Decompiled:
    def __init__(self):
        self.qc = ""
        self.mdl = None
        self.smds = []
        self.physics = None
        self.triangles = 0


def _safe(name):
    n = re.sub(r"[^A-Za-z0-9_\-]", "_", name).strip("_")
    return n or "body"


def decompile(read, mdl_rel, out_dir):
    """read(rel) -> bytes | None. Decompiles the model into out_dir as QC + SMD."""
    stem_rel = mdl_rel[:-4]
    data = read(mdl_rel)
    if not data:
        raise MDLError("Could not read the MDL")
    mdl = MDL(data)
    vvd = read(stem_rel + ".vvd")
    if not vvd:
        raise MDLError("no .vvd file")
    vtx = None
    for ext in VTX_EXTS:
        vtx = read(stem_rel + ext)
        if vtx:
            break
    if not vtx:
        raise MDLError("no .vtx file")
    verts = read_vvd(vvd, mdl.checksum)
    vtx_parts = read_vtx(vtx, mdl.version)
    if len(vtx_parts) != len(mdl.bodyparts):
        raise MDLError("VTX and MDL body part counts do not match")

    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.basename(stem_rel)
    res = Decompiled()
    res.mdl = mdl
    single = len(mdl.bodyparts) == 1 and len(mdl.bodyparts[0].models) == 1
    body_lines = []
    for bi, (part, vparts) in enumerate(zip(mdl.bodyparts, vtx_parts)):
        studios = []
        for mi, sub in enumerate(part.models):
            meshes = vparts[mi] if mi < len(vparts) else []
            if not sub.meshes or not any(len(x) for x in meshes):
                studios.append(None)
                continue
            if single:
                fname = f"{stem}.smd"
            else:
                fname = f"{stem}_{_safe(part.name)}_{_safe(os.path.splitext(sub.name)[0]) or mi}.smd"
            n = write_ref_smd(os.path.join(out_dir, fname), mdl, verts, sub, meshes)
            res.triangles += n
            res.smds.append(fname)
            studios.append(fname)
        if not any(studios):
            continue
        # standard decompile layout (the VMDL Generator also expects $bodygroup)
        inner = "\n".join(f'\tstudio "{s}"' if s else "\tblank" for s in studios)
        body_lines.append(f'$bodygroup "{part.name}"\n{{\n{inner}\n}}')
    if not res.smds:
        raise MDLError("The model has no visual mesh")

    phy = read(stem_rel + ".phy")
    collision = ""
    if phy:
        try:
            solids, text = read_phy(phy)
            if solids and any(s.pieces for s in solids):
                pname = f"{stem}_physics.smd"
                write_phys_smd(os.path.join(out_dir, pname), mdl, solids, _phy_solid_bones(text, mdl.bones))
                res.physics = pname
                kw = "$collisionjoints" if len(solids) > 1 else "$collisionmodel"
                collision = f'{kw} "{pname}"\n{{\n\t$concave\n}}'
        except (MDLError, struct.error, ValueError, IndexError):
            res.physics = None

    modelname = mdl.name.replace("\\", "/").strip() or (mdl_rel[len("models/"):] if mdl_rel.startswith("models/") else mdl_rel)
    q = ["// Decompiled by CS2 Porter", "", f'$modelname "{modelname}"']
    if mdl.is_static:
        q.append("$staticprop")
    q.append("")
    q += body_lines
    q.append("")
    if mdl.surfaceprop:
        q.append(f'$surfaceprop "{mdl.surfaceprop}"')
    for cd in mdl.cdmaterials:
        q.append(f'$cdmaterials "{cd}"')
    if len(mdl.skins) > 1:
        cols = [c for c in range(len(mdl.skins[0])) if len({row[c] for row in mdl.skins if c < len(row)}) > 1]
        if cols:
            rows = []
            for row in mdl.skins:
                names = " ".join(f'"{mdl.textures[row[c]]}"' for c in cols if 0 <= row[c] < len(mdl.textures))
                rows.append(f"\t{{ {names} }}")
            q.append('$texturegroup "skinfamilies"\n{\n' + "\n".join(rows) + "\n}")
    if collision:
        q.append("")
        q.append(collision)
    res.qc = os.path.join(out_dir, f"{stem}.qc")
    with open(res.qc, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(q) + "\n")
    return res
