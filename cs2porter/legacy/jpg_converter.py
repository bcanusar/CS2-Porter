import os
import re
import shutil
import string
import random
import zipfile
import tempfile
from collections import defaultdict

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ASSETS_SUBFOLDER = "assets"
TEXTURE_EXT      = {".jpg", ".jpeg", ".png", ".tga", ".bmp"}
MAX_PATH         = 260
_WIN_PREFIX      = 140
MAX_GAME_PATH    = MAX_PATH - _WIN_PREFIX - 20   # ~100 char

# Texture slot definitions
TEXTURE_SLOTS: list[tuple[list[str], str, int]] = [
    (["_basecolor", "_base_color", "_albedo", "_diffuse", "_color", "_col", "_bc", "_diff"],
     "color", 0),
    (["_normalmap", "_normal", "_norm", "_nrm", "_nor", "_ddna", "_nm"],
     "normal", 0),
    (["_roughness", "_rougness", "_rough", "_rgh"],
     "roughness", 0),
    (["_glossiness", "_gloss"],
     "roughness", 1),
    (["_ambientocclusion", "_ambient_occlusion", "_occlusion", "_ao"],
     "ao", 0),
    (["_cavity", "_fuzz"],
     "ao", 1),
    (["_opacity"],
     "translucency", 0),
    (["_translucency", "_trans", "_alpha", "_mask"],
     "translucency", 1),
]

