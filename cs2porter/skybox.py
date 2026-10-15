"""Source 1 skybox (6 faces) -> CS2 cube cross + skybox.vmat / skybox_occluder.vmat.

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
import re

import numpy as np
from PIL import Image

from . import vtf as vtfmod
from .i18n import t
from .materials import parse_vmt, norm_tex

FACE_SIZE = 1024
SIDES = ("bk", "rt", "ft", "lf")          # Source default around the horizon: left, front, right, back
FACES = SIDES + ("up", "dn")
SKY_VMAT = "materials/skybox/skybox.vmat"
# the sky brushes: draws the map's sky (env_sky and the 3D skybox) and hides what is behind it.
# It shows no texture, so every map of an addon uses the same file.
OCCLUDER_VMAT = "materials/skybox/skybox_occluder.vmat"
SKY_PNG = "materials/skybox/skybox.png"


def sky_paths(map_name=""):
    """(sky vmat, occluder vmat, png) of a map. With a map name the sky files get it as a suffix
    (skybox_<map>.vmat ...), so maps sharing an addon keep their own skies."""
    if not map_name:
        return SKY_VMAT, OCCLUDER_VMAT, SKY_PNG
    sfx = "_" + re.sub(r"[^a-z0-9_]+", "_", map_name.lower()).strip("_")
    return f"materials/skybox/skybox{sfx}.vmat", OCCLUDER_VMAT, f"materials/skybox/skybox{sfx}.png"

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

OCCLUDER_TEMPLATE = """// THIS FILE IS AUTO-GENERATED

Layer0
{
	shader "sky_occluder.vfx"

	//---- Color ----
	TextureColor "[1.000000 1.000000 1.000000 0.000000]"


	VariableState
	{
		"Color"
		{
		}
	}
}
"""

# a surface that shows a cubemap of its own (materials.py, WindowImposter)
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


# ---------------------------------------------------------------------------
# env_sun: CS2 has no sun sprite, so the sun is drawn into the sky texture
# ---------------------------------------------------------------------------

SUN_MATERIAL = "sprites/light_glow02_add_noz"
# Source draws a sun sprite as a square facing the camera, this far away in the sun's direction,
# that reaches its size (in units) to each side (CGlowOverlay::CalcBasis)
_SUN_DIST = 100.0
# the glow overlay (C_SunGlowOverlay, modulated by the view): seen straight on
_GLOW_SIZE, _GLOW_ALPHA = 6.0, 0.75


def _num(text, default):
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _color(text, default=(255, 255, 255)):
    parts = (text or "").split()
    try:
        vals = [float(v) for v in parts[:3]]
    except ValueError:
        vals = []
    return tuple(max(v, 0.0) / 255.0 for v in (vals if len(vals) == 3 else default))


def _vec3(text):
    parts = (text or "").split()
    try:
        return np.asarray([float(v) for v in parts[:3]], dtype=np.float64) if len(parts) >= 3 else None
    except ValueError:
        return None


def sun_direction(ent, by_name):
    """Direction towards the sun of an env_sun ({key: value}, lower case keys), as CSun::Activate
    sets it: with use_angles from its angles / angle / pitch (SetupLightNormalFromProps, turned
    around), else from its target to itself, else straight up."""
    if _num(ent.get("use_angles"), 0) == 0:
        tgt = by_name.get((ent.get("target") or "").lower())
        a, b = _vec3(ent.get("origin")), _vec3((tgt or {}).get("origin"))
        if a is not None and b is not None and np.linalg.norm(a - b) > 1e-3:
            return (a - b) / np.linalg.norm(a - b)
        return np.asarray([0.0, 0.0, 1.0])
    angles = _vec3(ent.get("angles")) if ent.get("angles") else np.zeros(3)
    if angles is None:
        angles = np.zeros(3)
    angle = _num(ent.get("angle"), 0.0)
    if angle == -1:
        d = np.asarray([0.0, 0.0, 1.0])
    elif angle == -2:
        d = np.asarray([0.0, 0.0, -1.0])
    else:
        y = np.radians(angle or angles[1])
        d = np.asarray([np.cos(y), np.sin(y), 0.0])
    p = np.radians(_num(ent.get("pitch"), 0.0) or angles[0])
    d = np.asarray([d[0] * np.cos(p), d[1] * np.cos(p), np.sin(p)])
    if np.linalg.norm(d) < 1e-6:
        return np.asarray([0.0, 0.0, 1.0])
    # that is the direction the light goes; the sun is the other way
    return -d / np.linalg.norm(d)


def suns_of(entities):
    """The env_sun entities of a map ([{key: value}]) as [{dir, size, layers: [(material, half size, color)]}]."""
    by_name = {e["targetname"].lower(): e for e in entities if e.get("targetname")}
    out = []
    for e in entities:
        if e.get("classname", "").lower() != "env_sun":
            continue
        size = _num(e.get("size"), 16.0)
        layers = []
        # C_Sun: the core keeps the hue of its color at full strength
        main = _color(e.get("rendercolor"))
        main = tuple(c / max(main) for c in main) if max(main) > 0 else (1.0, 1.0, 1.0)
        if size > 0:
            layers.append((e.get("material") or SUN_MATERIAL, size, main))
        # the glow around it is always there (overlaycolor 0 0 0: the core's color); looked at,
        # C_SunGlowOverlay draws it 6 times the size at 0.75
        over = _color(e.get("overlaycolor"), (0, 0, 0))
        over = over if any(c > 0 for c in over) else main
        osize = _num(e.get("overlaysize"), -1.0)
        osize = size if osize == -1 else osize
        if osize > 0:
            layers.append((e.get("overlaymaterial") or SUN_MATERIAL, osize * _GLOW_SIZE,
                           tuple(c * _GLOW_ALPHA for c in over)))
        if layers:
            out.append({"dir": sun_direction(e, by_name), "layers": layers, "size": size})
    return out


def _glow_fallback(size=128):
    """A soft round glow (light_glow02 like) when the sun's texture is not found."""
    c = (np.arange(size, dtype=np.float32) + 0.5) / size * 2.0 - 1.0
    r = np.hypot(*np.meshgrid(c, c))
    g = np.clip(1.0 - r, 0.0, 1.0)
    return np.repeat((g ** 3 * 0.75 + np.clip(1.0 - r * 6.0, 0.0, 1.0) * 0.25)[..., None], 3, axis=2)


