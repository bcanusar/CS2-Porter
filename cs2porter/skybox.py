"""Source 1 skybox (6 faces) -> CS2 cube cross + skybox.vmat / skybox_moondome.vmat.

Output layout (3072x4096, 1024 px faces, transparent around the cross):
    [ -  up  - ]
    [ lf ft  rt]
    [ -  dn  - ]
    [ -  bk  - ]   (back face turned 180 degrees)

Source skyboxes do not always follow the same face order and up/down orientation, so the
order of the four side faces and the rotation of up / down are picked by comparing the
pixels along every shared edge: the arrangement with the smallest seams wins.
"""

import itertools
import os

import numpy as np
from PIL import Image

from . import vtf as vtfmod
from .i18n import t
from .materials import parse_vmt, norm_tex

FACE_SIZE = 1024
SIDES = ("bk", "rt", "ft", "lf")          # Source default around the horizon: left, front, right, back
FACES = SIDES + ("up", "dn")
SKY_VMAT = "materials/skybox/skybox.vmat"
MOONDOME_VMAT = "materials/skybox/skybox_moondome.vmat"
SKY_PNG = "materials/skybox/skybox.png"

SKY_TEMPLATE = """// THIS FILE IS AUTO-GENERATED

Layer0
{
	shader "sky.vfx"

	//---- Texture ----
	g_flBrightnessExposureBias "0.000"
	g_flRenderOnlyExposureBias "0.000"
	SkyTexture "%(png)s"


	VariableState
	{
		"Texture"
		{
		}
	}
}
"""

MOONDOME_TEMPLATE = """// THIS FILE IS AUTO-GENERATED

Layer0
{
	shader "csgo_moondome.vfx"

	//---- Shadows ----
	F_DO_NOT_CAST_SHADOWS 1

	//---- Color ----
	g_flTexCoordRotation "0.000"
	g_nScaleTexCoordUByModelScaleAxis "0" // None
	g_nScaleTexCoordVByModelScaleAxis "0" // None
	g_vColorTint "[1.000000 1.000000 1.000000 0.000000]"
	g_vTexCoordCenter "[0.500 0.500]"
	g_vTexCoordOffset "[0.000 0.000]"
	g_vTexCoordScale "[1.000 1.000]"
	g_vTexCoordScrollSpeed "[0.000 0.000]"
	TextureColor "[1.000000 1.000000 1.000000 0.000000]"

	//---- CubeParallax ----
	g_flCubeParallax "0.000"

	//---- Fog ----
	g_bFogEnabled "1"

	//---- Texture ----
	TextureCubeMap "%(png)s"

	//---- Texture Address Mode ----
	g_nTextureAddressModeU "0" // Wrap
	g_nTextureAddressModeV "0" // Wrap
}
"""


# Source skybox faces (Quake's st_to_vec): world direction of a face pixel from s (left -1 ..
# right 1) and t (bottom -1 .. top 1); each entry gives x, y, z as +-(1: s, 2: t, 3: 1)
_SKY_AXES = {"rt": (3, -1, 2), "lf": (-3, 1, 2), "bk": (1, 3, 2), "ft": (-1, -3, 2),
             "up": (-2, -1, 3), "dn": (2, -1, -3)}


def cubemap_to_sky(cube):
    """Faces of a cubemap texture ({"rt": +X, "lf": -X, "bk": +Y, "ft": -Y, "up": +Z, "dn": -Z}
    in the cube texture's own layout) -> the six faces of a Source skybox showing the same sky."""
    size = max(img.width for img in cube.values())
    arrs = [np.asarray(cube[n].convert("RGB").resize((size, size), Image.LANCZOS))
            for n in ("rt", "lf", "bk", "ft", "up", "dn")]
    c = (np.arange(size, dtype=np.float32) + 0.5) / size * 2.0 - 1.0
    s, t = np.meshgrid(c, -c)
    b = (s, t, np.ones_like(s))
    out = {}
    for name, axes in _SKY_AXES.items():
        x, y, z = (b[k - 1] if k > 0 else -b[-k - 1] for k in axes)
        ax, ay, az = np.abs(x), np.abs(y), np.abs(z)
        face = np.where((ax >= ay) & (ax >= az), np.where(x > 0, 0, 1),
                        np.where(ay >= az, np.where(y > 0, 2, 3), np.where(z > 0, 4, 5)))
        ma = np.maximum(np.maximum(ax, ay), az)
        # Direct3D cube face coordinates (sc, tc) of every face
        sc = np.choose(face, [-z, z, x, x, x, -x])
        tc = np.choose(face, [-y, -y, z, -z, -y, -y])
        u = np.clip(((sc / ma + 1) * 0.5 * size).astype(np.int32), 0, size - 1)
        v = np.clip(((tc / ma + 1) * 0.5 * size).astype(np.int32), 0, size - 1)
        img = np.zeros((size, size, 3), dtype=np.uint8)
        for f in range(6):
            m = face == f
            img[m] = arrs[f][v[m], u[m]]
        out[name] = Image.fromarray(img, "RGB")
    return out