SURFACE_KEYWORDS = [
    (["glass", "cam", "pencere", "window"],               "glass"),
    (["plaster", "siva", "stucco"],                       "plaster"),
    (["concrete", "beton", "cement"],                     "concrete"),
    (["brick", "tugla"],                                  "brick"),
    (["metal", "grate", "vent", "steel", "iron",
      "aluminum", "aluminium", "galv"],                   "metal"),
    (["wood", "ahsap", "tahta", "plank", "crate",
      "lumber", "parquet", "oak", "pine"],                "Wood"),
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(m):  print(m)
def _ok(m):   print(f"  OK      {m}")
def _info(m): print(f"  INFO    {m}")
def _warn(m): print(f"  WARN    {m}")
def _err(m):  print(f"  ERROR   {m}")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rnd(n=4):
    return "".join(random.choices(string.ascii_lowercase, k=n))


def _kw_hit(text, kw):
    return bool(re.search(r'(?<![a-z])' + re.escape(kw), text))


def detect_surface(name: str) -> str | None:
    low = name.lower()
    for keywords, surface in SURFACE_KEYWORDS:
        for kw in keywords:
            if _kw_hit(low, kw):
                return surface
    return None


def shorten_name(name: str, max_len: int) -> str:
    if len(name) <= max_len:
        return name
    keep = max(4, max_len - 5)
    short = name[:keep] + "_" + _rnd(4)
    _warn(f"Name too long, shortened: '{name}' -> '{short}'")
    return short


# ---------------------------------------------------------------------------
# Texture classification
# ---------------------------------------------------------------------------

def classify_texture(path: str) -> tuple[str, int, int] | None:
    base = os.path.splitext(os.path.basename(path))[0].lower()
    best = None
    for suffixes, slot, tier in TEXTURE_SLOTS:
        for suf in suffixes:
            if base.endswith(suf):
                suf_len = len(suf)
                if best is None or tier < best[1] or (tier == best[1] and suf_len > best[2]):
                    best = (slot, tier, suf_len)
    return best


def select_slots(textures: list[str]) -> dict[str, str]:
    candidates: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for tex in textures:
        r = classify_texture(tex)
        if r:
            slot, tier, suf_len = r
            candidates[slot].append((tier, suf_len, tex))

    selected: dict[str, str] = {}
    for slot, cands in candidates.items():
        _, _, best = sorted(cands, key=lambda x: (x[0], -x[1]))[0]
        selected[slot] = best
        others = [c[2] for c in cands if c[2] != best]
        if others:
            _info(f"Slot conflict '{slot}': picked '{os.path.basename(best)}', "
                  f"skipped: {[os.path.basename(o) for o in others]}")
    return selected


# ---------------------------------------------------------------------------
# Opacity greyscale conversion
# ---------------------------------------------------------------------------

def _is_greyscale(img: Image.Image, threshold: float = 0.01) -> bool:
    arr = np.array(img.convert("RGB"), dtype=np.float32)
    r, g, b = arr[:,:,0], arr[:,:,1], arr[:,:,2]
    return (np.abs(r-g).mean()/255 <= threshold and
            np.abs(r-b).mean()/255 <= threshold)


def convert_opacity_to_grey(src: str, dst: str) -> bool:
    try:
        img = Image.open(src)
    except Exception as e:
        _warn(f"Could not open opacity ({os.path.basename(src)}): {e}")
        return False

    if img.mode == "RGBA":
        if _is_greyscale(img.convert("RGB")):
            shutil.copy2(src, dst)
            _info(f"Opacity already greyscale, copied: {os.path.basename(dst)}")
            return True
        arr = np.array(img, dtype=np.float32)
        alpha = arr[:,:,3] / 255.0
        brightness = (alpha * 255).astype(np.uint8)
        rgb = np.stack([brightness, brightness, brightness], axis=2)
        result = Image.fromarray(rgb, "RGB")
    else:
        img_rgb = img.convert("RGB")
        if _is_greyscale(img_rgb):
            shutil.copy2(src, dst)
            _info(f"Opacity already greyscale, copied: {os.path.basename(dst)}")
            return True
        img_grey = img_rgb.convert("L")
        arr = np.array(img_grey, dtype=np.uint8)
        rgb = np.stack([arr, arr, arr], axis=2)
        result = Image.fromarray(rgb, "RGB")

    try:
        result.save(dst)
        _ok(f"Opacity converted to greyscale: {os.path.basename(dst)}")
        return True
    except Exception as e:
        _warn(f"Could not save opacity: {e}")
        return False


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def _strip_suffix(stem: str) -> str:
    known: list[str] = []
    for suffixes, _, _ in TEXTURE_SLOTS:
        known.extend(suffixes)
    known.sort(key=len, reverse=True)
    low = stem.lower()
    for suf in known:
        if low.endswith(suf):
            return stem[:-len(suf)]
    return stem


def group_textures(paths: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for p in paths:
        stem = os.path.splitext(os.path.basename(p))[0]
        root = _strip_suffix(stem) or stem
        groups[root].append(p)
    return dict(groups)


# ---------------------------------------------------------------------------
# ZIP extraction
# ---------------------------------------------------------------------------

def extract_zip(zip_path: str) -> tuple[str, list[str]]:
    """Extracts the ZIP into a temp folder and returns the texture paths inside it."""
    tmp = tempfile.mkdtemp(prefix="jpg_vmat_")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(tmp)
    textures = []
    for root, _, files in os.walk(tmp):
        for f in files:
            if os.path.splitext(f)[1].lower() in TEXTURE_EXT:
                textures.append(os.path.join(root, f))
    return tmp, textures


# ---------------------------------------------------------------------------
# VMAT templates
# ---------------------------------------------------------------------------

def build_vmat_texture(slot_map: dict[str, str], has_trans: bool,
                       surface: str | None) -> str:
    """csgo_lightmappedgeneric.vfx: normal texture."""
    T = "\t"
    ao     = slot_map.get("ao",     "materials/default/default_ao.tga")
    color  = slot_map.get("color",  "")
    normal = slot_map.get("normal", "materials/default/default_normal.tga")
    trans  = slot_map.get("translucency", "")

    lines = [
        "// THIS FILE IS AUTO-GENERATED",
        "",
        "Layer0",
        "{",
        f'\tshader "csgo_lightmappedgeneric.vfx"',
        "",
    ]

    if has_trans and trans:
        lines += [f"{T}//---- Translucent ----", f"{T}F_TRANSLUCENT 1", ""]

    lines += [
        f"{T}//---- Color ----",
        f'{T}g_flModelTintAmount "1.000"',
        f'{T}g_flVertexColorOpacityScale "1.000"',
        f'{T}g_vColorTint "[1.000000 1.000000 1.000000 0.000000]"',
        "",
        f"{T}//---- Fog ----",
        f'{T}g_bFogEnabled "1"',
        "",
        f"{T}//---- Lighting ----",
        f'{T}g_flMetalness "0.000"',
        "",
        f"{T}//---- PBR 1 ----",
        f'{T}g_vLayer1Tint "[1.000000 1.000000 1.000000 0.000000]"',
        f'{T}TextureLayer1AmbientOcclusion "{ao}"',
    ]
    if color:
        lines.append(f'{T}TextureLayer1Color "{color}"')
    lines.append(f'{T}TextureLayer1Normal "{normal}"')
    lines.append(f'{T}TextureLayer1Roughness "[1.000000 1.000000 1.000000 0.000000]"')
    if has_trans and trans:
        lines.append(f'{T}TextureLayer1Translucency "{trans}"')

    lines += [
        "",
        f"{T}//---- Texture Address Mode ----",
        f'{T}g_nTextureAddressModeU "0" // Wrap',
        f'{T}g_nTextureAddressModeV "0" // Wrap',
    ]

    if has_trans and trans:
        lines += [
            "",
            f"{T}//---- Translucent ----",
            f'{T}g_flOpacityScale "1.000"',
        ]

    if surface:
        lines += [
            "",
            f"{T}SystemAttributes",
            f"{T}{{",
            f'{T}\tPhysicsSurfaceProperties "{surface}"',
            f"{T}}}",
        ]

    pbr = [
        f'{T*2}"Albedo" 0',
        f'{T*2}"Normal" 0',
        f'{T*2}"Roughness" 0',
        f'{T*2}"Ambient Occlusion" 0',
    ]
    if has_trans and trans:
        pbr.insert(1, f'{T*2}"Albedo Translucency" 0')

    lines += [
        "",
        f"{T}VariableState",
        f"{T}{{",
        f'{T}"Color"',
        f"{T}{{",
        f"{T}}}",
        f'{T}"Fog"',
        f"{T}{{",
        f"{T}}}",
        f'{T}"Lighting"',
        f"{T}{{",
        f'{T}\t"Metalness" 0',
        f"{T}}}",
        f'{T}"PBR 1"',
        f"{T}{{",
    ]
    lines.extend(pbr)
    lines += [
        f"{T}}}",
        f'{T}"Texture Address Mode"',
        f"{T}{{",
        f"{T}}}",
    ]
    if has_trans and trans:
        lines += [
            f'{T}"Translucent"',
            f"{T}{{",
            f"{T}}}",
        ]
    lines += [f"{T}}}", "}", ""]
    return "\n".join(lines)


def build_vmat_decal(slot_map: dict[str, str], has_trans: bool) -> str:
    """csgo_static_overlay.vfx: decal. (FIXED)"""
    T = "\t"
    # FIX: the ao and normal slots now come from the map dynamically, defaults otherwise.
    ao     = slot_map.get("ao",     "materials/default/default_ao.tga")
    color  = slot_map.get("color",  "")
    normal = slot_map.get("normal", "materials/default/default_normal.tga")
    trans  = slot_map.get("translucency", "")

    lines = [
        "// THIS FILE IS AUTO-GENERATED",
        "",
        "Layer0",
        "{",
        f'\tshader "csgo_static_overlay.vfx"',
        "",
    ]

    if has_trans and trans:
        lines += [
            f"{T}//---- Blend Mode ----",
            f"{T}F_BLEND_MODE 1 // Translucent",
            "",
        ]

    lines += [
        f"{T}//---- Lighting ----",
        f"{T}F_LIT 1",
        "",
        f"{T}//---- Ambient Occlusion ----",
        f'{T}TextureAmbientOcclusion "{ao}"',  # FIXED
        "",
        f"{T}//---- Color ----",
        f'{T}g_flModelTintAmount "1.000"',
        f'{T}g_flTexCoordRotation "0.000"',
        f'{T}g_fTextureColorBrightness "1.000"',
        f'{T}g_fTextureColorContrast "1.000"',
        f'{T}g_fTextureColorSaturation "1.000"',
        f'{T}g_nScaleTexCoordUByModelScaleAxis "0" // None',
        f'{T}g_nScaleTexCoordVByModelScaleAxis "0" // None',
        f'{T}g_vColorTint "[1.000000 1.000000 1.000000 0.000000]"',
        f'{T}g_vTexCoordCenter "[0.500 0.500]"',
        f'{T}g_vTexCoordOffset "[0.000 0.000]"',
        f'{T}g_vTexCoordScale "[1.000 1.000]"',
        f'{T}g_vTexCoordScrollSpeed "[0.000 0.000]"',
        f'{T}g_vTextureColorCorrectionTint "[1.000000 1.000000 1.000000 0.000000]"',
    ]
    if color:
        lines.append(f'{T}TextureColor "{color}"')

    lines += [
        "",
        f"{T}//---- Fog ----",
        f'{T}g_bFogEnabled "1"',
        "",
        f"{T}//---- Lighting ----",
        f'{T}g_fTextureRoughnessBrightness "1.000"',
        f'{T}g_fTextureRoughnessContrast "1.000"',
        f'{T}TextureMetalness "materials/default/default_metal.tga"',
        f'{T}TextureRoughness "[1.000000 1.000000 1.000000 0.000000]"',
        "",
        f"{T}//---- Normal Map ----",
        f'{T}g_fTextureNormalContrast "1.000"',
        f'{T}TextureNormal "{normal}"',  # FIXED
        "",
        f"{T}//---- Self Illum ----",
        f'{T}g_flSelfIllumAlbedoFactor "1.000"',
        f'{T}g_flSelfIllumBrightness "0.000"',
        f'{T}g_flSelfIllumScale "1.000"',
        f'{T}g_vSelfIllumScrollSpeed "[0.000 0.000]"',
        f'{T}g_vSelfIllumTint "[1.000000 1.000000 1.000000 0.000000]"',
        f'{T}TextureSelfIllumMask "materials/default/default_selfillum.tga"',
        "",
        f"{T}//---- Texture Address Mode ----",
        f'{T}g_nTextureAddressModeU "0" // Wrap',
        f'{T}g_nTextureAddressModeV "0" // Wrap',
    ]

    if has_trans and trans:
        lines += [
            "",
            f"{T}//---- Translucent ----",
            f'{T}g_flOpacityScale "1.000"',
            f'{T}TextureTranslucency "{trans}"',
        ]

    lines += [
        "",
        f"{T}VariableState",
        f"{T}{{",
        f'{T}\t"Ambient Occlusion"',
        f"{T}\t{{",
        f"{T}\t}}",
        f'{T}\t"Color"',
        f"{T}\t{{",
        f'{T}\t\t"Color Correction" 0',
        f"{T}\t}}",
        f'{T}\t"Fog"',
        f"{T}\t{{",
        f"{T}\t}}",
        f'{T}\t"Lighting"',
        f"{T}\t{{",
        f'{T}\t\t"Roughness" 0',
        f'{T}\t\t"Metalness" 0',
        f"{T}\t}}",
        f'{T}\t"Normal Map"',
        f"{T}\t{{",
        f"{T}\t}}",
        f'{T}\t"Self Illum"',
        f"{T}\t{{",
        f"{T}\t}}",
        f'{T}\t"Texture Address Mode"',
        f"{T}\t{{",
        f"{T}\t}}",
    ]
    if has_trans and trans:
        lines += [
            f'{T}\t"Translucent"',
            f"{T}\t{{",
            f"{T}\t}}",
        ]
    lines += [f"{T}}}", "}", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Single group processing
# ---------------------------------------------------------------------------

def process_group(
    group_name: str,
    textures: list[str],
    category: str,
    out_root: str,
) -> bool:
    _log(f"\n  Group [{category}] {group_name}")

    mat_dir = os.path.join(out_root, "materials", ASSETS_SUBFOLDER, category, group_name)
    os.makedirs(mat_dir, exist_ok=True)

    slot_map_src = select_slots(textures)

    # FEATURE: roughness maps are not used in the VMATs, so they are not copied
    if "roughness" in slot_map_src:
        del slot_map_src["roughness"]

    if not slot_map_src:
        _warn(f"No known slot found, skipping: {group_name}")
        # FEATURE: clean up so no empty folders are left for unknown or unused texture groups
        try:
            if os.path.exists(mat_dir) and not os.listdir(mat_dir):
                os.rmdir(mat_dir)
        except Exception:
            pass
        return False

    game_prefix = f"materials/{ASSETS_SUBFOLDER}/{category}/{group_name}"

    opacity_remap: dict[str, str] = {}

    for slot, src in slot_map_src.items():
        fname = os.path.basename(src).lower()
        dst   = os.path.join(mat_dir, fname)
        if not os.path.exists(dst):
            shutil.copy2(src, dst)

        if slot == "translucency":
            clf = classify_texture(src)
            if clf and clf[1] == 0:
                stem, ext = os.path.splitext(fname)
                grey_name = stem + "_grey" + ext
                grey_dst  = os.path.join(mat_dir, grey_name)
                if convert_opacity_to_grey(dst, grey_dst):
                    opacity_remap[dst] = grey_dst
                else:
                    opacity_remap[dst] = dst

    slot_map_game: dict[str, str] = {}
    for slot, src in slot_map_src.items():
        fname = os.path.basename(src).lower()
        dst   = os.path.join(mat_dir, fname)
        if slot == "translucency":
            grey = opacity_remap.get(dst)
            if grey and grey != dst:
                fname = os.path.basename(grey)
        slot_map_game[slot] = f"{game_prefix}/{fname}"

    has_trans = "translucency" in slot_map_game

    used_fnames: set[str] = set()
    for gpath in slot_map_game.values():
        used_fnames.add(os.path.basename(gpath))

    for f in os.listdir(mat_dir):
        if f not in used_fnames and not f.endswith(".vmat"):
            fp = os.path.join(mat_dir, f)
            if os.path.isfile(fp):
                os.remove(fp)
                _info(f"Removed unused texture: {f}")

    surface = detect_surface(group_name)
    if surface:
        _info(f"Surface: {surface}")

    if category == "decals":
        vmat_content = build_vmat_decal(slot_map_game, has_trans)
    else:
        vmat_content = build_vmat_texture(slot_map_game, has_trans, surface)

    vmat_name = f"{group_name}.vmat"
    vmat_path = os.path.join(mat_dir, vmat_name)
    try:
        with open(vmat_path, "w", encoding="utf-8") as f:
            f.write(vmat_content)
        _ok(f"VMAT: {game_prefix}/{vmat_name}")
    except OSError as e:
        _err(f"Could not write VMAT: {e}")
        return False

    return True


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def find_textures_in_dir(folder: str) -> list[str]:
    result = []
    for f in os.listdir(folder):
        if os.path.splitext(f)[1].lower() in TEXTURE_EXT:
            result.append(os.path.join(folder, f))
    return result


def find_textures_in_zip(zip_path: str) -> tuple[str | None, list[str]]:
    try:
        tmp, textures = extract_zip(zip_path)
        return tmp, textures
    except Exception as e:
        _warn(f"Could not open ZIP ({zip_path}): {e}")
        return None, []


def collect_category(category_dir: str) -> list[tuple[str, list[str], str | None]]:
    entries: list[tuple[str, list[str], str | None]] = []
    if not os.path.isdir(category_dir):
        return entries

    root_textures: list[str] = []
    tmp_dirs: list[str] = []

    for item in sorted(os.listdir(category_dir)):
        item_path = os.path.join(category_dir, item)
        ext = os.path.splitext(item)[1].lower()

        if os.path.isdir(item_path):
            sub_textures = find_textures_in_dir(item_path)
            for f in os.listdir(item_path):
                if os.path.splitext(f)[1].lower() == ".zip":
                    tmp, zt = find_textures_in_zip(os.path.join(item_path, f))
                    if zt:
                        sub_textures.extend(zt)
                        tmp_dirs.append(tmp)
            if sub_textures:
                groups = group_textures(sub_textures)
                for gname, gpaths in groups.items():
                    safe_gname = re.sub(r'[^\w\-]', '_', gname).lower()
                    entries.append((safe_gname, gpaths, None))

        elif ext == ".zip":
            tmp, zt = find_textures_in_zip(item_path)
            if zt:
                groups = group_textures(zt)
                for gname, gpaths in groups.items():
                    safe_gname = re.sub(r'[^\w\-]', '_', gname).lower()
                    entries.append((safe_gname, gpaths, tmp))

        elif ext in TEXTURE_EXT:
            root_textures.append(item_path)

    if root_textures:
        groups = group_textures(root_textures)
        for gname, gpaths in groups.items():
            safe_gname = re.sub(r'[^\w\-]', '_', gname).lower()
            entries.append((safe_gname, gpaths, None))

    return entries


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------

def main(root_dir=None, out_dir=None):
    root_dir = os.path.abspath(root_dir) if root_dir else os.path.dirname(os.path.abspath(__file__))

    print("=" * 50)
    print("  jpg_to_vmat  -  tehlikeli91")
    print("=" * 50)
    print(f"  Folder: {root_dir}")
    print("=" * 50)

    decals_dir   = os.path.join(root_dir, "decals")
    textures_dir = os.path.join(root_dir, "textures")

    decal_entries   = collect_category(decals_dir)
    texture_entries = collect_category(textures_dir)

    total = len(decal_entries) + len(texture_entries)
    print(f"  Decal groups   : {len(decal_entries)}")
    print(f"  Texture groups : {len(texture_entries)}")
    print(f"  Total          : {total}")

    if total == 0:
        print("\n  No textures found.")
        print("  Create a 'decals' or 'textures' folder and put the .jpg files in it.")
        return

    # CS2 Porter: the output folder can be chosen separately (empty means the input folder)
    out_root = os.path.abspath(out_dir) if out_dir else root_dir

    ok_count = err_count = 0
    tmp_to_clean: set[str] = set()

    def _run(entries, category):
        nonlocal ok_count, err_count
        for gname, paths, tmp in entries:
            if tmp:
                tmp_to_clean.add(tmp)
            try:
                ok = process_group(gname, paths, category, out_root)
                if ok:
                    ok_count += 1
                else:
                    err_count += 1
            except Exception as e:
                _err(f"Unexpected error - {gname}: {e}")
                import traceback; traceback.print_exc()
                err_count += 1

    _run(decal_entries,   "decals")
    _run(texture_entries, "textures")

    for d in tmp_to_clean:
        try:
            shutil.rmtree(d)
        except Exception:
            pass

    print(f"\n{'=' * 50}")
    print(f"  Done   OK: {ok_count}   Failed: {err_count}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()