"""Converts Source 1 materials (VMT + VTF/TGA) into CS2 .vmat files.

The templates and the translucency / surface detection come from tehlikeli91's
vmat_converter.py and fbx_converter.py scripts.
"""

import math
import os
import re
import shutil

import numpy as np
from PIL import Image

from . import kv
from . import texlights
from . import vtf as vtfmod
from .i18n import t
from .legacy import vmat_converter as VC
from .legacy.fbx_converter import build_vmat_foliage

VALID_SURFACES = {
    "glass", "plaster", "concrete", "brick", "metal",
    "Wood", "tile", "rock", "sand", "dirt", "grass",
    "water", "snow", "mud", "plastic", "rubber", "carpet",
    "Flesh", "gravel", "cardboard", "foliage",
}

# Source 1 $surfaceprop -> CS2 surface (substring match, order matters)
_SURFACE_ALIASES = [
    ("glass", "glass"), ("window", "glass"),
    ("plaster", "plaster"), ("drywall", "plaster"),
    ("concrete", "concrete"), ("cement", "concrete"),
    ("brick", "brick"),
    ("gravel", "gravel"),
    ("metal", "metal"), ("chain", "metal"), ("canister", "metal"), ("grate", "metal"),
    ("popcan", "metal"), ("paintcan", "metal"), ("vent", "metal"), ("roller", "metal"),
    ("weapon", "metal"), ("ladder", "metal"), ("pipe", "metal"), ("combine", "metal"),
    ("wood", "Wood"), ("crate", "Wood"),
    ("tile", "tile"), ("ceramic", "tile"), ("porcelain", "tile"),
    ("rock", "rock"), ("stone", "rock"), ("boulder", "rock"), ("cobble", "rock"),
    ("sand", "sand"),
    ("mud", "mud"),
    ("dirt", "dirt"), ("soil", "dirt"),
    ("grass", "grass"),
    ("slime", "water"), ("water", "water"), ("wade", "water"),
    ("snow", "snow"), ("ice", "snow"),
    ("plastic", "plastic"), ("computer", "plastic"),
    ("rubber", "rubber"), ("tire", "rubber"),
    ("carpet", "carpet"), ("cloth", "carpet"), ("fabric", "carpet"), ("upholstery", "carpet"),
    ("flesh", "Flesh"), ("alien", "Flesh"),
    ("cardboard", "cardboard"), ("paper", "cardboard"),
    ("foliage", "foliage"), ("leaves", "foliage"),
]

_TRUE_VALUES = {"1", "1.0", "true", "yes"}
_LIT_SHADERS = {"lightmappedgeneric", "vertexlitgeneric", "lightmappedgeneric_dx9",
                "vertexlitgeneric_dx9", "lightmapped_4wayblend", "worldtwotextureblend",
                "lightmappedreflective", "customcharacter", "character", "lightmappedtwotexture"}
_UNLIT_SHADERS = {"unlitgeneric", "unlittwotexture", "sprite", "spritecard", "modulate",
                  "decalmodulate_unlit", "monitorscreen", "wireframe", "screenspace_general"}
_DECAL_SHADERS = {"decalmodulate", "decalbasetimeslightmapalphablendselfillum"}
_BLEND_SHADERS = {"worldvertextransition", "lightmapped_4wayblend", "worldtwotextureblend"}
_FOLIAGE_KW = ("foliage", "leaf", "leaves", "tree", "bush", "shrub", "grass", "fern",
               "plant", "ivy", "branch", "flower", "hedge", "vine", "pine", "palm")
_IMAGE_COPY_EXTS = {".tga", ".png", ".jpg", ".jpeg", ".psd", ".tif", ".tiff"}

DEFAULT_COLOR = "materials/default/default_color.tga"
DEFAULT_NORMAL = "materials/default/default_normal.tga"
# blend modulation texture for blend materials without $blendmodulatetexture: red (range) and
# green (middle) at 0.5, so the second layer fades in over the whole paint range like in Source 1
SOFT_BLEND_TEX = "materials/cs2porter/blendmodulate_soft.tga"
BLEND_SOFTNESS = "0.500"
# $selfillum materials: csgo_complex with self illumination (the layout of a material saved by
# the material editor); the mask is $selfillummask, the base texture's alpha, or white.
# CS2 glows with texture x tint x 2^brightness (Self Illum Brightness counts in doublings, 0 =
# the texture at full brightness). A Source 1 $selfillum surface shows its texture times
# $selfillumtint at full brightness, so a tint over 1 goes into the brightness. Texture lights
# (texlights.py) glow as bright as the light they gave in Source 1, in their light's color.
TMPL_SELF_ILLUM = """\
// THIS FILE IS AUTO-GENERATED

Layer0
{{
\tshader "csgo_complex.vfx"

\t//---- PBR ----
\tF_SELF_ILLUM 1

\t//---- Ambient Occlusion ----
\tTextureAmbientOcclusion "materials/default/default_ao.tga"

\t//---- Color ----
\tg_flModelTintAmount "1.000"
\tg_flTexCoordRotation "0.000"
\tg_nScaleTexCoordUByModelScaleAxis "0" // None
\tg_nScaleTexCoordVByModelScaleAxis "0" // None
\tg_vColorTint "[1.000000 1.000000 1.000000 0.000000]"
\tg_vTexCoordCenter "[0.500 0.500]"
\tg_vTexCoordOffset "[0.000 0.000]"
\tg_vTexCoordScale "[1.000 1.000]"
\tg_vTexCoordScrollSpeed "[0.000 0.000]"
\tTextureColor "{color}"

\t//---- Fog ----
\tg_bFogEnabled "1"

\t//---- Lighting ----
\tg_flMetalness "0.000"
\tTextureRoughness "[1.000000 1.000000 1.000000 0.000000]"

\t//---- Normal Map ----
\tTextureNormal "{normal}"

\t//---- Self Illum ----
\tg_flSelfIllumAlbedoFactor "{albedo}"
\tg_flSelfIllumBrightness "{brightness}"
\tg_flSelfIllumScale "1.000"
\tg_vSelfIllumScrollSpeed "[0.000 0.000]"
\tg_vSelfIllumTint "{tint}"
\tTextureSelfIllumMask "{mask}"

\t//---- Texture Address Mode ----
\tg_nTextureAddressModeU "0" // Wrap
\tg_nTextureAddressModeV "0" // Wrap

{sysattr}\tVariableState
\t{{
\t\t"Ambient Occlusion"
\t\t{{
\t\t}}
\t\t"Color"
\t\t{{
\t\t}}
\t\t"Fog"
\t\t{{
\t\t}}
\t\t"Lighting"
\t\t{{
\t\t\t"Roughness" 0
\t\t\t"Metalness" 0
\t\t}}
\t\t"Normal Map"
\t\t{{
\t\t}}
\t\t"Self Illum"
\t\t{{
\t\t}}
\t\t"Texture Address Mode"
\t\t{{
\t\t}}
\t}}
}}
"""
WHITE_MASK = "[1.000000 1.000000 1.000000 0.000000]"
ROUGH_FULL = "[1.000000 1.000000 1.000000 0.000000]"