def _cube_faces(sources, vmt, vtfcmd):
    """Sky faces of a face material that shows a cubemap ($envmap) instead of a texture."""
    env = vmt.get("$envmap")
    if not env or env.strip().lower() == "env_cubemap":
        return None
    src, rel, ext = sources.find_texture(norm_tex(env))
    if src is None or ext != ".vtf":
        return None
    try:
        cube = vtfmod.load_vtf_faces(src.read(rel))
    except Exception:  # noqa: BLE001
        return None
    return cubemap_to_sky(cube) if cube else None


def _load_face(sources, skyname, face, vtfcmd, cube_cache=None):
    vmt_rel = f"materials/skybox/{skyname}{face}.vmt"
    tex = None
    data, _src = sources.read(vmt_rel)
    if data:
        try:
            vmt = parse_vmt(data, vmt_rel, lambda r: sources.read(r)[0])
            tex = vmt.get("$basetexture") or vmt.get("$hdrbasetexture") or vmt.get("$hdrcompressedtexture")
            if not tex and cube_cache is not None:
                # the face shows a cubemap (WindowImposter sky): every face comes from it
                key = (vmt.get("$envmap") or "").lower()
                if key not in cube_cache:
                    cube_cache[key] = _cube_faces(sources, vmt, vtfcmd)
                if cube_cache[key]:
                    return cube_cache[key][face]
        except Exception:  # noqa: BLE001
            tex = None
    cands = [norm_tex(tex)] if tex else []
    cands.append(f"skybox/{skyname}{face}")
    for c in cands:
        src, rel, ext = sources.find_texture(c)
        if src is None:
            continue
        try:
            img = vtfmod.image_from_bytes(src.read(rel), ext, vtfcmd)
            return img.convert("RGB")
        except Exception:  # noqa: BLE001
            continue
    return None


# ---------------------------------------------------------------------------
# Edge matching
# ---------------------------------------------------------------------------

def _arr(img, size=128):
    return np.asarray(img.resize((size, size), Image.BILINEAR), dtype=np.float32)


def _edge(a, side):
    """Pixels along one edge (2 px deep, averaged): top / bottom left->right, left / right top->bottom."""
    if side == "top":
        return a[:2].mean(axis=0)
    if side == "bottom":
        return a[-2:].mean(axis=0)
    if side == "left":
        return a[:, :2].mean(axis=1)
    return a[:, -2:].mean(axis=1)


def _diff(e1, e2):
    return float(np.abs(e1 - e2).mean())


def _ring_cost(ring, arrs):
    return sum(_diff(_edge(arrs[a], "right"), _edge(arrs[b], "left"))
               for a, b in zip(ring, ring[1:] + ring[:1]))


def _up_cost(u, ring, arrs):
    left, front, right, back = (arrs[f] for f in ring)
    return (_diff(_edge(u, "bottom"), _edge(front, "top"))
            + _diff(_edge(u, "left"), _edge(left, "top"))
            + _diff(_edge(u, "right"), _edge(right, "top")[::-1])
            + _diff(_edge(u, "top"), _edge(back, "top")[::-1]))


def _down_cost(d, ring, arrs):
    left, front, right, back = (arrs[f] for f in ring)
    return (_diff(_edge(d, "top"), _edge(front, "bottom"))
            + _diff(_edge(d, "left"), _edge(left, "bottom")[::-1])
            + _diff(_edge(d, "right"), _edge(right, "bottom"))
            + _diff(_edge(d, "bottom"), _edge(back, "bottom")[::-1]))


