import os
import sys
import glob
import shutil
import subprocess
import time
import re as _re
import numpy as np
from PIL import Image

BASE_DIR = os.getcwd()
TEXTURE_INFO_FILE = os.path.join(BASE_DIR, "Missing Textures", "texture_info.txt")

OPACITY_THRESHOLD    = 0.99
WHITE_SKIP_THRESHOLD = 0.99

SURFACE_KEYWORDS = [
    (["glass", "cam", "pencere", "window"],               "glass"),
    (["plaster", "siva", "stucco"],                       "plaster"),
    (["concrete", "beton", "cement"],                     "concrete"),
    (["brick", "tugla"],                                  "brick"),
    (["metal", "grate", "vent", "steel", "iron",
      "aluminum", "aluminium", "galv"],                    "metal"),
    (["wood", "ahsap", "tahta", "plank", "crate",
      "lumber", "parquet", "oak", "pine"],                 "Wood"),
    (["tile", "fayans", "ceramic"],                       "tile"),
    (["rock", "stone", "cobble"],                         "rock"),
    (["sand", "kum", "desert"],                           "sand"),
    (["dirt", "toprak", "earth", "ground", "soil"],       "dirt"),
    (["grass", "cim", "lawn", "turf"],                    "grass"),
    (["water", "su", "liquid", "pool", "ocean", "river"], "water"),
    (["snow", "kar", "ice", "buz"],                       "snow"),
    (["mud", "camur"],                                    "mud"),
    (["plastic", "plastik"],                              "plastic"),
    (["rubber", "lastik", "tire"],                        "rubber"),
    (["carpet", "hali", "fabric", "cloth"],               "carpet"),
    (["flesh", "body", "organ"],                          "Flesh"),
    (["gravel"],                                          "gravel"),
    (["cardboard", "karton", "paper"],                    "cardboard"),
    (["foliage", "leaf", "yaprak", "bush", "shrub"],      "foliage"),
]

def _path_segments(abs_path):
    rel = os.path.relpath(abs_path, BASE_DIR).replace("\\", "/").lower()
    return rel

def _keyword_hit(text, kw):
    return bool(_re.search(r'(?<![a-z])' + _re.escape(kw), text))

def detect_surface(abs_path):
    rel = _path_segments(abs_path)
    parts = rel.rsplit("/", 1)
    folder_part = parts[0] if len(parts) == 2 else ""
    file_part   = parts[-1]
    for segment in (folder_part, file_part):
        for keywords, surface in SURFACE_KEYWORDS:
            for kw in keywords:
                if _keyword_hit(segment, kw):
                    return surface
    return None

NORMAL_SUFFIXES = [
    "_n", "_nrm", "_normal", "_norm", "_nor",
    "_normalmap", "_nm", "_nmap", "_ddna",
]

def is_valid_normal_map(path):
    try:
        img = Image.open(path).convert("RGB")
        arr = np.array(img, dtype=np.float32)
        b_mean = arr[:, :, 2].mean()
        r_mean = arr[:, :, 0].mean()
        g_mean = arr[:, :, 1].mean()
        return b_mean > r_mean + 20 and b_mean > g_mean + 20 and b_mean > 100
    except Exception:
        return False

def find_normal_map(folder, base_name):
    base_lower = base_name.lower()
    extensions = [".png", ".jpg", ".jpeg", ".tga", ".bmp"]
    candidates = []
    for fname in os.listdir(folder):
        fbase, fext = os.path.splitext(fname)
        if fext.lower() not in extensions:
            continue
        fbase_lower = fbase.lower()
        for suffix in NORMAL_SUFFIXES:
            if fbase_lower == base_lower + suffix:
                candidates.append(os.path.join(folder, fname))
            if fbase_lower == base_lower and suffix in fbase_lower:
                candidates.append(os.path.join(folder, fname))
    for candidate in candidates:
        if is_valid_normal_map(candidate):
            return candidate
    return None

def is_normal_map_file(fname):
    base = os.path.splitext(fname)[0].lower()
    return any(base.endswith(s) for s in NORMAL_SUFFIXES)

def has_transparency(path):
    """
    Real translucency test.

    Returns True only if both conditions hold:
      1. The share of pixels that are not fully opaque is above OPACITY_THRESHOLD  (existing rule)
      2. At least 1% of the pixels are really transparent (alpha < 10)  (NEW rule)

    Without the second rule, TGAs that carry roughness/spec data in the
    alpha channel (stonework, brick etc.) were wrongly marked as
    translucent.
    """
    try:
        ext = os.path.splitext(path)[1].lower()
        # JPG/JPEG never has an alpha channel
        if ext in (".jpg", ".jpeg"):
            return False
        img = Image.open(path)
        if img.mode not in ("RGBA", "LA", "PA"):
            return False
        img = img.convert("RGBA")
        arr = np.array(img, dtype=np.uint8)
        alpha = arr[:, :, 3]
        # Rule 1: are there enough pixels that are not fully opaque?
        not_fully_opaque = (np.sum(alpha == 255) / alpha.size) < OPACITY_THRESHOLD
        # Rule 2: are at least 1% of the pixels really transparent (alpha < 10)?
        # (alpha used as a roughness/spec channel does not pass this)
        truly_transparent = (np.sum(alpha < 10) / alpha.size) >= 0.01
        return not_fully_opaque and truly_transparent
    except Exception:
        return False