# animated textures (AnimatedTexture proxy): every frame goes into one sheet, played by
# csgo_complex's texture animation (the layout hand made animated materials use)
SHEET_MAX = 4096
DEFAULT_FRAME_RATE = 15.0
# csgo_water: below this the water fog starts right at the surface and the water looks murky
WATER_START_MIN = 15.0
# csgo_water reflects white (the light probe cubemaps are baked in white rooms) as soon as the
# reflectance goes up or Fresnel is on: it is kept in 0..0.03 with Fresnel off
WATER_REFLECTANCE = 0.02
# Refract materials with a scrolling normal map: csgo_refract has no scroll, so they become a
# translucent, shiny, scrolling csgo_complex surface tinted with $refracttint
REFRACT_OPACITY = 0.35
REFRACT_ROUGHNESS = "[0.150000 0.150000 0.150000 0.000000]"
# csgo_refract with its default white tints draws far too bright. A working glass (blur, the
# same normal map as second normal, very dark tints) has these values; the tint texture keeps
# the hue of $refracttint at this brightness.
REFRACT_COLOR_TINT = 0.05098
REFRACT_TINT_PEAK = 0.133333
REFRACT_TINT_DEFAULT = (0.043137, 0.066667, 0.133333)
REFRACT_SCALE = 0.03


def refract_vmat(normal, refract_tint, sysattr):
    """csgo_refract material text: blurred, both normal maps the same, dark tints."""
    raw = (refract_tint or "").strip()
    tint = (_floats(raw, 1 / 255.0 if "{" in raw else 1.0) + [1.0, 1.0, 1.0])[:3] if raw else None
    peak = max(tint) if tint else 0.0
    if peak <= 1e-6 or min(tint) >= peak * 0.98:
        # white or grey tint (or none): the cool dark glass color
        dark = REFRACT_TINT_DEFAULT
    else:
        dark = tuple(max(c, 0.0) / peak * REFRACT_TINT_PEAK for c in tint)
    c = REFRACT_COLOR_TINT
    return ("// THIS FILE IS AUTO-GENERATED\n\nLayer0\n{\n\tshader \"csgo_refract.vfx\"\n\n"
            "\t//---- Features ----\n\tF_BLUR 1\n\tF_SECONDARY_NORMAL 1\n\tF_TINT_TEXTURE 1\n\n"
            f"\t//---- Color ----\n\tg_vColorTint \"[{c:.6f} {c:.6f} {c:.6f} 0.000000]\"\n\n"
            f"\t//---- Normal Map ----\n\tTextureNormal \"{normal}\"\n\tTextureNormal2 \"{normal}\"\n\n"
            f"\t//---- Refraction ----\n\tg_flRefractScale \"{REFRACT_SCALE:.3f}\"\n"
            "\tg_vSecondaryTexCoordScale \"[1.000 1.000]\"\n\n"
            f"\t//---- Tinting ----\n\tTextureTintTexture \"[{dark[0]:.6f} {dark[1]:.6f} {dark[2]:.6f} 0.000000]\"\n\n"
            f"{sysattr}}}\n")


def complex_vmat(color, normal, sysattr, translucency=None, opacity="1.000", scroll=None, anim=None,
                 roughness=ROUGH_FULL, tint=None):
    """csgo_complex material text (the material editor's layout).
    scroll: (u, v) per second; anim: (cells, columns, rows, seconds per frame)."""
    T = "\t"
    feats = []
    if translucency:
        feats.append(f"{T}F_TRANSLUCENT 1")
    if anim:
        feats.append(f"{T}F_TEXTURE_ANIMATION 1")
    su, sv = (round(c, 3) + 0.0 for c in (scroll or (0.0, 0.0)))     # + 0.0: no "-0.000"
    tint = tint or [1.0, 1.0, 1.0]
    lines = ["// THIS FILE IS AUTO-GENERATED", "", "Layer0", "{", f'{T}shader "csgo_complex.vfx"', ""]
    if feats:
        lines += [f"{T}//---- Features ----"] + feats + [""]
    lines += [
        f"{T}//---- Ambient Occlusion ----",
        f'{T}TextureAmbientOcclusion "materials/default/default_ao.tga"', "",
        f"{T}//---- Color ----",
        f'{T}g_flModelTintAmount "1.000"',
        f'{T}g_flTexCoordRotation "0.000"',
        f'{T}g_nScaleTexCoordUByModelScaleAxis "0" // None',
        f'{T}g_nScaleTexCoordVByModelScaleAxis "0" // None',
        f'{T}g_vColorTint "[{tint[0]:.6f} {tint[1]:.6f} {tint[2]:.6f} 0.000000]"',
        f'{T}g_vTexCoordCenter "[0.500 0.500]"',
        f'{T}g_vTexCoordOffset "[0.000 0.000]"',
        f'{T}g_vTexCoordScale "[1.000 1.000]"',
        f'{T}g_vTexCoordScrollSpeed "[{su:.3f} {sv:.3f}]"',
        f'{T}TextureColor "{color}"', "",
    ]
    if anim:
        cells, cols, rows, per_frame = anim
        lines += [
            f"{T}//---- Texture Animation ----",
            f'{T}g_nNumAnimationCells "{cells}"',
            f'{T}g_flAnimationFrame "0"',
            f'{T}g_flAnimationTimeOffset "0"',
            f'{T}g_flAnimationTimePerFrame "{per_frame:.4f}"',
            f'{T}g_vAnimationGrid "[{cols:.6f} {rows:.6f} 0.000000 0.000000]"', "",
        ]
    lines += [
        f"{T}//---- Fog ----", f'{T}g_bFogEnabled "1"', "",
        f"{T}//---- Lighting ----", f'{T}g_flMetalness "0.000"', f'{T}TextureRoughness "{roughness}"', "",
        f"{T}//---- Normal Map ----", f'{T}TextureNormal "{normal}"', "",
    ]
    if translucency:
        lines += [f"{T}//---- Translucent ----", f'{T}g_flOpacityScale "{opacity}"',
                  f'{T}TextureTranslucency "{translucency}"', ""]
    lines += [f"{T}//---- Texture Address Mode ----", f'{T}g_nTextureAddressModeU "0" // Wrap',
              f'{T}g_nTextureAddressModeV "0" // Wrap', ""]
    return "\n".join(lines) + "\n" + sysattr + "}\n"


# csgo_unlitgeneric F_BLEND_MODE values (Opaque, Translucent, Mod2x, Additive, ModThenAdd)
UNLIT_BLEND_TRANSLUCENT = 1
UNLIT_BLEND_ADDITIVE = 3


def unlit_vmat(color, translucency=None, blend=0, tint=None, scroll=None, backfaces=False):
    """csgo_unlitgeneric material text. blend: F_BLEND_MODE (see above); tint: (r, g, b);
    scroll: (u, v) per second; backfaces: drawn from both sides ($nocull)."""
    T = "\t"
    tint = tint or (1.0, 1.0, 1.0)
    su, sv = (round(c, 3) + 0.0 for c in (scroll or (0.0, 0.0)))     # + 0.0: no "-0.000"
    lines = ["// THIS FILE IS AUTO-GENERATED", "", "Layer0", "{", f'{T}shader "csgo_unlitgeneric.vfx"', ""]
    feats = []
    if blend:
        feats.append(f"{T}F_BLEND_MODE {blend}")
    if backfaces:
        feats.append(f"{T}F_RENDER_BACKFACES 1")
    if feats:
        lines += [f"{T}//---- Features ----"] + feats + [""]
    lines += [
        f"{T}//---- Color ----",
        f'{T}g_flModelTintAmount "1.000"',
        f'{T}g_flTexCoordRotation "0.000"',
        f'{T}g_nScaleTexCoordUByModelScaleAxis "0" // None',
        f'{T}g_nScaleTexCoordVByModelScaleAxis "0" // None',
        f'{T}g_vColorTint "[{tint[0]:.6f} {tint[1]:.6f} {tint[2]:.6f} 0.000000]"',
        f'{T}g_vTexCoordCenter "[0.500 0.500]"',
        f'{T}g_vTexCoordOffset "[0.000 0.000]"',
        f'{T}g_vTexCoordScale "[1.000 1.000]"',
        f'{T}g_vTexCoordScrollSpeed "[{su:.3f} {sv:.3f}]"',
        f'{T}TextureColor "{color}"', "",
        f"{T}//---- Fog ----", f'{T}g_bFogEnabled "1"', "",
        f"{T}//---- Texture Address Mode ----", f'{T}g_nTextureAddressModeU "0" // Wrap',
        f'{T}g_nTextureAddressModeV "0" // Wrap', ""]
    if translucency:
        lines += [f"{T}//---- Translucent ----", f'{T}TextureTranslucency "{translucency}"', ""]
    return "\n".join(lines) + "}\n"