def _best(costs, default, tolerance=1.03):
    """Key with the lowest cost; the default wins when it is about as good."""
    best = min(costs, key=costs.get)
    if default in costs and costs[default] <= costs[best] * tolerance + 0.5:
        return default
    return best


def arrange(faces):
    """faces: {name: PIL image}. Returns (ring (left, front, right, back), up_rot, down_rot);
    rotations are counter-clockwise quarter turns."""
    ring = SIDES
    if all(f in faces for f in SIDES):
        arrs = {f: _arr(faces[f]) for f in SIDES}
        costs = {}
        for perm in itertools.permutations(SIDES):
            # equal rings that only start at another face: keep the one with rt in front
            k = perm.index("rt")
            perm = perm[(k - 1) % 4:] + perm[:(k - 1) % 4]
            costs.setdefault(perm, _ring_cost(perm, arrs))
        ring = _best(costs, SIDES)
    else:
        arrs = {f: _arr(faces[f]) for f in SIDES if f in faces}
    up_rot = dn_rot = 0
    if len(arrs) == 4:
        if "up" in faces:
            u = _arr(faces["up"])
            up_rot = _best({k: _up_cost(np.rot90(u, k), ring, arrs) for k in range(4)}, 0)
        if "dn" in faces:
            d = _arr(faces["dn"])
            dn_rot = _best({k: _down_cost(np.rot90(d, k), ring, arrs) for k in range(4)}, 0)
    return ring, up_rot, dn_rot


def _rot(img, quarter_turns):
    return img.rotate(90 * quarter_turns, expand=True) if quarter_turns else img


def compose(faces, ring, up_rot, dn_rot, size=FACE_SIZE):
    cube = Image.new("RGBA", (size * 3, size * 4), (0, 0, 0, 0))
    left, front, right, back = ring

    def put(name, gx, gy, turns=0):
        if name in faces:
            img = _rot(faces[name], turns).resize((size, size), Image.LANCZOS).convert("RGBA")
            cube.paste(img, (gx * size, gy * size))

    put("up", 1, 0, up_rot)
    put(left, 0, 1)
    put(front, 1, 1)
    put(right, 2, 1)
    put("dn", 1, 2, dn_rot)
    put(back, 1, 3, 2)
    return cube


def build_skybox(sources, content_dir, skyname, vtfcmd=None, overwrite=False, log=None):
    """Writes skybox.png, skybox.vmat and skybox_moondome.vmat. Returns (status, detail)."""
    log = log or (lambda m, tag="info": None)
    sky = skyname.strip().lower()
    if not sky:
        return "skip", t("sk_no_sky")
    out_dir = os.path.join(content_dir, "materials", "skybox")
    vmats = [os.path.join(content_dir, *p.split("/")) for p in (SKY_VMAT, MOONDOME_VMAT, SKY_PNG)]
    if not overwrite and all(os.path.isfile(p) for p in vmats):
        return "exists", t("m_exists")

    faces = {}
    cube_cache = {}
    for f in FACES:
        img = _load_face(sources, sky, f, vtfcmd, cube_cache)
        if img is not None:
            faces[f] = img
    if not any(f in faces for f in SIDES):
        return "missing", t("sk_missing", sky=sky)

    ring, up_rot, dn_rot = arrange(faces)
    log(t("sk_fit", order=" ".join(ring), up=up_rot * 90, dn=dn_rot * 90), "dim")
    os.makedirs(out_dir, exist_ok=True)
    compose(faces, ring, up_rot, dn_rot).save(os.path.join(content_dir, *SKY_PNG.split("/")), "PNG")
    with open(vmats[0], "w", encoding="utf-8", newline="\n") as fh:
        fh.write(SKY_TEMPLATE % {"png": SKY_PNG})
    with open(vmats[1], "w", encoding="utf-8", newline="\n") as fh:
        fh.write(MOONDOME_TEMPLATE % {"png": SKY_PNG})
    missing = [f for f in FACES if f not in faces]
    detail = t("sk_faces", n=len(faces)) + (t("sk_missing_faces", list=", ".join(missing)) if missing else "")
    return "created", detail