def fix_image_imagemagick(src, dst):
    try:
        result = subprocess.run(
            ["convert", src, "-define", "png:compression-level=9", dst],
            capture_output=True, timeout=30
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

def create_translucent_texture(src_path):
    ext      = os.path.splitext(src_path)[1].lower()
    base     = os.path.splitext(src_path)[0]
    out_path = base + "_transculent" + ext
    if os.path.exists(out_path):
        return out_path
    img = None
    try:
        img = Image.open(src_path)
        img.verify()
        img = Image.open(src_path)
    except Exception as e:
        print(f"  warn  corrupt image, trying to repair it: {os.path.basename(src_path)} ({e})")
        tmp_dir  = os.path.join(BASE_DIR, "_temp_fixed_vmat")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_path = os.path.join(tmp_dir, os.path.basename(src_path))
        if fix_image_imagemagick(src_path, tmp_path):
            try:
                img = Image.open(tmp_path)
            except Exception as e2:
                print(f"  fail  could not repair: {e2}")
                return None
        else:
            print(f"  fail  could not repair the file")
            return None
    img        = img.convert("RGBA")
    arr        = np.array(img, dtype=np.float32)
    alpha      = arr[:, :, 3] / 255.0
    white_ratio = np.sum(alpha >= WHITE_SKIP_THRESHOLD) / alpha.size
    if white_ratio >= WHITE_SKIP_THRESHOLD:
        return None
    brightness  = (alpha * 255).astype(np.uint8)
    rgb         = np.stack([brightness, brightness, brightness], axis=2)
    result_img  = Image.fromarray(rgb, "RGB")
    if ext == ".tga":
        result_img.save(out_path)
    elif ext == ".bmp":
        result_img.save(out_path, format="BMP")
    else:
        result_img.save(out_path, optimize=False, compress_level=1)
    return out_path

def make_relative_path(abs_path):
    rel = os.path.relpath(abs_path, BASE_DIR).replace("\\", "/")
    if rel.startswith("materials/"):
        return rel
    return f"materials/{rel}"

def build_system_attributes(surface):
    if not surface:
        return ""
    return f'\tSystemAttributes\n\t{{\n\t\tPhysicsSurfaceProperties "{surface}"\n\t}}\n\n'

TMPL_BASIC = """\
// THIS FILE IS AUTO-GENERATED

Layer0
{{
\tshader "csgo_lightmappedgeneric.vfx"

\t//---- Color ----
\tg_flModelTintAmount "1.000"
\tg_flVertexColorOpacityScale "1.000"
\tg_vColorTint "[1.000000 1.000000 1.000000 0.000000]"

\t//---- Fog ----
\tg_bFogEnabled "1"

\t//---- Lighting ----
\tg_flMetalness "0.000"

\t//---- PBR 1 ----
\tg_vLayer1Tint "[1.000000 1.000000 1.000000 0.000000]"
\tTextureLayer1AmbientOcclusion "materials/default/default_ao.tga"
\tTextureLayer1Color "{color}"
\tTextureLayer1Normal "materials/default/default_normal.tga"
\tTextureLayer1Roughness "[1.000000 1.000000 1.000000 0.000000]"

\t//---- Texture Address Mode ----
\tg_nTextureAddressModeU "0" // Wrap
\tg_nTextureAddressModeV "0" // Wrap

{sysattr}\tVariableState
\t{{
\t\t"Color"
\t\t{{
\t\t}}
\t\t"Fog"
\t\t{{
\t\t}}
\t\t"Lighting"
\t\t{{
\t\t\t"Metalness" 0
\t\t}}
\t\t"PBR 1"
\t\t{{
\t\t\t"Albedo" 0
\t\t\t"Normal" 0
\t\t\t"Roughness" 0
\t\t\t"Ambient Occlusion" 0
\t\t}}
\t\t"Texture Address Mode"
\t\t{{
\t\t}}
\t}}
}}
"""

TMPL_WITH_NORMAL = """\
// THIS FILE IS AUTO-GENERATED

Layer0
{{
\tshader "csgo_lightmappedgeneric.vfx"

\t//---- Color ----
\tg_flModelTintAmount "1.000"
\tg_flVertexColorOpacityScale "1.000"
\tg_vColorTint "[1.000000 1.000000 1.000000 0.000000]"

\t//---- Fog ----
\tg_bFogEnabled "1"

\t//---- Lighting ----
\tg_flMetalness "0.000"

\t//---- PBR 1 ----
\tg_vLayer1Tint "[1.000000 1.000000 1.000000 0.000000]"
\tTextureLayer1AmbientOcclusion "materials/default/default_ao.tga"
\tTextureLayer1Color "{color}"
\tTextureLayer1Normal "{normal}"
\tTextureLayer1Roughness "[1.000000 1.000000 1.000000 0.000000]"

\t//---- Texture Address Mode ----
\tg_nTextureAddressModeU "0" // Wrap
\tg_nTextureAddressModeV "0" // Wrap

{sysattr}\tVariableState
\t{{
\t\t"Color"
\t\t{{
\t\t}}
\t\t"Fog"
\t\t{{
\t\t}}
\t\t"Lighting"
\t\t{{
\t\t\t"Metalness" 0
\t\t}}
\t\t"PBR 1"
\t\t{{
\t\t\t"Albedo" 0
\t\t\t"Normal" 0
\t\t\t"Roughness" 0
\t\t\t"Ambient Occlusion" 0
\t\t}}
\t\t"Texture Address Mode"
\t\t{{
\t\t}}
\t}}
}}
"""

TMPL_TRANSLUCENT = """\
// THIS FILE IS AUTO-GENERATED

Layer0
{{
\tshader "csgo_lightmappedgeneric.vfx"

\t//---- Translucent ----
\tF_TRANSLUCENT 1

\t//---- Color ----
\tg_flModelTintAmount "1.000"
\tg_flVertexColorOpacityScale "1.000"
\tg_vColorTint "[1.000000 1.000000 1.000000 0.000000]"

\t//---- Fog ----
\tg_bFogEnabled "1"

\t//---- Lighting ----
\tg_flMetalness "0.000"

\t//---- PBR 1 ----
\tg_vLayer1Tint "[1.000000 1.000000 1.000000 0.000000]"
\tTextureLayer1AmbientOcclusion "materials/default/default_ao.tga"
\tTextureLayer1Color "{color}"
\tTextureLayer1Normal "{normal}"
\tTextureLayer1Roughness "[1.000000 1.000000 1.000000 0.000000]"
\tTextureLayer1Translucency "{transculent}"

\t//---- Texture Address Mode ----
\tg_nTextureAddressModeU "0" // Wrap
\tg_nTextureAddressModeV "0" // Wrap

\t//---- Translucent ----
\tg_flOpacityScale "1.000"

{sysattr}\tVariableState
\t{{
\t\t"Color"
\t\t{{
\t\t}}
\t\t"Fog"
\t\t{{
\t\t}}
\t\t"Lighting"
\t\t{{
\t\t\t"Metalness" 0
\t\t}}
\t\t"PBR 1"
\t\t{{
\t\t\t"Albedo" 0
\t\t\t"Albedo Translucency" 0
\t\t\t"Normal" 0
\t\t\t"Roughness" 0
\t\t\t"Ambient Occlusion" 0
\t\t}}
\t\t"Texture Address Mode"
\t\t{{
\t\t}}
\t\t"Translucent"
\t\t{{
\t\t}}
\t}}
}}
"""

TMPL_DECAL = """\
// THIS FILE IS AUTO-GENERATED

Layer0
{{
\tshader "csgo_static_overlay.vfx"

\t//---- Lighting ----
\tF_LIT 1

\t//---- Ambient Occlusion ----
\tTextureAmbientOcclusion "materials/default/default_ao.tga"

\t//---- Color ----
\tg_flModelTintAmount "1.000"
\tg_flTexCoordRotation "0.000"
\tg_fTextureColorBrightness "1.000"
\tg_fTextureColorContrast "1.000"
\tg_fTextureColorSaturation "1.000"
\tg_nScaleTexCoordUByModelScaleAxis "0" // None
\tg_nScaleTexCoordVByModelScaleAxis "0" // None
\tg_vColorTint "[1.000000 1.000000 1.000000 0.000000]"
\tg_vTexCoordCenter "[0.500 0.500]"
\tg_vTexCoordOffset "[0.000 0.000]"
\tg_vTexCoordScale "[1.000 1.000]"
\tg_vTexCoordScrollSpeed "[0.000 0.000]"
\tg_vTextureColorCorrectionTint "[1.000000 1.000000 1.000000 0.000000]"
\tTextureColor "{color}"

\t//---- Fog ----
\tg_bFogEnabled "1"

\t//---- Lighting ----
\tg_fTextureRoughnessBrightness "1.000"
\tg_fTextureRoughnessContrast "1.000"
\tTextureMetalness "materials/default/default_metal.tga"
\tTextureRoughness "[1.000000 1.000000 1.000000 0.000000]"

\t//---- Normal Map ----
\tg_fTextureNormalContrast "1.000"
\tTextureNormal "materials/default/default_normal.tga"

\t//---- Self Illum ----
\tg_flSelfIllumAlbedoFactor "1.000"
\tg_flSelfIllumBrightness "0.000"
\tg_flSelfIllumScale "1.000"
\tg_vSelfIllumScrollSpeed "[0.000 0.000]"
\tg_vSelfIllumTint "[1.000000 1.000000 1.000000 0.000000]"
\tTextureSelfIllumMask "materials/default/default_selfillum.tga"

\t//---- Texture Address Mode ----
\tg_nTextureAddressModeU "0" // Wrap
\tg_nTextureAddressModeV "0" // Wrap

\tVariableState
\t{{
\t\t"Ambient Occlusion"
\t\t{{
\t\t}}
\t\t"Color"
\t\t{{
\t\t\t"Color Correction" 0
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

TMPL_DECAL_TRANSLUCENT = """\
// THIS FILE IS AUTO-GENERATED

Layer0
{{
\tshader "csgo_static_overlay.vfx"

\t//---- Blend Mode ----
\tF_BLEND_MODE 1 // Translucent

\t//---- Lighting ----
\tF_LIT 1

\t//---- Ambient Occlusion ----
\tTextureAmbientOcclusion "materials/default/default_ao.tga"

\t//---- Color ----
\tg_flModelTintAmount "1.000"
\tg_flTexCoordRotation "0.000"
\tg_fTextureColorBrightness "1.000"
\tg_fTextureColorContrast "1.000"
\tg_fTextureColorSaturation "1.000"
\tg_nScaleTexCoordUByModelScaleAxis "0" // None
\tg_nScaleTexCoordVByModelScaleAxis "0" // None
\tg_vColorTint "[1.000000 1.000000 1.000000 0.000000]"
\tg_vTexCoordCenter "[0.500 0.500]"
\tg_vTexCoordOffset "[0.000 0.000]"
\tg_vTexCoordScale "[1.000 1.000]"
\tg_vTexCoordScrollSpeed "[0.000 0.000]"
\tg_vTextureColorCorrectionTint "[1.000000 1.000000 1.000000 0.000000]"
\tTextureColor "{color}"

\t//---- Fog ----
\tg_bFogEnabled "1"

\t//---- Lighting ----
\tg_fTextureRoughnessBrightness "1.000"
\tg_fTextureRoughnessContrast "1.000"
\tTextureMetalness "materials/default/default_metal.tga"
\tTextureRoughness "[1.000000 1.000000 1.000000 0.000000]"

\t//---- Normal Map ----
\tg_fTextureNormalContrast "1.000"
\tTextureNormal "materials/default/default_normal.tga"

\t//---- Self Illum ----
\tg_flSelfIllumAlbedoFactor "1.000"
\tg_flSelfIllumBrightness "0.000"
\tg_flSelfIllumScale "1.000"
\tg_vSelfIllumScrollSpeed "[0.000 0.000]"
\tg_vSelfIllumTint "[1.000000 1.000000 1.000000 0.000000]"
\tTextureSelfIllumMask "materials/default/default_selfillum.tga"

\t//---- Texture Address Mode ----
\tg_nTextureAddressModeU "0" // Wrap
\tg_nTextureAddressModeV "0" // Wrap

\t//---- Translucent ----
\tg_flOpacityScale "1.000"
\tTextureTranslucency "{transculent}"

\tVariableState
\t{{
\t\t"Ambient Occlusion"
\t\t{{
\t\t}}
\t\t"Color"
\t\t{{
\t\t\t"Color Correction" 0
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
\t\t"Translucent"
\t\t{{
\t\t}}
\t}}
}}
"""

TMPL_GLASS_TRANSLUCENT = """\
// THIS FILE IS AUTO-GENERATED

Layer0
{{
\tshader "csgo_lightmappedgeneric.vfx"

\t//---- Translucent ----
\tF_TRANSLUCENT 1

\t//---- Color ----
\tg_flModelTintAmount "1.000"
\tg_flVertexColorOpacityScale "1.000"
\tg_vColorTint "[1.000000 1.000000 1.000000 0.000000]"

\t//---- Fog ----
\tg_bFogEnabled "1"

\t//---- Lighting ----
\tg_flMetalness "0.000"

\t//---- PBR 1 ----
\tg_vLayer1Tint "[1.000000 1.000000 1.000000 0.000000]"
\tTextureLayer1AmbientOcclusion "materials/default/default_ao.tga"
\tTextureLayer1Color "{color}"
\tTextureLayer1Normal "materials/default/default_normal.tga"
\tTextureLayer1Roughness "materials/default/default_rough.tga"
\tTextureLayer1Translucency "{transculent}"

\t//---- Texture Address Mode ----
\tg_nTextureAddressModeU "0" // Wrap
\tg_nTextureAddressModeV "0" // Wrap

\t//---- Translucent ----
\tg_flOpacityScale "1.000"

\tSystemAttributes
\t{{
\t\tPhysicsSurfaceProperties "glass"
\t}}
}}
"""

TMPL_GLASS_OPAQUE = """\
// THIS FILE IS AUTO-GENERATED

Layer0
{{
\tshader "csgo_lightmappedgeneric.vfx"

\t//---- Translucent ----
\tF_TRANSLUCENT 1

\t//---- Color ----
\tg_flModelTintAmount "1.000"
\tg_flVertexColorOpacityScale "1.000"
\tg_vColorTint "[1.000000 1.000000 1.000000 0.000000]"

\t//---- Fog ----
\tg_bFogEnabled "1"

\t//---- Lighting ----
\tg_flMetalness "0.000"

\t//---- PBR 1 ----
\tg_vLayer1Tint "[1.000000 1.000000 1.000000 0.000000]"
\tTextureLayer1AmbientOcclusion "materials/default/default_ao.tga"
\tTextureLayer1Color "{color}"
\tTextureLayer1Normal "materials/default/default_normal.tga"
\tTextureLayer1Roughness "[1.000000 1.000000 1.000000 0.000000]"
\tTextureLayer1Translucency "[0.500000 0.500000 0.500000 0.000000]"

\t//---- Texture Address Mode ----
\tg_nTextureAddressModeU "0" // Wrap
\tg_nTextureAddressModeV "0" // Wrap

\t//---- Translucent ----
\tg_flOpacityScale "1.000"

\tSystemAttributes
\t{{
\t\tPhysicsSurfaceProperties "glass"
\t}}


\tVariableState
\t{{
\t\t"Color"
\t\t{{
\t\t}}
\t\t"Fog"
\t\t{{
\t\t}}
\t\t"Lighting"
\t\t{{
\t\t\t"Metalness" 0
\t\t}}
\t\t"PBR 1"
\t\t{{
\t\t\t"Albedo" 0
\t\t\t"Albedo Translucency" 0
\t\t\t"Normal" 0
\t\t\t"Roughness" 0
\t\t\t"Ambient Occlusion" 0
\t\t}}
\t\t"Texture Address Mode"
\t\t{{
\t\t}}
\t\t"Translucent"
\t\t{{
\t\t}}
\t}}
}}
"""

TMPL_WORLDVERTEX = """\
// THIS FILE IS AUTO-GENERATED

Layer0
{{
\tshader "csgo_lightmappedgeneric.vfx"

\t//---- Number of Additional Layers ----
\tF_LAYERS 1 // WorldVertexTransition

\t//---- Color ----
\tg_flModelTintAmount "1.000"
\tg_flVertexColorOpacityScale "1.000"
\tg_vColorTint "[1.000000 1.000000 1.000000 0.000000]"

\t//---- Fog ----
\tg_bFogEnabled "1"

\t//---- Lighting ----
\tg_flMetalness "0.000"

\t//---- PBR 1 ----
\tg_vLayer1Tint "[1.000000 1.000000 1.000000 0.000000]"
\tTextureLayer1AmbientOcclusion "materials/default/default_ao.tga"
\tTextureLayer1Color "{color1}"
\tTextureLayer1Normal "{normal1}"
\tTextureLayer1Roughness "[1.000000 1.000000 1.000000 0.000000]"

\t//---- PBR 2 ----
\tg_vLayer2Tint "[1.000000 1.000000 1.000000 0.000000]"
\tTextureLayer2AmbientOcclusion "materials/default/default_ao.tga"
\tTextureLayer2Color "{color2}"
\tTextureLayer2Normal "{normal2}"
\tTextureLayer2Roughness "[1.000000 1.000000 1.000000 0.000000]"

\t//---- Texture Address Mode ----
\tg_nTextureAddressModeU "0" // Wrap
\tg_nTextureAddressModeV "0" // Wrap

{sysattr}}}
"""

TMPL_UNLIT = """\
// THIS FILE IS AUTO-GENERATED

Layer0
{{
\tshader "csgo_unlitgeneric.vfx"

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

\t//---- Texture Address Mode ----
\tg_nTextureAddressModeU "0" // Wrap
\tg_nTextureAddressModeV "0" // Wrap

\tUnusedVariables
\t{{
\t\t"TextureTranslucency" ""
\t}}
}}
"""

TMPL_UNLIT_TRANSLUCENT = """\
// THIS FILE IS AUTO-GENERATED

Layer0
{{
\tshader "csgo_unlitgeneric.vfx"

\t//---- Blend Mode ----
\tF_BLEND_MODE 1 // Translucent

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

\t//---- Texture Address Mode ----
\tg_nTextureAddressModeU "0" // Wrap
\tg_nTextureAddressModeV "0" // Wrap

\t//---- Translucent ----
\tTextureTranslucency "{transculent}"
}}
"""

def _folder_contains(root, keyword):
    rel = os.path.relpath(root, BASE_DIR).replace("\\", "/").lower()
    segments = rel.replace(".", "/").split("/")
    return any(_keyword_hit(seg, keyword) for seg in segments)

def is_decal(root, fname):
    if _folder_contains(root, "decal"):
        return True
    return _keyword_hit(os.path.splitext(fname)[0].lower(), "decal")

def is_glass(root, fname):
    if _folder_contains(root, "glass"):
        return True
    return _keyword_hit(os.path.splitext(fname)[0].lower(), "glass")


SHADER_MAP = {
    "lightmappedgeneric":    "lightmapped",
    "vertexlitgeneric":      "lightmapped",
    "worldvertextransition": "worldvertex",
    "unlitgeneric":          "unlit",
}

def classify_shader(shader_str):
    s = shader_str.lower().strip()
    for key, val in SHADER_MAP.items():
        if key in s:
            return val
    return "lightmapped"


def parse_texture_info(path):
    entries = []
    current_label = None
    current_data  = {}
    in_block      = False

    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.rstrip()
            stripped = line.strip()

            if stripped.startswith("//"):
                continue

            label_match = _re.match(r'^"([^"]+)"$', stripped)
            if label_match:
                current_label = label_match.group(1)
                current_data  = {}
                in_block      = False
                continue

            if stripped == "{" and current_label:
                in_block = True
                continue

            if stripped == "}" and in_block:
                entries.append((current_label, current_data))
                in_block      = False
                current_label = None
                current_data  = {}
                continue

            if in_block and "=" in stripped:
                kv = stripped.split("=", 1)
                key = kv[0].strip()
                val = kv[1].strip()
                val = val.split("//")[0].strip()
                val = val.strip('"')
                current_data[key.lower()] = val

    return entries


def resolve_texture_path(rel_path):
    rel_path = rel_path.strip()

    search_bases = [rel_path]
    if rel_path.lower().startswith("materials/"):
        search_bases.append(rel_path[len("materials/"):])

    # texture_info.txt usually gives paths without an extension (e.g. ".../concretewall011c").
    # Try it as it is first, then add the common texture extensions one by one.
    extensions = ["", ".tga", ".png", ".jpg", ".jpeg", ".bmp"]

    last_candidate = os.path.join(BASE_DIR, rel_path)
    for base in search_bases:
        root, ext = os.path.splitext(base)
        # if it already has an extension, try that first
        ext_order = [ext] + [e for e in extensions if e != ext] if ext else extensions
        for try_ext in ext_order:
            candidate = os.path.join(BASE_DIR, root + try_ext if try_ext else base)
            last_candidate = candidate
            if os.path.exists(candidate):
                return candidate

    return last_candidate


def convert_from_txt(entries, stats):
    created_vmat_contents = {}
    processed_textures = set()

    for label, data in entries:
        shader_raw  = data.get("shader", "Unknown")
        surface     = data.get("surface properties", None)
        shader_type = classify_shader(shader_raw)

        color1   = data.get("base texture", "materials/default/default_color.tga").split("//")[0].strip()
        color2   = data.get("base texture 2", "materials/default/default_color.tga").split("//")[0].strip()
        normal1  = data.get("normal map", "materials/default/default_normal.tga").split("//")[0].strip()
        normal2  = data.get("normal map 2", "materials/default/default_normal.tga").split("//")[0].strip()

        # Paths in texture_info.txt usually have no extension (e.g. ".../concretewall011c").
        # resolve_texture_path() finds the real file (with extension) on disk; the path is
        # rebuilt from that file and color1/color2/normal1/normal2 are updated,
        # otherwise a path without an extension was written to the .vmat and the file was not found.
        def _resolve_and_fix(raw_path, default_path):
            if raw_path == default_path:
                return raw_path
            abs_p = resolve_texture_path(raw_path)
            if os.path.exists(abs_p):
                return make_relative_path(abs_p)
            # If the file is not on disk and has no extension, add .tga by default
            if not os.path.splitext(raw_path)[1]:
                return raw_path + ".tga"
            return raw_path

        color1  = _resolve_and_fix(color1,  "materials/default/default_color.tga")
        color2  = _resolve_and_fix(color2,  "materials/default/default_color.tga")
        normal1 = _resolve_and_fix(normal1, "materials/default/default_normal.tga")
        normal2 = _resolve_and_fix(normal2, "materials/default/default_normal.tga")

        abs_color1 = resolve_texture_path(color1)
        if os.path.exists(abs_color1):
            processed_textures.add(os.path.normcase(os.path.abspath(abs_color1)))
        transparent = has_transparency(abs_color1) if os.path.exists(abs_color1) else False
        transculent_path = None
        if transparent:
            transculent_abs = create_translucent_texture(abs_color1)
            if transculent_abs:
                transculent_path = make_relative_path(transculent_abs)
            else:
                transparent = False

        sysattr = build_system_attributes(surface if surface and surface != "default" else None)
        root_dir = os.path.dirname(abs_color1)

        if shader_type == "worldvertex":
            content = TMPL_WORLDVERTEX.format(
                color1=color1, normal1=normal1,
                color2=color2, normal2=normal2,
                sysattr=sysattr,
            )
            type_tag = "worldvertex"
        elif is_glass(root_dir, label):
            if transparent and transculent_path:
                content = TMPL_GLASS_TRANSLUCENT.format(color=color1, transculent=transculent_path)
                type_tag = "glass + translucent"
            else:
                content = TMPL_GLASS_OPAQUE.format(color=color1)
                type_tag = "glass (opaque)"
        elif is_decal(root_dir, label):
            if transparent and transculent_path:
                content = TMPL_DECAL_TRANSLUCENT.format(color=color1, transculent=transculent_path)
                type_tag = "decal + translucent"
            else:
                content = TMPL_DECAL.format(color=color1)
                type_tag = "decal (opaque)"
        elif shader_type == "unlit":
            if transparent and transculent_path:
                content  = TMPL_UNLIT_TRANSLUCENT.format(color=color1, transculent=transculent_path)
                type_tag = "unlit + translucent"
            else:
                content  = TMPL_UNLIT.format(color=color1)
                type_tag = "unlit"
        else:
            if transparent and transculent_path:
                content = TMPL_TRANSLUCENT.format(
                    color=color1, normal=normal1,
                    transculent=transculent_path, sysattr=sysattr,
                )
                type_tag = "lightmapped + trans"
            elif normal1 and normal1 != "materials/default/default_normal.tga":
                content  = TMPL_WITH_NORMAL.format(color=color1, normal=normal1, sysattr=sysattr)
                type_tag = "lightmapped + normal"
            else:
                content  = TMPL_BASIC.format(color=color1, sysattr=sysattr)
                type_tag = "lightmapped basic"

        clean_label = label[:-5] if label.lower().endswith(".vmat") else label
        vmat_filename = clean_label + ".vmat"

        vmat_path_raw = data.get("vmat path", "")
        if vmat_path_raw:
            vmat_dir_from_txt = os.path.dirname(vmat_path_raw)
            candidate_dir = os.path.join(BASE_DIR, vmat_dir_from_txt)
            if not os.path.isdir(candidate_dir):
                stripped_dir = vmat_dir_from_txt
                if stripped_dir.lower().startswith("materials/"):
                    stripped_dir = stripped_dir[len("materials/"):]
                candidate_dir = os.path.join(BASE_DIR, stripped_dir)
            out_dir = candidate_dir
        elif "materials/" in color1.lower():
            parts = color1.replace("\\", "/").split("/")
            if len(parts) >= 3:
                out_dir = os.path.join(BASE_DIR, parts[0], parts[1])
            else:
                out_dir = os.path.join(BASE_DIR, os.path.dirname(color1))
        else:
            out_dir = os.path.join(BASE_DIR, "materials", "converted")

        vmat_out_path = os.path.join(out_dir, vmat_filename)
        os.makedirs(out_dir, exist_ok=True)

        if vmat_out_path in created_vmat_contents and created_vmat_contents[vmat_out_path] == content:
            print(f"  [TXT] {label} -> skipped (duplicate, already created at same path with same content).")
            continue

        created_vmat_contents[vmat_out_path] = content
        with open(vmat_out_path, "w", encoding="utf-8") as f:
            f.write(content)

        print(f"  [TXT] {label} -> created as {type_tag}.")
        stats["ok"] += 1

    return processed_textures


def convert_file(root, fname, stats, skip_set=None):
    ext = os.path.splitext(fname)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".tga"):
        return

    abs_path_check = os.path.normcase(os.path.abspath(os.path.join(root, fname)))
    if skip_set and abs_path_check in skip_set:
        print(f"  skip  [already done via texture_info.txt]  {fname}")
        return

    base_noext = os.path.splitext(fname)[0]
    base_lower = base_noext.lower()

    if "tooltexture" in base_lower:
        return
        
    if is_normal_map_file(fname):
        print(f"  skip  [normal map]  {fname}")
        return
    base_noext = os.path.splitext(fname)[0]
    if base_noext.endswith("_transculent"):
        return
    base_lower = base_noext.lower()
    if base_lower.endswith("_spec") or base_lower.endswith("_mask"):
        print(f"  skip  [spec/mask]   {fname}")
        return

    abs_path   = os.path.join(root, fname)
    color_path = make_relative_path(abs_path)
    rel_display = os.path.relpath(abs_path, BASE_DIR)
    print(f"\n  Processing  {rel_display}")

    normal_map_abs = find_normal_map(root, base_noext)
    normal_path    = make_relative_path(normal_map_abs) if normal_map_abs else "materials/default/default_normal.tga"
    if normal_map_abs:
        print(f"    Normal map   {os.path.basename(normal_map_abs)}")

    transparent      = has_transparency(abs_path)
    transculent_path = None
    if transparent:
        transculent_abs = create_translucent_texture(abs_path)
        if transculent_abs:
            transculent_path = make_relative_path(transculent_abs)
            print(f"    Translucency  {os.path.basename(transculent_abs)}")
        else:
            transparent = False
            print(f"    Translucency  skipped (nearly opaque)")

    surface = detect_surface(os.path.join(root, fname))
    sysattr = build_system_attributes(surface)

    if is_glass(root, fname):
        if transparent and transculent_path:
            content  = TMPL_GLASS_TRANSLUCENT.format(color=color_path, transculent=transculent_path)
            type_tag = "glass + translucent"
        else:
            content  = TMPL_GLASS_OPAQUE.format(color=color_path)
            type_tag = "glass (opaque)"
    elif is_decal(root, fname):
        if transparent and transculent_path:
            content  = TMPL_DECAL_TRANSLUCENT.format(color=color_path, transculent=transculent_path)
            type_tag = "decal + translucent"
        else:
            content  = TMPL_DECAL.format(color=color_path)
            type_tag = "decal (opaque)"
    elif transparent and transculent_path:
        content  = TMPL_TRANSLUCENT.format(color=color_path, normal=normal_path,
                                           transculent=transculent_path, sysattr=sysattr)
        type_tag = "translucent"
    elif normal_map_abs:
        content  = TMPL_WITH_NORMAL.format(color=color_path, normal=normal_path, sysattr=sysattr)
        type_tag = "with normal map"
    else:
        content  = TMPL_BASIC.format(color=color_path, sysattr=sysattr)
        type_tag = "basic"

    print(f"    Type         {type_tag}  |  surface  {surface or 'default'}")
    vmat_path = os.path.join(root, base_noext + ".vmat")
    with open(vmat_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"    Output       {os.path.basename(vmat_path)}")
    stats["ok"] += 1


def walk_and_convert(base, stats, skip_set=None):
    all_files = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith("_temp")]
        for fname in sorted(files):
            ext = os.path.splitext(fname)[1].lower()
            if ext in (".png", ".jpg", ".jpeg", ".bmp", ".tga"):
                all_files.append((root, fname))

    print(f"  Found {len(all_files)} texture file(s) to scan\n")

    current_folder = None
    for root, fname in all_files:
        folder_rel = os.path.relpath(root, base)
        if folder_rel != current_folder:
            current_folder = folder_rel
            display = folder_rel if folder_rel != "." else "(root)"
            print(f"\n  Folder  {display}")
            print("  " + "." * min(len(display) + 8, 52))
        convert_file(root, fname, stats, skip_set)

    tmp_dir = os.path.join(BASE_DIR, "_temp_fixed_vmat")
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)

    return stats


def set_base_dir(base_dir):
    """CS2 Porter: sets the folder the script works in (os.getcwd() in the original)."""
    global BASE_DIR, TEXTURE_INFO_FILE
    BASE_DIR = os.path.abspath(base_dir)
    TEXTURE_INFO_FILE = os.path.join(BASE_DIR, "Missing Textures", "texture_info.txt")


def main(base_dir=None):
    if base_dir:
        set_base_dir(base_dir)
    W = 55
    print()
    print("=" * W)
    print("  VMAT Converter".center(W))
    print("=" * W)
    print(f"  Base dir  {BASE_DIR}")
    print("=" * W)

    t0    = time.time()
    stats = {"ok": 0}

    txt_path = TEXTURE_INFO_FILE
    if not os.path.exists(txt_path) and os.path.exists(os.path.join(BASE_DIR, "texture_info.txt")):
        txt_path = os.path.join(BASE_DIR, "texture_info.txt")

    if os.path.exists(txt_path):
        entries = parse_texture_info(txt_path)
        processed_textures = convert_from_txt(entries, stats)
        walk_and_convert(BASE_DIR, stats, skip_set=processed_textures)
    else:
        walk_and_convert(BASE_DIR, stats)

    elapsed = round(time.time() - t0, 2)
    print()
    print("=" * W)
    print(f"  DONE".center(W))
    print("=" * W)
    print(f"  VMAT files created   {stats['ok']}")
    print(f"  Time elapsed         {elapsed}s")
    print("=" * W)
    print()


if __name__ == "__main__":
    main()