def vmt_color(vmt, key="$color"):
    """$color (or another color key) as (r, g, b) with 1 = full, None when not set."""
    raw = (vmt.get(key) or "").strip()
    if not raw:
        return None
    vals = _floats(raw, 1 / 255.0 if "{" in raw else 1.0)
    if len(vals) == 1:
        vals *= 3
    return tuple(max(v, 0.0) for v in vals[:3]) if len(vals) >= 3 else None


def _proxy_writes(vmt, var):
    """Is var the result of one of the material's proxies (then its value in the file is not
    what is shown)?"""
    return any((p.get("resultvar") or "").lower() == var for p in _proxies(vmt))


# Source 1 proxies that compute a value from the player's distance to the surface
_FADE_SAMPLES = 1024
_FADE_STEP = 8.0


def _run_proxies(vmt, distance):
    """Runs the math proxies of a material for a player at this distance; returns the variables."""
    vars_ = {}
    for k, v in vmt.params.items():
        nums = _floats(v)
        if k.startswith("$") and len(nums) == 1 and not re.search(r"[a-z]", str(v).lower().replace("e-", "")):
            vars_[k] = nums[0]

    def val(tok, default=0.0):
        if tok is None:
            return default
        tok = str(tok).strip().lower()
        if tok.startswith("$"):
            return vars_.get(tok, default)
        nums = _floats(tok)
        return nums[0] if nums else default

    for p in _proxies(vmt):
        name = p.name.lower()
        out = (p.get("resultvar") or "").lower()
        a, b = val(p.get("srcvar1")), val(p.get("srcvar2"))
        if name == "playerproximity":
            r = distance * val(p.get("scale"), 1.0)
        elif name == "subtract":
            r = a - b
        elif name == "add":
            r = a + b
        elif name == "multiply":
            r = a * b
        elif name == "divide":
            r = a / b if abs(b) > 1e-9 else 0.0
        elif name == "clamp":
            r = min(max(a, val(p.get("min"), 0.0)), val(p.get("max"), 1.0))
        elif name == "equals":
            r = a
        elif name == "abs":
            r = abs(a)
        elif name == "sine":
            r = (val(p.get("sinemin"), -1.0) + val(p.get("sinemax"), 1.0)) / 2.0
        else:
            continue
        if out:
            vars_[out] = r
    return vars_


def proximity_fade(vmt):
    """A material whose $alpha follows the player's distance (PlayerProximity proxy chain) ->
    (fade start, fade end, alpha near, alpha far), else None."""
    if not any(p.name.lower() == "playerproximity" for p in _proxies(vmt)) or not _proxy_writes(vmt, "$alpha"):
        return None
    alphas = [min(max(_run_proxies(vmt, i * _FADE_STEP).get("$alpha", 1.0), 0.0), 1.0)
              for i in range(_FADE_SAMPLES)]
    near, far = alphas[0], alphas[-1]
    if abs(far - near) < 0.05:
        return None
    start = next(i for i, a in enumerate(alphas) if abs(a - near) > 0.01) * _FADE_STEP
    end = next(i for i, a in enumerate(alphas) if abs(a - far) <= 0.01) * _FADE_STEP
    return start, max(end, start + _FADE_STEP), near, far


def fade_vmat(color, tint, fade):
    """csgo_effects material that fades with the distance to the camera (its Distance Fade
    group): fade = (start, end, opacity near, opacity far)."""
    start, end, near, far = fade
    T = "\t"
    return "\n".join([
        "// THIS FILE IS AUTO-GENERATED", "", "Layer0", "{", f'{T}shader "csgo_effects.vfx"', "",
        f"{T}//---- Features ----", f"{T}F_DO_NOT_CAST_SHADOWS 1", "",
        f"{T}//---- Color ----",
        f'{T}g_vColorTint "[{tint[0]:.6f} {tint[1]:.6f} {tint[2]:.6f} 0.000000]"',
        f'{T}TextureColor "{color}"', "",
        f"{T}//---- Distance Fade ----",
        f'{T}g_flFadeDistance "{start:.1f}"',
        f'{T}g_flFadeFalloff "{end - start:.1f}"',
        f'{T}g_flFadeMin "{near:.3f}"',
        f'{T}g_flFadeMax "{far:.3f}"', "",
        f"{T}//---- Fog ----", f'{T}g_bFogEnabled "1"', "",
        f"{T}//---- Translucent ----", f'{T}g_flOpacityScale "1.000"',
        f'{T}TextureTranslucency "[1.000000 1.000000 1.000000 0.000000]"', "", "}", ""])


def _pow2_up(v):
    p = 1
    while p < v:
        p *= 2
    return p


def _make_pow2(path):
    """Stretches an image file whose sides are not powers of two (CS2 can not make the mip
    maps of a texture with alpha then, and the material fails)."""
    if not path.lower().endswith((".tga", ".png")):
        return
    try:
        with Image.open(path) as img:
            size = tuple(min(_pow2_up(v), SHEET_MAX) for v in img.size)
            if size == img.size:
                return
            img.load()
            fixed = img.resize(size, Image.LANCZOS)
        fixed.save(path)
    except (OSError, ValueError):
        pass


def sheet_grid(n, w, h):
    """(columns, rows) of the sheet for n frames of w x h. The column count is a power of two:
    the shader finds a frame's row by dividing by it, and with 5, 6 or 7 columns that division
    is sometimes rounded down to the row above, so a wrong frame flashes up."""
    best = None
    cols = 1
    while True:
        rows = math.ceil(n / cols)
        key = (max(cols * w, rows * h), cols * rows - n)
        if best is None or key < best[0]:
            best = (key, cols, rows)
        if cols >= n:
            break
        cols *= 2
    return best[1], best[2]


def _proxies(vmt):
    if vmt.block is None:
        return []
    return [p for blk in vmt.block.blocks() if blk.name.lower() == "proxies" for p in blk.blocks()]


def animated_frame_rate(vmt):
    """Frame rate of an AnimatedTexture proxy on the base texture, else None."""
    for p in _proxies(vmt):
        if p.name.lower() != "animatedtexture":
            continue
        if (p.get("animatedtexturevar") or "").lower() not in ("$basetexture", ""):
            continue
        try:
            return max(float(p.get("animatedtextureframerate") or DEFAULT_FRAME_RATE), 0.1)
        except ValueError:
            return DEFAULT_FRAME_RATE
    return None


def texture_scroll(vmt, variables=("$basetexturetransform",)):
    """TextureScroll proxy on one of the variables -> (u, v) per second, else None."""
    for p in _proxies(vmt):
        if p.name.lower() != "texturescroll":
            continue
        if (p.get("texturescrollvar") or "").lower() not in variables:
            continue
        try:
            rate = float(p.get("texturescrollrate") or 0)
            ang = math.radians(float(p.get("texturescrollangle") or 0))
        except ValueError:
            return None
        u, v = rate * math.cos(ang), rate * math.sin(ang)
        return (u, v) if abs(u) > 1e-6 or abs(v) > 1e-6 else None
    return None

# rope material that is not in any game or map file
CABLE_FALLBACK = "cs2porter/cable_black"
# rope materials that CS2 has but draws broken on cables: they get CABLE_FALLBACK too
BROKEN_CABLE_MATERIALS = frozenset({"cable/cable"})