def _sprite_texture(sources, material, vtfcmd):
    """The sprite's texture as an RGB float array (alpha multiplied in), or a stand-in glow."""
    mat = norm_tex(material)
    if mat.endswith((".vmt", ".spr")):
        mat = mat[:-4]
    tex = None
    rel = f"materials/{mat}.vmt"
    data, _src = sources.read(rel)
    if data:
        try:
            tex = parse_vmt(data, rel, lambda r: sources.read(r)[0]).get("$basetexture")
        except Exception:  # noqa: BLE001
            tex = None
    for cand in ([norm_tex(tex)] if tex else []) + [mat]:
        src, trel, ext = sources.find_texture(cand)
        if src is None:
            continue
        try:
            img = vtfmod.image_from_bytes(src.read(trel), ext, vtfcmd)
        except Exception:  # noqa: BLE001
            continue
        rgba = np.asarray(img.convert("RGBA"), dtype=np.float32) / 255.0
        if img.mode in ("RGBA", "LA", "PA"):
            return rgba[..., :3] * rgba[..., 3:4]
        return rgba[..., :3]
    return None


def _bilinear(tex, u, v):
    """tex sampled at u, v (0..1, left / top)."""
    h, w = tex.shape[:2]
    x = np.clip(u * w - 0.5, 0, w - 1)
    y = np.clip(v * h - 0.5, 0, h - 1)
    x0, y0 = np.floor(x).astype(np.int32), np.floor(y).astype(np.int32)
    x1, y1 = np.minimum(x0 + 1, w - 1), np.minimum(y0 + 1, h - 1)
    fx, fy = (x - x0)[..., None], (y - y0)[..., None]
    top = tex[y0, x0] * (1 - fx) + tex[y0, x1] * fx
    bot = tex[y1, x0] * (1 - fx) + tex[y1, x1] * fx
    return top * (1 - fy) + bot * fy


def _angle_matrix(pitch, yaw, roll):
    """Source's AngleMatrix: columns forward, left, up of the angles (degrees)."""
    p, y, r = np.radians([pitch, yaw, roll])
    sp, cp, sy, cy, sr, cr = np.sin(p), np.cos(p), np.sin(y), np.cos(y), np.sin(r), np.cos(r)
    return np.asarray([[cp * cy, sr * sp * cy - cr * sy, cr * sp * cy + sr * sy],
                       [cp * sy, sr * sp * sy + cr * cy, cr * sp * sy - sr * cy],
                       [-sp, sr * cp, cr * cp]])