def write_cable_fallback(content_dir, overwrite=False):
    """Plain black material (roughness 1) for ropes whose material was not found.
    Returns its name (like a Source 1 material name)."""
    path = os.path.join(content_dir, "materials", *CABLE_FALLBACK.split("/")) + ".vmat"
    if overwrite or not os.path.isfile(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(VC.TMPL_BASIC.format(color="[0.000000 0.000000 0.000000 0.000000]",
                                         sysattr=VC.build_system_attributes("metal")))
    return CABLE_FALLBACK


BLACK_VMAT = "cs2porter/black"
# plain black Source 1 materials: they become the program's own pure black, unlit material
BLACK_MATERIALS = frozenset({"dev/black_simple", "cs_italy/black"})
_BLACK_TEXT = """// THIS FILE IS AUTO-GENERATED

Layer0
{
	shader "csgo_unlitgeneric.vfx"

	//---- Color ----
	g_vColorTint "[1.000000 1.000000 1.000000 0.000000]"
	TextureColor "[0.000000 0.000000 0.000000 0.000000]"

	//---- Fog ----
	g_bFogEnabled "1"
}
"""


def write_black_material(content_dir):
    """materials/cs2porter/black.vmat (always brought up to date)."""
    path = os.path.join(content_dir, "materials", *BLACK_VMAT.split("/")) + ".vmat"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_BLACK_TEXT)
    return BLACK_VMAT


def map_surface(raw):
    if not raw:
        return None
    r = raw.strip().lower()
    for valid in VALID_SURFACES:
        if valid.lower() == r:
            return valid
    for key, surf in _SURFACE_ALIASES:
        if key in r:
            return surf
    return None


def norm_tex(val):
    v = val.strip().replace("\\", "/").lower()
    while "//" in v:
        v = v.replace("//", "/")
    v = v.lstrip("/")
    if v.startswith("materials/"):
        v = v[len("materials/"):]
    root, ext = os.path.splitext(v)
    if ext in (".vtf", ".tga", ".png", ".jpg", ".jpeg", ".psd", ".tif", ".bmp"):
        v = root
    return v


def out_name(rel):
    """CS2 path rule: lower case and underscores instead of spaces (same as the map importer).
    A leading '+' (animated texture names of Source 1, e.g. +0water) is dropped from every part
    of the path, because such names break in CS2; vmap.strip_plus fixes the map's references."""
    n = rel.lower().replace(" ", "_")
    if "+" in n:
        n = "/".join(p.lstrip("+") or p for p in n.split("/"))
    return n


def _is_true(v):
    return str(v).strip().strip('"').lower() in _TRUE_VALUES


class VMT:
    def __init__(self, shader, params, path, block=None):
        self.shader = shader
        self.params = params
        self.path = path
        self.block = block          # raw KV block (for Proxies etc.)

    def get(self, key, default=None):
        return self.params.get(key.lower(), default)

    def flag(self, key):
        return _is_true(self.params.get(key.lower(), "0"))


def _condition_true(cond):
    """VMT key conditions like "!gameconsole?$normalmap" or "GPU>=2?$x", evaluated for a PC."""
    neg = cond.startswith("!")
    c = cond.lstrip("!").strip().lower()
    m = re.match(r"gpu\s*(<=|>=|<|>|==)\s*(\d+)$", c)
    if m:
        level = 2
        val = {"<=": level <= int(m.group(2)), ">=": level >= int(m.group(2)), "<": level < int(m.group(2)),
               ">": level > int(m.group(2)), "==": level == int(m.group(2))}[m.group(1)]
    else:
        val = c not in ("gameconsole", "360", "x360", "ps3", "lowqualitycsm", "lowfillrate")
    return val != neg


def _collect_params(block, params, depth=0):
    conditional = []
    for k, v in block.values():
        if "?" in k:
            cond, key = k.split("?", 1)
            if _condition_true(cond):
                conditional.append((key.lower(), v))
            continue
        params[k.lower()] = v
    # true conditional keys win over the plain ones, like in the engine
    for key, v in conditional:
        params[key] = v
    for sub in block.blocks():
        name = sub.name.lower()
        if name in ("proxies",) or name.startswith("<") or "dx8" in name or "dx6" in name or "dx7" in name:
            continue
        if "dx9" in name or name.startswith(">=") or "hdr" in name or name in ("insert", "replace"):
            _collect_params(sub, params, depth + 1)


def parse_vmt(data, path, loader=None, _depth=0):
    """Parses VMT bytes. loader(rel) -> bytes|None: used for patch includes."""
    root = kv.parse(kv.decode_bytes(data))
    blocks = root.blocks()
    if not blocks:
        raise ValueError(t("m_vmt_empty"))
    top = blocks[0]
    shader = top.name.lower()
    params = {}
    if shader == "patch" and loader and _depth < 5:
        include = top.get("include")
        block = None
        if include:
            inc = include.replace("\\", "/").lower()
            if not inc.startswith("materials/"):
                inc = "materials/" + inc
            if not inc.endswith(".vmt"):
                inc += ".vmt"
            base_data = loader(inc)
            if base_data:
                base = parse_vmt(base_data, inc, loader, _depth + 1)
                shader = base.shader
                params.update(base.params)
                block = base.block
        for sub in top.blocks():
            if sub.name.lower() in ("insert", "replace"):
                for k, v in sub.values():
                    params[k.lower()] = v
        return VMT(shader, params, path, block)
    _collect_params(top, params)
    return VMT(shader, params, path, top)


def _floats(text, scale=1.0):
    if not text:
        return []
    nums = re.findall(r"-?\d*\.?\d+(?:e-?\d+)?", str(text))
    out = []
    for n in nums:
        try:
            out.append(float(n) * scale)
        except ValueError:
            pass
    return out


def _water_scroll_proxy(vmt):
    """TextureScroll proxy (rate, angle) -> (dx, dy)."""
    import math
    if vmt.block is None:
        return None
    for blk in vmt.block.blocks():
        if blk.name.lower() != "proxies":
            continue
        for p in blk.blocks():
            if p.name.lower() != "texturescroll":
                continue
            var = (p.get("texturescrollvar") or "").lower()
            if var not in ("$bumptransform", "$basetexturetransform", "$normalmaptransform"):
                continue
            try:
                rate = float(p.get("texturescrollrate") or 0)
                ang = math.radians(float(p.get("texturescrollangle") or 0))
            except ValueError:
                return None
            return rate * math.cos(ang), rate * math.sin(ang)
    return None


class MaterialResult:
    def __init__(self, mat, status, detail="", source=""):
        self.mat = mat
        self.status = status        # created | exists | cs2 | tool | missing | unsupported | error
        self.detail = detail
        self.source = source


class MaterialConverter:
    """Writes the missing materials into a target content folder (addon)."""

    def __init__(self, sources, index, content_dir, vtfcmd=None, log=None,
                 overwrite=False, convert_cs2_existing=False, texture_lights=None):
        self.sources = sources
        # {material: (r, g, b)} glow of the map's texture lights (texlights.texture_lights)
        self.texture_lights = texture_lights or {}
        self.index = index
        self.content_dir = content_dir
        self.vtfcmd = vtfcmd
        self.log = log or (lambda m, t="info": None)
        self.overwrite = overwrite
        self.convert_cs2_existing = convert_cs2_existing
        self.tex_cache = {}
        self.results = {}
        self.extra_textures = 0
        VC.set_base_dir(content_dir)

    # --- helpers -----------------------------------------------------------
    def _abs(self, game_rel):
        return os.path.join(self.content_dir, *game_rel.split("/"))

    def _load(self, rel):
        data, _src = self.sources.read(rel)
        return data

    def find_vmt(self, mat):
        rel = f"materials/{mat}.vmt"
        src = self.sources.find(rel)
        return src, rel

    def material_available(self, mat):
        """Is the material in the target / in CS2, or can it be found in a source?"""
        o = out_name(mat)
        if self.index.in_addon(f"materials/{o}.vmat") or self.index.in_cs2(f"materials/{o}.vmat_c"):
            return True
        return self.sources.find(f"materials/{mat}.vmt") is not None

    # --- textures ---------------------------------------------------------------
    def ensure_texture(self, tex):
        """Copies / converts a texture into the target folder. Returns 'materials/...ext'."""
        tex = norm_tex(tex)
        if not tex:
            return None
        if tex in self.tex_cache:
            return self.tex_cache[tex]
        o = out_name(tex)
        base_abs = self._abs(f"materials/{o}")
        if not self.overwrite:
            for ext in (".tga", ".png", ".jpg", ".jpeg", ".psd", ".tif", ".tiff"):
                if os.path.isfile(base_abs + ext):
                    res = f"materials/{o}{ext}"
                    self.tex_cache[tex] = res
                    return res
        src, rel, ext = self.sources.find_texture(tex)
        if src is None:
            self.tex_cache[tex] = None
            return None
        result = None
        try:
            data = src.read(rel)
            if ext == ".vtf":
                out = base_abs + ".tga"
                if vtfmod.vtf_to_image_file(data, out, self.vtfcmd, self.log):
                    result = f"materials/{o}.tga"
            elif ext in _IMAGE_COPY_EXTS:
                out = base_abs + ext
                os.makedirs(os.path.dirname(out), exist_ok=True)
                p = src.path(rel)
                if p:
                    shutil.copyfile(p, out)
                else:
                    with open(out, "wb") as f:
                        f.write(data)
                result = f"materials/{o}{ext}"
            else:  # bmp etc. -> tga
                out = base_abs + ".tga"
                img = vtfmod.image_from_bytes(data, ext)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                img.save(out)
                result = f"materials/{o}.tga"
        except Exception as e:  # noqa: BLE001
            self.log(t("m_tex_fail", tex=tex, e=e), "warn")
            result = None
        if result:
            _make_pow2(self._abs(result))
            self.log(t("d_texture", tex=result, src=getattr(src, "label", "")), "tool")
        self.tex_cache[tex] = result
        return result

    def ensure_sheet(self, tex):
        """Animated VTF -> one sheet with every frame, row by row from the top left (see sheet_grid).
        Returns (path, frames, columns, rows), or None for a texture with one frame."""
        tex = norm_tex(tex)
        src, rel, ext = self.sources.find_texture(tex)
        if src is None or ext != ".vtf":
            return None
        try:
            data = src.read(rel)
            if vtfmod.read_header(data).frames < 2:
                return None
            frames, _info = vtfmod.load_vtf_frames(data)
        except Exception as e:  # noqa: BLE001
            self.log(t("m_tex_fail", tex=tex, e=e), "warn")
            return None
        n = len(frames)
        if n < 2:
            return None
        w, h = frames[0].size
        cols, rows = sheet_grid(n, w, h)
        # CS2 can not compile a texture whose sides are not powers of two: the row count is
        # made one too (the extra rows repeat the frames, see below)
        rows = _pow2_up(rows)
        scale = min(1.0, SHEET_MAX / (cols * w), SHEET_MAX / (rows * h))
        fw, fh = max(1, int(w * scale)), max(1, int(h * scale))
        alpha = any(f.mode == "RGBA" and f.getchannel("A").getextrema()[0] < 250 for f in frames)
        mode = "RGBA" if alpha else "RGB"
        sheet = Image.new(mode, (cols * fw, rows * fh))
        cells = []
        for f in frames:
            f = f.convert(mode)
            if (fw, fh) != f.size:
                f = f.resize((fw, fh), Image.LANCZOS)
            cells.append(f)
        # the empty cells after the last frame get the frames that follow it (0, 1, ...): a
        # frame number that is rounded past the end then still shows the right picture
        for i in range(cols * rows):
            sheet.paste(cells[i % n], ((i % cols) * fw, (i // cols) * fh))
        size = tuple(min(_pow2_up(v), SHEET_MAX) for v in sheet.size)
        if size != sheet.size:
            sheet = sheet.resize(size, Image.LANCZOS)
        res = f"materials/{out_name(tex)}.tga"
        out = self._abs(res)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        sheet.save(out)
        self.tex_cache[tex] = res
        self.log(t("d_sheet", tex=res, n=n, cols=cols, rows=rows), "tool")
        return res, n, cols, rows

    # --- translucency ---------------------------------------------------------------
    @staticmethod
    def _alpha_used(abs_path):
        try:
            img = Image.open(abs_path)
            if img.mode not in ("RGBA", "LA", "PA") and "transparency" not in img.info:
                return False
            a = np.asarray(img.convert("RGBA").getchannel("A"))
            return a.min() < 250
        except Exception:
            return False

    def _translucency(self, color_rel, vmt, decide_by_pixels=True):
        """(is_translucent, translucency_texture_path)"""
        abs_color = self._abs(color_rel)
        if not os.path.isfile(abs_color):
            return False, None
        explicit = vmt.flag("$translucent") or vmt.flag("$alphatest") or vmt.flag("$vertexalpha")
        alpha_is_mask = any(vmt.flag(k) for k in (
            "$basealphaenvmapmask", "$selfillum", "$blendtintbybasealpha",
            "$basemapalphaphongmask", "$basealphaphongmask"))
        if explicit:
            transparent = self._alpha_used(abs_color)
        elif alpha_is_mask:
            transparent = False
        elif decide_by_pixels:
            transparent = VC.has_transparency(abs_color)
        else:
            transparent = False
        if not transparent:
            return False, None
        trans_abs = VC.create_translucent_texture(abs_color)
        if not trans_abs:
            return False, None
        rel = os.path.relpath(trans_abs, self.content_dir).replace("\\", "/")
        return True, rel

    # --- main conversion -----------------------------------------------------------
    def convert(self, mat, usages=("brush",)):
        mat = mat.lower().replace("\\", "/")
        if mat in self.results:
            return self.results[mat]
        res = self._convert(mat, set(usages))
        self.results[mat] = res
        return res

    def _convert(self, mat, usages):
        o = out_name(mat)
        vmat_rel = f"materials/{o}.vmat"
        if mat.startswith("tools/") or mat.startswith("tools\\"):
            return MaterialResult(mat, "tool", t("m_tool"))
        if mat in BLACK_MATERIALS:
            # the faces are given the program's own black material in the .vmap
            write_black_material(self.content_dir)
            return MaterialResult(mat, "created", t("m_black"))
        if not self.overwrite and self.index.in_addon(vmat_rel):
            return MaterialResult(mat, "exists", t("m_exists"))
        # a texture light needs its own glowing material, also when CS2 has one of that name
        if not self.convert_cs2_existing and self.index.in_cs2(f"{vmat_rel}_c") and mat not in self.texture_lights:
            return MaterialResult(mat, "cs2", t("m_cs2"))

        src, rel = self.find_vmt(mat)
        if src is None:
            # no VMT, but a texture with the same name exists: make a simple material
            color = self.ensure_texture(mat)
            if color:
                vmt = VMT("lightmappedgeneric", {}, rel)
                return self._write(mat, vmt, usages, color, None, None, None, t("m_texture_only"))
            return MaterialResult(mat, "missing", t("m_no_vmt"))
        try:
            vmt = parse_vmt(src.read(rel), rel, self._load)
        except Exception as e:  # noqa: BLE001
            return MaterialResult(mat, "error", t("m_vmt_error", e=e), src.label)

        shader = vmt.shader
        if shader in ("sky", "skybox"):
            return MaterialResult(mat, "unsupported", t("m_sky"), src.label)
        fade = proximity_fade(vmt)
        if fade:
            return self._write_fade(mat, vmt, fade, src.label)
        if shader == "windowimposter":
            return self._write_imposter(mat, vmt, src.label)
        if shader == "water":
            return self._write_water(mat, vmt, src.label)
        if shader == "refract":
            return self._write_refract(mat, vmt, src.label)

        base = vmt.get("$basetexture") or vmt.get("$hdrbasetexture")
        env = (vmt.get("$envmap") or "").strip().lower()
        if not base and env and env != "env_cubemap":
            # only a cubemap: the surface shows that sky
            res = self._write_imposter(mat, vmt, src.label)
            if res.status == "created":
                return res
        if not base:
            return MaterialResult(mat, "unsupported", t("m_no_base", shader=shader), src.label)
        vmt.anim = None
        rate = animated_frame_rate(vmt)
        sheet = self.ensure_sheet(base) if rate else None
        if sheet:
            color = sheet[0]
            vmt.anim = (sheet[1], sheet[2], sheet[3], 1.0 / rate)
        else:
            color = self.ensure_texture(base)
        if not color:
            return MaterialResult(mat, "missing", t("m_tex_missing", tex=norm_tex(base)), src.label)

        normal = None
        bump = vmt.get("$bumpmap") or vmt.get("$normalmap")
        if bump and not norm_tex(bump).startswith("dev/flat"):
            normal = self.ensure_texture(bump)

        color2 = normal2 = None
        if shader in _BLEND_SHADERS:
            b2 = vmt.get("$basetexture2")
            if b2:
                color2 = self.ensure_texture(b2)
            n2 = vmt.get("$bumpmap2") or vmt.get("$normalmap2")
            if n2:
                normal2 = self.ensure_texture(n2)
        return self._write(mat, vmt, usages, color, normal, color2, normal2, src.label)

    def _blend_modulation(self, vmt):
        """Blend modulation texture of a two layer material: the material's own
        $blendmodulatetexture, else a plain one that gives the smooth Source 1 fade."""
        tex = vmt.get("$blendmodulatetexture")
        if tex:
            found = self.ensure_texture(tex)
            if found:
                return found
        path = self._abs(SOFT_BLEND_TEX)
        if not os.path.isfile(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            Image.new("RGB", (4, 4), (128, 128, 0)).save(path)
        return SOFT_BLEND_TEX

    def _tinted(self, color, tint):
        """color texture multiplied by tint (csgo_unlitgeneric ignores its color tint), the same
        texture when the tint is white or the texture can not be read."""
        if color.startswith("[") or all(abs(c - 1.0) < 1e-3 for c in tint):
            return color
        tag = "".join(f"{min(max(int(round(c * 255)), 0), 255):02x}" for c in tint)
        out_rel = color.rsplit(".", 1)[0] + f"_tint_{tag}.tga"
        out = self._abs(out_rel)
        if os.path.isfile(out):
            return out_rel
        try:
            with Image.open(self._abs(color)) as img:
                img = img.convert("RGBA")
            bands = list(img.split())
            for i in range(3):
                k = max(tint[i], 0.0)
                bands[i] = bands[i].point(lambda v, k=k: min(int(v * k + 0.5), 255))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            Image.merge("RGBA", bands).save(out)
            return out_rel
        except (OSError, ValueError):
            return color

    def _additive_layers(self, color, trans, tint):
        """(color texture, translucency texture) of a translucent surface that looks like an
        additive one: the added light (texture x tint x translucency) divided by its brightest
        channel, and that channel as the opacity. None when the texture can not be read."""
        if color.startswith("["):
            return None
        tag = "".join(f"{min(max(int(round(c * 255)), 0), 255):02x}" for c in tint)
        base = color.rsplit(".", 1)[0] + f"_add_{tag}"
        out_c, out_m = base + ".tga", base + "_mask.tga"
        if os.path.isfile(self._abs(out_c)) and os.path.isfile(self._abs(out_m)):
            return out_c, out_m
        try:
            with Image.open(self._abs(color)) as img:
                rgb = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
            light = rgb * np.asarray([max(c, 0.0) for c in tint], dtype=np.float32)
            if trans and not trans.startswith("["):
                with Image.open(self._abs(trans)) as m:
                    m = m.convert("L")
                    if m.size != (rgb.shape[1], rgb.shape[0]):
                        m = m.resize((rgb.shape[1], rgb.shape[0]), Image.BILINEAR)
                    light *= (np.asarray(m, dtype=np.float32) / 255.0)[..., None]
            light = np.clip(light, 0.0, 1.0)
            peak = light.max(axis=2)
            col = np.where(peak[..., None] > 1e-4, light / np.maximum(peak, 1e-4)[..., None], 0.0)
            os.makedirs(os.path.dirname(self._abs(out_c)), exist_ok=True)
            Image.fromarray((col * 255 + 0.5).astype(np.uint8), "RGB").save(self._abs(out_c))
            mask = (peak * 255 + 0.5).astype(np.uint8)
            Image.merge("RGB", [Image.fromarray(mask, "L")] * 3).save(self._abs(out_m))
            return out_c, out_m
        except (OSError, ValueError):
            return None

    def _self_illum_mask(self, vmt, color, is_unlit):
        """Where a $selfillum material glows: $selfillummask, else the alpha of the base texture
        (Source 1 lit shaders), else everywhere (unlit shaders, or no alpha)."""
        tex = vmt.get("$selfillummask")
        if tex:
            found = self.ensure_texture(tex)
            if found:
                return found
        if is_unlit:
            return WHITE_MASK
        src = self._abs(color)
        out_rel = color.rsplit(".", 1)[0] + "_selfillum.tga"
        try:
            with Image.open(src) as img:
                if img.mode not in ("RGBA", "LA", "PA") and "transparency" not in img.info:
                    return WHITE_MASK
                alpha = img.convert("RGBA").getchannel("A")
            if alpha.getextrema()[0] >= 250:
                return WHITE_MASK
            out = self._abs(out_rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            Image.merge("RGB", (alpha, alpha, alpha)).save(out)
            return out_rel
        except (OSError, ValueError):
            return WHITE_MASK

    def _write_water(self, mat, vmt, source_label):
        """Water shader -> csgo_water.vfx (same mapping as the map importer), with Lightmapped
        Water Fog and Fresnel off and a low reflectance: otherwise the water reflects white."""
        ntex = vmt.get("$normalmap") or vmt.get("$bumpmap")
        normal = self.ensure_texture(ntex) if ntex else None
        s1 = (_floats(vmt.get("$scroll1")) + [0.0, 0.0])[:2]
        s2 = (_floats(vmt.get("$scroll2")) + [0.0, 0.0])[:2]
        try:
            refract = float(_floats(vmt.get("$refractamount", "1"))[0])
        except IndexError:
            refract = 1.0
        fog_raw = vmt.get("$fogcolor") or "{20 40 45}"
        fog = _floats(fog_raw, 1 / 255.0) if "{" in fog_raw else _floats(fog_raw)
        fog = (fog + [0.08, 0.15, 0.18])[:3]
        # $reflecttint (the color of the reflection in Source 1) times the water's color
        tint_raw = vmt.get("$reflecttint") or ""
        rt = (_floats(tint_raw, 1 / 255.0 if "{" in tint_raw else 1.0) + [1.0, 1.0, 1.0])[:3] if tint_raw else [1.0] * 3
        refl = [min(max(fog[k] * rt[k], 0.0), 1.0) for k in range(3)]
        # Source 1 water fog does not start at $fogstart: it grows with the depth under the
        # surface and is full at ($fogend - $fogstart) deep, so only that range is kept
        s1_start = (_floats(vmt.get("$fogstart", "0")) + [0.0])[0]
        s1_end = (_floats(vmt.get("$fogend", "500")) + [500.0])[0]
        fstart = WATER_START_MIN
        fend = fstart + max(s1_end - max(s1_start, 0.0), 1.0)
        T = "\t"
        lines = ['"Layer0"', "{", f'{T}"Shader"{T}{T}"csgo_water.vfx"']
        if normal:
            # a water without a normal map gets none (flat water)
            lines.append(f'{T}"TextureNormal"{T}{T}"{normal}"')
        lines += [
            f'{T}"g_vExtraTexCoordScroll"{T}{T}"[{s1[0]:.6f} {s1[1]:.6f} {s2[0]:.6f} {s2[1]:.6f}]"',
            f'{T}"g_flRefractionAmount"{T}{T}"{refract:.6f}"',
            f'{T}"g_vWaterFogColor"{T}{T}"[{fog[0]:.6f} {fog[1]:.6f} {fog[2]:.6f}]"',
            # the reflected cubemap is tinted with the water's color: the light probe cubemaps
            # are baked in white rooms (see vmap.light_room_blocks)
            f'{T}"g_vReflectionColor"{T}{T}"[{refl[0]:.6f} {refl[1]:.6f} {refl[2]:.6f}]"',
            f'{T}"g_flReflectance"{T}{T}"{WATER_REFLECTANCE:.6f}"',
            f'{T}"F_FRESNEL"{T}{T}"0"',
            f'{T}"F_LIGHTMAP_WATER_FOG"{T}{T}"0"',
            f'{T}"g_flWaterDepth"{T}{T}"{fend:.6f}"',
            f'{T}"g_flWaterStart"{T}{T}"{fstart:.6f}"',
        ]
        scroll = _water_scroll_proxy(vmt)
        if scroll and (abs(scroll[0]) > 1e-6 or abs(scroll[1]) > 1e-6):
            lines += [f'{T}"DynamicParams"', f"{T}{{",
                      f'{T}{T}"g_vNormalTexCoordOffset"{T}{T}"frac( float2( {scroll[0]:.3f}, {scroll[1]:.3f} ) * time() )"',
                      f"{T}}}"]
        lines += [f'{T}"Attributes"', f"{T}{{",
                  f'{T}{T}"mapbuilder.nonsolid"{T}{T}"1"',
                  f'{T}{T}"mapbuilder.water"{T}{T}"1"', f"{T}}}",
                  f'{T}"SystemAttributes"', f"{T}{{",
                  f'{T}{T}"PhysicsSurfaceProperties"{T}{T}"water"', f"{T}}}", "}", ""]
        vmat_abs = self._abs(f"materials/{out_name(mat)}.vmat")
        os.makedirs(os.path.dirname(vmat_abs), exist_ok=True)
        with open(vmat_abs, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return MaterialResult(mat, "created", t("m_water"), source_label)

    def _save(self, mat, content):
        vmat_abs = self._abs(f"materials/{out_name(mat)}.vmat")
        os.makedirs(os.path.dirname(vmat_abs), exist_ok=True)
        with open(vmat_abs, "w", encoding="utf-8") as f:
            f.write(content)

    def _write_fade(self, mat, vmt, fade, source_label):
        """$alpha driven by the player's distance -> csgo_effects with a distance fade."""
        base = vmt.get("$basetexture")
        tint = vmt_color(vmt) or (1.0, 1.0, 1.0)
        color = None
        if base and not norm_tex(base).startswith("tools/"):
            color = self.ensure_texture(base)
        if not color:
            # a tool texture (or none) under a $color: the plain color
            color = f"[{tint[0]:.6f} {tint[1]:.6f} {tint[2]:.6f} 0.000000]"
            tint = (1.0, 1.0, 1.0)
        self._save(mat, fade_vmat(color, tint, fade))
        return MaterialResult(mat, "created", t("m_fade", a=round(fade[0]), b=round(fade[1])), source_label)

    def _write_imposter(self, mat, vmt, source_label):
        """WindowImposter (a surface that shows a cubemap like a window to far away) -> a
        moondome material showing that cubemap, like the sky brushes."""
        from . import skybox as skymod     # skybox imports this module
        env = vmt.get("$envmap")
        if not env or env.strip().lower() == "env_cubemap":
            return MaterialResult(mat, "unsupported", t("m_no_base", shader=vmt.shader), source_label)
        src, rel, ext = self.sources.find_texture(norm_tex(env))
        cube = None
        if src is not None and ext == ".vtf":
            try:
                cube = vtfmod.load_vtf_faces(src.read(rel))
            except Exception:  # noqa: BLE001
                cube = None
        if not cube:
            return MaterialResult(mat, "missing", t("m_tex_missing", tex=norm_tex(env)), source_label)
        faces = skymod.cubemap_to_sky(cube)
        size = min(1024, max(img.width for img in faces.values()))
        png = f"materials/{out_name(norm_tex(env))}_cube.png"
        out = self._abs(png)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        skymod.compose(faces, skymod.SIDES, 0, 0, size).save(out, "PNG")
        content = skymod.MOONDOME_TEMPLATE % {"png": png}
        tint = vmt_color(vmt) if not _proxy_writes(vmt, "$color") else None
        if tint:
            content = content.replace('g_vColorTint "[1.000000 1.000000 1.000000 0.000000]"',
                                      f'g_vColorTint "[{tint[0]:.6f} {tint[1]:.6f} {tint[2]:.6f} 0.000000]"')
        self._save(mat, content)
        return MaterialResult(mat, "created", t("m_imposter"), source_label)

    def _write_refract(self, mat, vmt, source_label):
        """Refract shader. Without a scroll it becomes csgo_refract (see refract_vmat).
        csgo_refract cannot scroll, so a scrolling one becomes a look-alike: a translucent,
        shiny csgo_complex surface in the $refracttint color whose normal map scrolls."""
        ntex = vmt.get("$normalmap") or vmt.get("$bumpmap")
        normal = self.ensure_texture(ntex) if ntex else None
        if not normal:
            return MaterialResult(mat, "unsupported", t("m_no_base", shader=vmt.shader), source_label)
        sysattr = VC.build_system_attributes(map_surface(vmt.get("$surfaceprop")) or "glass")
        scroll = texture_scroll(vmt, ("$bumptransform", "$normalmaptransform", "$basetexturetransform"))
        if scroll:
            raw = vmt.get("$refracttint") or ""
            tint = (_floats(raw, 1 / 255.0 if "{" in raw else 1.0) + [1.0, 1.0, 1.0])[:3]
            tex = vmt.get("$refracttinttexture") or vmt.get("$basetexture")
            color = (self.ensure_texture(tex) if tex else None) or \
                f"[{tint[0]:.6f} {tint[1]:.6f} {tint[2]:.6f} 0.000000]"
            op = REFRACT_OPACITY
            content = complex_vmat(color, normal, sysattr, translucency=f"[{op:.6f} {op:.6f} {op:.6f} 0.000000]",
                                   scroll=scroll, roughness=REFRACT_ROUGHNESS,
                                   tint=tint if not color.startswith("[") else None)
            detail = t("m_refract_scroll")
        else:
            content = refract_vmat(normal, vmt.get("$refracttint"), sysattr)
            detail = t("m_refract")
        vmat_abs = self._abs(f"materials/{out_name(mat)}.vmat")
        os.makedirs(os.path.dirname(vmat_abs), exist_ok=True)
        with open(vmat_abs, "w", encoding="utf-8") as f:
            f.write(content)
        return MaterialResult(mat, "created", detail, source_label)

    def _write(self, mat, vmt, usages, color, normal, color2, normal2, source_label):
        o = out_name(mat)
        vmat_abs = self._abs(f"materials/{o}.vmat")
        shader = vmt.shader
        surface = map_surface(vmt.get("$surfaceprop"))
        sysattr = VC.build_system_attributes(surface)
        normal_path = normal or DEFAULT_NORMAL
        lower = mat.lower()
        segs = re.split(r"[/_.\-]", lower)

        on_brush = bool({"brush", "brushent"} & usages)
        # $decal materials on brush faces stay normal (translucent) materials in CS2
        is_decal = (shader in _DECAL_SHADERS
                    or (("overlay" in usages or "decal" in usages) and not on_brush)
                    or ((vmt.flag("$decal") or VC._keyword_hit(lower, "decal")) and not on_brush))
        is_glass = VC._keyword_hit(lower, "glass")
        is_unlit = shader in _UNLIT_SHADERS
        is_model = "model" in usages
        is_foliage = is_model and (surface == "foliage" or any(any(s.startswith(k) for k in _FOLIAGE_KW) for s in segs))
        scroll = texture_scroll(vmt)

        if shader in _BLEND_SHADERS and color2:
            content = VC.TMPL_WORLDVERTEX.format(
                color1=color, normal1=normal_path,
                color2=color2, normal2=normal2 or DEFAULT_NORMAL, sysattr=sysattr)
            # without "fancy blending" CS2 switches between the layers with a hard edge;
            # with a blend modulation texture it fades like Source 1. Mode 2 also keeps the
            # Blend Softness setting (mode 1 drops it when the material is compiled)
            content = content.replace(
                "\tF_LAYERS 1 // WorldVertexTransition\n",
                "\tF_LAYERS 1 // WorldVertexTransition\n\tF_FANCY_BLENDING 2\n"
                f'\tTextureBlendModulation "{self._blend_modulation(vmt)}"\n'
                f'\tg_flBlendSoftness "{BLEND_SOFTNESS}"\n', 1)
            tag = t("m_worldvertex")
        elif getattr(vmt, "anim", None) and not is_decal:
            transparent, trans = self._translucency(color, vmt)
            content = complex_vmat(color, normal_path, sysattr, translucency=trans if transparent else None,
                                   anim=vmt.anim, scroll=scroll)
            tag = t("m_animated", n=vmt.anim[0])
        elif lower in self.texture_lights and not is_decal:
            # the whole surface glows in the light's color (not the texture's)
            tint, bright = texlights.self_illum(self.texture_lights[lower])
            content = TMPL_SELF_ILLUM.format(
                color=color, normal=normal_path, mask=WHITE_MASK, brightness=f"{bright:.3f}", albedo="0.000",
                tint=f"[{tint[0]:.6f} {tint[1]:.6f} {tint[2]:.6f} 0.000000]", sysattr=sysattr)
            tag = t("m_texture_light", b=f"{bright:.2f}")
        elif vmt.flag("$selfillum") and not is_decal:
            mask = self._self_illum_mask(vmt, color, is_unlit)
            raw_tint = vmt.get("$selfillumtint") or ""
            tint = (_floats(raw_tint, 1 / 255.0 if "{" in raw_tint else 1.0) + [1.0, 1.0, 1.0])[:3]
            tint, bright = texlights.self_illum([max(v, 0.0) for v in tint])
            content = TMPL_SELF_ILLUM.format(
                color=color, normal=normal_path, mask=mask, brightness=f"{bright:.3f}", albedo="1.000",
                tint=f"[{tint[0]:.6f} {tint[1]:.6f} {tint[2]:.6f} 0.000000]", sysattr=sysattr)
            tag = "self illum"
        else:
            transparent, trans = self._translucency(color, vmt, decide_by_pixels=not is_model or is_foliage or is_decal or is_glass)
            if is_foliage and (transparent or vmt.flag("$alphatest") or vmt.flag("$translucent")):
                slot = {"TextureLayer1Color": color, "TextureLayer1Normal": normal_path}
                if transparent and trans:
                    slot["TextureLayer1Translucency"] = trans
                # Source 1 foliage props did not sway (Valve's own CS2 trees don't either), and
                # the CS2 wind animation moves the whole converted model far too much
                content = build_vmat_foliage(slot, "").replace("F_FOLIAGE_ANIMATION 1", "F_FOLIAGE_ANIMATION 0")
                tag = "foliage"
            elif is_glass:
                if transparent and trans:
                    content = VC.TMPL_GLASS_TRANSLUCENT.format(color=color, transculent=trans)
                    tag = "glass + translucent"
                else:
                    content = VC.TMPL_GLASS_OPAQUE.format(color=color)
                    tag = "glass (opaque)"
            elif is_decal:
                if transparent and trans:
                    content = VC.TMPL_DECAL_TRANSLUCENT.format(color=color, transculent=trans)
                    tag = "decal + translucent"
                else:
                    content = VC.TMPL_DECAL.format(color=color)
                    tag = "decal"
            elif is_unlit:
                # $color tints, $alpha fades (an additive surface by getting darker), $additive
                # adds the surface to what is behind it, $nocull draws both sides
                tint = list(vmt_color(vmt) or (1.0, 1.0, 1.0)) if not _proxy_writes(vmt, "$color") else [1.0] * 3
                try:
                    alpha = min(max(float(_floats(vmt.get("$alpha", "1"))[0]), 0.0), 1.0)
                except IndexError:
                    alpha = 1.0
                if _proxy_writes(vmt, "$alpha"):
                    alpha = 1.0
                additive = vmt.flag("$additive")
                blend = 0
                trans_tex = trans if transparent and trans else None
                if additive:
                    tint = [c * alpha for c in tint]
                    # CS2 draws the Additive blend mode of csgo_unlitgeneric white: the added
                    # light becomes a translucent layer (its color at full strength, as opaque
                    # as it is bright)
                    layers = self._additive_layers(color, trans_tex, tint)
                    if layers:
                        color, trans_tex = layers
                        tint = [1.0, 1.0, 1.0]
                        blend = UNLIT_BLEND_TRANSLUCENT
                    else:
                        blend = UNLIT_BLEND_ADDITIVE
                elif trans_tex or alpha < 0.999:
                    blend = UNLIT_BLEND_TRANSLUCENT
                    if not trans_tex:
                        trans_tex = f"[{alpha:.6f} {alpha:.6f} {alpha:.6f} 0.000000]"
                tinted = self._tinted(color, tint)
                if tinted != color:
                    color, tint = tinted, [1.0, 1.0, 1.0]
                content = unlit_vmat(color, translucency=trans_tex, blend=blend, tint=tint, scroll=scroll,
                                     backfaces=vmt.flag("$nocull"))
                tag = "unlit" + (" + additive" if additive else " + translucent" if blend else "") + \
                    (" + " + t("m_scroll") if scroll else "")
            elif scroll:
                content = complex_vmat(color, normal_path, sysattr,
                                       translucency=trans if transparent and trans else None, scroll=scroll)
                tag = t("m_scroll")
            elif transparent and trans:
                content = VC.TMPL_TRANSLUCENT.format(color=color, normal=normal_path,
                                                     transculent=trans, sysattr=sysattr)
                tag = "translucent"
            elif normal:
                content = VC.TMPL_WITH_NORMAL.format(color=color, normal=normal_path, sysattr=sysattr)
                tag = "normal map"
            else:
                content = VC.TMPL_BASIC.format(color=color, sysattr=sysattr)
                tag = "basic"

        os.makedirs(os.path.dirname(vmat_abs), exist_ok=True)
        with open(vmat_abs, "w", encoding="utf-8") as f:
            f.write(content)
        detail = tag + (t("m_surface", s=surface) if surface else "")
        return MaterialResult(mat, "created", detail, source_label)