def sky_to_faces(sky_angles=None):
    """Turns a CS2 world direction into the direction of the sky faces (Source face layout,
    _SKY_AXES) that the CS2 sky shows there. CS2 shows the middle of the cross (Source's +X
    face, rt) towards +Y, and the env_sky's angles turn the whole sky."""
    quarter = np.asarray([[0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])   # yaw -90
    return quarter @ _angle_matrix(*(sky_angles or (0.0, 0.0, 0.0))).T


def draw_suns(faces, suns, sources, vtfcmd=None, size=FACE_SIZE, sky_angles=None):
    """Adds the suns to the sky faces ({name: image}, Source face layout) where the CS2 map
    (env_sky angles sky_angles) shows their direction, the way Source draws them over the
    sky: an additive sprite. Returns the faces (square, size px)."""
    to_faces = sky_to_faces(sky_angles)
    out = {n: img.convert("RGB").resize((size, size), Image.LANCZOS) for n, img in faces.items()}
    textures = {}
    c = (np.arange(size, dtype=np.float64) + 0.5) / size * 2.0 - 1.0
    s, t = np.meshgrid(c, -c)
    b = (s, t, np.ones_like(s))
    for name in list(out):
        x, y, z = (b[k - 1] if k > 0 else -b[-k - 1] for k in _SKY_AXES[name])
        p = np.stack([x, y, z], axis=-1)
        p /= np.linalg.norm(p, axis=-1, keepdims=True)
        add = np.zeros((size, size, 3), dtype=np.float64)
        for sun in suns:
            d = sun["dir"]
            right = np.cross(d, (0.0, 0.0, 1.0))
            if np.linalg.norm(right) < 1e-6:
                right = np.asarray([0.0, 1.0, 0.0])
            right /= np.linalg.norm(right)
            up = np.cross(right, d)
            up /= np.linalg.norm(up)
            # the sprite as the CS2 map sees it, in the faces' own directions
            d, right, up = to_faces @ d, to_faces @ right, to_faces @ up
            dot = p @ d
            front = dot > 1e-3
            if not front.any():
                continue
            # where each pixel's view ray meets the sprite's plane
            hit = p * (_SUN_DIST / np.where(front, dot, 1.0))[..., None] - d * _SUN_DIST
            ru, rv = hit @ right, hit @ up
            for material, half, color in sun["layers"]:
                u, v = ru / half, rv / half
                inside = front & (np.abs(u) < 1) & (np.abs(v) < 1)
                if not inside.any():
                    continue
                if material not in textures:
                    found = _sprite_texture(sources, material, vtfcmd)
                    textures[material] = found if found is not None else _glow_fallback()
                tex = textures[material]
                val = _bilinear(tex, (u[inside] + 1) * 0.5, (1 - v[inside]) * 0.5)
                add[inside] += val * np.asarray(color)
        if add.any():
            base = np.asarray(out[name], dtype=np.float64) / 255.0
            out[name] = Image.fromarray((np.clip(base + add, 0, 1) * 255 + 0.5).astype(np.uint8), "RGB")
    return out


def build_skybox(sources, content_dir, skyname, vtfcmd=None, overwrite=False, log=None, occluder=True,
                 suns=None, sky_angles=None, paths=None):
    """Writes skybox.png, skybox.vmat and (occluder) skybox_occluder.vmat, or the names in paths
    (see sky_paths). Returns (status, detail).
    suns: the map's env_sun entities (see suns_of), drawn into the sky where the CS2 map's
    env_sky (angles sky_angles, None: 0 0 0) shows their direction."""
    log = log or (lambda m, tag="info": None)
    sky = skyname.strip().lower()
    if not sky:
        return "skip", t("sk_no_sky")
    out_dir = os.path.join(content_dir, "materials", "skybox")
    sky_png = (paths or sky_paths())[2]
    vmats = [os.path.join(content_dir, *p.split("/")) for p in (paths or sky_paths())]
    needed = vmats if occluder else (vmats[0], vmats[2])
    if not overwrite and all(os.path.isfile(p) for p in needed):
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
    if suns:
        # after the arranging: the faces keep their Source names, which the sun is drawn by
        faces = draw_suns(faces, suns, sources, vtfcmd, sky_angles=sky_angles)
        for sun in suns:
            d = sun["dir"]
            log(t("sk_sun", pitch=f"{np.degrees(np.arcsin(np.clip(d[2], -1, 1))):.0f}",
                  yaw=f"{np.degrees(np.arctan2(d[1], d[0])):.0f}",
                  size=f"{sun['size']:g}"), "dim")
    os.makedirs(out_dir, exist_ok=True)
    compose(faces, ring, up_rot, dn_rot).save(vmats[2], "PNG")
    with open(vmats[0], "w", encoding="utf-8", newline="\n") as fh:
        fh.write(SKY_TEMPLATE % {"png": sky_png})
    if occluder:
        with open(vmats[1], "w", encoding="utf-8", newline="\n") as fh:
            fh.write(OCCLUDER_TEMPLATE)
    missing = [f for f in FACES if f not in faces]
    detail = t("sk_faces", n=len(faces)) + (t("sk_missing_faces", list=", ".join(missing)) if missing else "")
    if suns:
        detail += t("sk_with_sun")
    return "created", detail
