import os
import re
import random
import shutil
import string
import struct
import sys
import zipfile
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

ASSETS_SUBFOLDER   = "assets"
TEXTURE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tga", ".bmp", ".psd", ".exr", ".hdr"}
MAX_PATH           = 260

# Windows absolute path prefix consumed before our game-relative path begins.
# Typical CS2 addon path:
#   C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\content\csgo_addons\<map_name>\
# That prefix alone is ~115-130 chars; we reserve 140 to be safe.
_WIN_PREFIX_BUDGET = 140

# Maximum length allowed for the game-relative portion of any path
# (e.g. "materials/assets/<group>/<texture>.vtex_c").
# The engine also appends a hash suffix (_XXXXXXX.vtex), so shave 20 extra.
MAX_GAME_PATH = MAX_PATH - _WIN_PREFIX_BUDGET - 20   # ~100 chars

# Overhead of the fixed part of a texture game-path, excluding the group name:
# "materials/assets/" + "/" + "<texture>.jpg"  (~50 chars for the texture filename)
_GAME_PATH_OVERHEAD = len(f"materials/{ASSETS_SUBFOLDER}/") + 1 + 50


def _random_suffix(n: int = 4) -> str:
    """Return n random lowercase letters."""
    return "".join(random.choices(string.ascii_lowercase, k=n))


def shorten_group_name(group_name: str) -> str:
    """
    If group_name would cause game-relative paths to exceed MAX_GAME_PATH,
    shorten it and append a 4-char random suffix to avoid collisions.
    """
    max_group_len = MAX_GAME_PATH - _GAME_PATH_OVERHEAD
    if max_group_len < 8:
        max_group_len = 8

    if len(group_name) <= max_group_len:
        return group_name  # already short enough

    keep = max_group_len - 5
    if keep < 4:
        keep = 4
    suffix = _random_suffix(4)
    short = group_name[:keep] + "_" + suffix
    _warn(f"Folder name too long, shortened: '{group_name}' -> '{short}'")
    return short

LOD_BASE_STEP = 150.0
LOD_DECAY     = 0.80

FAKE_MAT_PATTERN = re.compile(r'^(matid_\d+|material\d*|mat\d+|defaultmat)$', re.IGNORECASE)

TEXTURE_SLOTS: list[tuple[list[str], str, int]] = [
    (["_basecolor", "_base_color", "_albedo", "_diffuse", "_color", "_col", "_bc", "_diff"],
     "TextureLayer1Color", 0),
    (["_normalmap", "_normal", "_norm", "_nrm", "_nor", "_ddna", "_nm"],
     "TextureLayer1Normal", 0),
    (["_roughness", "_rougness", "_rough", "_rgh"],
     "TextureLayer1Roughness", 0),
    (["_glossiness", "_gloss"],
     "TextureLayer1Roughness", 1),
    (["_ambientocclusion", "_ambient_occlusion", "_occlusion", "_ao"],
     "TextureLayer1AmbientOcclusion", 0),
    (["_cavity", "_fuzz"],
     "TextureLayer1AmbientOcclusion", 1),
    (["_opacity"],
     "TextureLayer1Translucency", 0),
    (["_translucency", "_trans", "_alpha", "_mask"],
     "TextureLayer1Translucency", 1),
]

EXCLUDED_SLOTS = {"TextureMetalness", "TextureSpecular", "TextureBump", "TextureSelfIllumMask"}


# ---------------------------------------------------------------------------
# FBX material/geometry name extraction
# ---------------------------------------------------------------------------

def _extract_fbx_names_binary(data: bytes, node_type: str) -> list[str]:
    """Extract FBX node names of a given type (e.g. 'Material', 'Geometry')."""
    pattern = rb'([A-Za-z0-9_ \-\.]+)\x00\x01' + node_type.encode()
    raw = re.findall(pattern, data)
    seen, result = set(), []
    for m in raw:
        name = m.decode("utf-8", errors="replace").strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _extract_fbx_names_ascii(data: bytes, node_type: str) -> list[str]:
    text = data.decode("utf-8", errors="replace")
    pattern = re.compile(rf'{node_type}:\s*\d+\s*,\s*"([^"\x00]+)"', re.IGNORECASE)
    raw = pattern.findall(text)
    seen, result = set(), []
    for m in raw:
        name = m.split("\x00")[0].strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _read_fbx(fbx_path: str) -> tuple[bytes, bool]:
    try:
        with open(fbx_path, "rb") as f:
            data = f.read()
        is_binary = data[:23].startswith(b"Kaydara FBX Binary")
        return data, is_binary
    except OSError as e:
        _warn(f"Could not read FBX ({fbx_path}): {e}")
        return b"", False


def extract_fbx_raw_mat_names(fbx_path: str) -> list[str]:
    """Return ALL material names from FBX including fake/generic ones."""
    data, is_binary = _read_fbx(fbx_path)
    if not data:
        return []
    if is_binary:
        names = _extract_fbx_names_binary(data, "Material")
    else:
        names = _extract_fbx_names_ascii(data, "Material")
    return [n.lower() for n in names]

def extract_fbx_vertex_count(fbx_path: str) -> int:
    """
    Estimate the total vertex count of an FBX file.
    Binary FBX: sums the element counts of the double arrays in 'Vertices' nodes (3 values per vertex).
    ASCII FBX:  counts the commas in 'Vertices:' lines.
    Returns 0 if nothing is found.
    """
    data, is_binary = _read_fbx(fbx_path)
    if not data:
        return 0

    total = 0
    if is_binary:
        # Binary FBX: look for ArrayType 'd' (double) or 'f' (float) in the property blocks.
        # Find the array property that comes right after the "Vertices" node name.
        # Node format: end_offset(4) | num_props(4) | prop_list_len(4) | name_len(1) | name | props
        # Simple approach: find the "Vertices" string in the raw bytes, then read the array header.
        i = 0
        needle = b"Vertices"
        while True:
            pos = data.find(needle, i)
            if pos == -1:
                break
            i = pos + len(needle)
            # name_len is the byte right before it; instead of working out the exact offset
            # look for the first array property after the needle.
            # Array property header: type(1) + count(4) + encoding(4) + compressed_len(4)
            # type 'd'=0x64 (double array) or 'f'=0x66 (float array)
            search_ahead = data[i:i+32]
            for offset in range(len(search_ahead)):
                if search_ahead[offset:offset+1] in (b'd', b'f'):
                    header_start = i + offset + 1
                    if header_start + 4 <= len(data):
                        count = struct.unpack_from("<I", data, header_start)[0]
                        if 3 <= count <= 30_000_000:  # sanity check
                            total += count // 3
                    break
    else:
        text = data.decode("utf-8", errors="replace")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("Vertices:"):
                # values are comma separated; 3 values per vertex (x,y,z)
                values_part = stripped[len("Vertices:"):].strip()
                if values_part:
                    count = values_part.count(",") + 1
                    total += count // 3

    return total


def calc_max_mesh_vertices(fbx_paths: list[str]) -> int:
    """
    Returns 10% of the total vertex count of the FBX list.
    Clamped to a minimum of 64 and a maximum of 1000.
    """
    total = sum(extract_fbx_vertex_count(p) for p in fbx_paths)
    if total <= 0:
        return 384  # default
    target = max(64, min(1000, round(total * 0.1)))
    _info(f"  FBX vertex: {total}  ->  maxMeshVertices: {target}")
    return target


def extract_fbx_geometry_names(fbx_path: str) -> list[str]:
    """Return geometry node names from FBX (lowercase, deduplicated)."""
    data, is_binary = _read_fbx(fbx_path)
    if not data:
        return []
    if is_binary:
        names = _extract_fbx_names_binary(data, "Geometry")
    else:
        names = _extract_fbx_names_ascii(data, "Geometry")
    seen, result = set(), []
    for n in names:
        nl = n.lower()
        if nl and nl not in seen:
            seen.add(nl)
            result.append(nl)
    return result


def extract_fbx_materials(fbx_path: str) -> list[str]:
    """
    Extract real (non-generic) material names from FBX.

    Returns only names that do NOT match FAKE_MAT_PATTERN (e.g. MatID_1, mat0).
    Generic/fake names are left for the raw-name fallback in process_group,
    so they are preserved as-is rather than replaced by geometry node names.
    """
    data, is_binary = _read_fbx(fbx_path)
    if not data:
        return []

    if is_binary:
        mat_names = _extract_fbx_names_binary(data, "Material")
    else:
        mat_names = _extract_fbx_names_ascii(data, "Material")

    real_names = [n for n in mat_names if not FAKE_MAT_PATTERN.match(n)]
    return [n.lower() for n in real_names]
def derive_material_name_from_textures(textures: list[str], fbx_stem: str) -> str:
    if not textures:
        return fbx_stem.lower()

    known_suffixes = []
    for suffixes, _, _ in TEXTURE_SLOTS:
        known_suffixes.extend(suffixes)
    known_suffixes.sort(key=len, reverse=True)

    candidates = []
    for tex in textures:
        base = os.path.splitext(os.path.basename(tex))[0].lower()
        stripped = base
        for suf in known_suffixes:
            if base.endswith(suf):
                stripped = base[:-len(suf)]
                break
        if stripped:
            candidates.append(stripped)

    if not candidates:
        return fbx_stem.lower()

    freq = defaultdict(int)
    for c in candidates:
        freq[c] += 1
    best = max(freq, key=lambda k: (freq[k], len(k)))
    return best


# ---------------------------------------------------------------------------
# LOD file grouping
# ---------------------------------------------------------------------------

_LOD_RE = re.compile(r'(.*?)[-_](lod\d+)$', re.IGNORECASE)


def group_fbx_files(fbx_paths: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for path in fbx_paths:
        stem = os.path.splitext(os.path.basename(path))[0]
        m = _LOD_RE.match(stem)
        if m:
            base_key  = m.group(1)
            lod_tag   = m.group(2).upper()
            lod_index = int(re.search(r'\d+', lod_tag).group())
            group_key = os.path.join(os.path.dirname(path), base_key)
            groups[group_key].append((lod_index, path))
        else:
            group_key = os.path.join(os.path.dirname(path), stem)
            groups[group_key].append((0, path))

    result: dict[str, list[str]] = {}
    for key, items in groups.items():
        sorted_items = sorted(items, key=lambda x: x[0])
        result[key] = [p for _, p in sorted_items]
    return result


def calc_lod_thresholds(n: int) -> list[float]:
    if n <= 0:
        return []
    thresholds = [0.0]
    step = LOD_BASE_STEP
    for _ in range(n - 1):
        thresholds.append(round(thresholds[-1] + step, 1))
        step *= LOD_DECAY
    return thresholds


# ---------------------------------------------------------------------------
# Texture classification & slot selection
# ---------------------------------------------------------------------------

def _classify_texture(tex_path: str) -> tuple[str, int, int] | None:
    base = os.path.splitext(os.path.basename(tex_path))[0].lower()
    for suffixes, slot, tier in TEXTURE_SLOTS:
        if slot in EXCLUDED_SLOTS:
            continue
        for suf in suffixes:
            if base.endswith(suf):
                return slot, tier, len(suf)
    return None


def find_textures_in_dir(folder: str) -> list[str]:
    result = []
    try:
        for f in os.listdir(folder):
            if os.path.splitext(f)[1].lower() in TEXTURE_EXTENSIONS:
                result.append(os.path.join(folder, f))
    except OSError:
        pass
    return result


def _detect_mat_prefix(all_textures: list[str], mat_name: str) -> bool:
    """Return True if ANY texture in the list contains mat_name in its basename."""
    mat_lower = mat_name.lower()
    for t in all_textures:
        base = os.path.splitext(os.path.basename(t))[0].lower()
        if mat_lower in base:
            return True
    return False


def _texture_belongs_to_material(
    tex_path: str,
    mat_name: str,
    all_mat_names: list[str],
    mat_has_named_textures: bool,
) -> bool:
    """
    Return True if this texture should be assigned to mat_name.

    Rule 1: if another known material name appears in the texture filename and
    mat_name does NOT, the texture belongs to that other material.

    Rule 2: if mat_name-specific textures exist in this folder (i.e. at least
    one texture contains mat_name), then textures that do NOT contain mat_name
    are also rejected; they belong to a different format group.
    Example: generic '_ao.jpg' must not enter billboard.vmat when
    'billboard_opacity.jpg' signals that billboard has its own named set.
    """
    tex_base = os.path.splitext(os.path.basename(tex_path))[0].lower()
    mat_lower = mat_name.lower()

    # Rule 1
    for other in all_mat_names:
        other_lower = other.lower()
        if other_lower == mat_lower:
            continue
        if other_lower in tex_base and mat_lower not in tex_base:
            return False

    # Rule 2
    if mat_has_named_textures and mat_lower not in tex_base:
        return False

    return True


def select_textures_for_slots(
    all_textures: list[str],
    mat_name: str = "",
    all_mat_names: list[str] | None = None,
) -> dict[str, str]:
    """
    Select best texture per slot, filtered to mat_name when multiple materials exist.

    When a material has at least one texture that contains its own name
    (e.g. 'billboard_opacity.jpg' for material 'billboard'), ONLY textures
    sharing that naming pattern are considered; generic textures (e.g. '_ao.jpg'
    with no material qualifier) are excluded and fall back to defaults in the VMAT.
    """
    if all_mat_names is None:
        all_mat_names = [mat_name] if mat_name else []

    filtered = all_textures
    if mat_name and len(all_mat_names) > 1:
        mat_has_named = _detect_mat_prefix(all_textures, mat_name)
        filtered = [t for t in all_textures
                    if _texture_belongs_to_material(t, mat_name, all_mat_names, mat_has_named)]

    candidates: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for tex in filtered:
        result = _classify_texture(tex)
        if result:
            slot, tier, suf_len = result
            candidates[slot].append((tier, suf_len, tex))

    selected: dict[str, str] = {}
    mat_lower = mat_name.lower()
    for slot, cands in candidates.items():
        # Primary sort: (tier ASC, name_match ASC, -suf_len ASC)
        # name_match=0 means filename contains mat_name → preferred
        def _sort_key(item: tuple[int, int, str]) -> tuple[int, int, int]:
            tier, suf_len, path = item
            tex_base = os.path.splitext(os.path.basename(path))[0].lower()
            name_match = 0 if (mat_lower and mat_lower in tex_base) else 1
            return (tier, name_match, -suf_len)

        _, _, best_path = sorted(cands, key=_sort_key)[0]
        selected[slot] = best_path
        others = [p for _, _, p in cands if p != best_path]
        if others:
            _info(f"Slot conflict '{slot}': picked '{os.path.basename(best_path)}', "
                  f"atlandi: {[os.path.basename(o) for o in others]}")
    return selected


# ---------------------------------------------------------------------------
# Opacity greyscale conversion (from convert_transculent.py logic)
# ---------------------------------------------------------------------------

def _is_greyscale(img: Image.Image, threshold: float = 0.01) -> bool:
    """Return True if image is already greyscale (R==G==B within threshold)."""
    arr = np.array(img.convert("RGB"), dtype=np.float32)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    diff_rg = np.abs(r - g).mean() / 255.0
    diff_rb = np.abs(r - b).mean() / 255.0
    return diff_rg <= threshold and diff_rb <= threshold


def convert_opacity_to_greyscale(src_path: str, dst_path: str) -> bool:
    """
    If the opacity texture is not already greyscale, convert its alpha channel
    (or luminance) to a greyscale RGB image and save to dst_path.
    Returns True if conversion was done (or file already exists), False on error.
    """
    if os.path.exists(dst_path):
        return True

    try:
        img = Image.open(src_path)
        img.verify()
        img = Image.open(src_path)
    except Exception as e:
        _warn(f"Could not open opacity texture ({os.path.basename(src_path)}): {e}")
        return False

    # If image has alpha, use alpha channel as the opacity mask
    if img.mode in ("RGBA", "LA"):
        img_rgba = img.convert("RGBA")
        if _is_greyscale(img_rgba.convert("RGB")):
            # RGB is already grey, just copy as-is
            shutil.copy2(src_path, dst_path)
            _info(f"Opacity already greyscale, copied: {os.path.basename(dst_path)}")
            return True
        arr = np.array(img_rgba, dtype=np.float32)
        alpha = arr[:, :, 3] / 255.0
        brightness = (alpha * 255).astype(np.uint8)
        rgb = np.stack([brightness, brightness, brightness], axis=2)
        result_img = Image.fromarray(rgb, "RGB")
    else:
        img_rgb = img.convert("RGB")
        if _is_greyscale(img_rgb):
            shutil.copy2(src_path, dst_path)
            _info(f"Opacity already greyscale, copied: {os.path.basename(dst_path)}")
            return True
        # Luminance-based greyscale
        img_grey = img_rgb.convert("L")
        arr = np.array(img_grey, dtype=np.uint8)
        rgb = np.stack([arr, arr, arr], axis=2)
        result_img = Image.fromarray(rgb, "RGB")

    ext = os.path.splitext(dst_path)[1].lower()
    save_kwargs = {}
    if ext in (".png",):
        save_kwargs = {"optimize": False, "compress_level": 1}

    try:
        result_img.save(dst_path, **save_kwargs)
        _ok(f"Opacity converted to greyscale: {os.path.basename(dst_path)}")
        return True
    except Exception as e:
        _warn(f"Could not save opacity ({os.path.basename(dst_path)}): {e}")
        return False


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def build_texture_name_cache(
    tex_fnames: list[str],
    game_prefix: str,
) -> dict[str, str]:
    """
    Build a stable mapping of  original_lowercase_filename -> safe_filename
    for every texture in this group.  The random suffix is generated ONCE here
    so every subsequent lookup returns the same name, keeping .vmat paths and
    the actual copied files in sync.

    tex_fnames : list of bare filenames (no directory), e.g. ["foo_ao.jpg", ...]
    game_prefix: e.g. "materials/assets/<group_name>"
    Returns    : {original_lc_fname: safe_fname}
    """
    cache: dict[str, str] = {}
    used_names: set[str] = set()          # guard against accidental collisions

    for tex_fname in tex_fnames:
        lc = tex_fname.lower()
        if lc in cache:
            continue                       # already processed

        game_path = f"{game_prefix}/{lc}"
        if len(game_path) < MAX_GAME_PATH:
            cache[lc] = lc               # no shortening needed
            used_names.add(lc)
            continue

        # Need to shorten the filename
        stem, ext = os.path.splitext(lc)
        allowed = MAX_GAME_PATH - len(game_prefix) - 1 - len(ext) - 1
        if allowed < 8:
            allowed = 8
        keep = allowed - 5
        if keep < 4:
            keep = 4

        # Generate a suffix that doesn't collide with an already-used name
        for _ in range(100):
            short_stem = stem[:keep] + "_" + _random_suffix(4)
            short_name = short_stem + ext
            if short_name not in used_names:
                break

        cache[lc] = short_name
        used_names.add(short_name)
        _warn(f"Texture name too long, shortened: '{lc}' -> '{short_name}'")

    return cache


def build_safe_texture_dst(
    material_dir: str,
    tex_fname: str,
    game_prefix: str,
    name_cache: dict[str, str] | None = None,
) -> tuple[str, str]:
    """
    Return (filesystem_dst_path, game_relative_path) for a texture file.

    When name_cache is provided (recommended) it is used for a stable name
    lookup so the same texture always resolves to the same safe filename.
    Without a cache the function falls back to generating a random suffix on
    the spot (legacy behaviour, avoid for new call-sites).
    """
    tex_fname_lc = tex_fname.lower()

    if name_cache is not None:
        safe_fname = name_cache.get(tex_fname_lc, tex_fname_lc)
    else:
        # Legacy fallback: generate suffix inline (may be inconsistent)
        game_path_test = f"{game_prefix}/{tex_fname_lc}"
        if len(game_path_test) >= MAX_GAME_PATH:
            stem, ext = os.path.splitext(tex_fname_lc)
            allowed = MAX_GAME_PATH - len(game_prefix) - 1 - len(ext) - 1
            if allowed < 8:
                allowed = 8
            keep = allowed - 5
            if keep < 4:
                keep = 4
            safe_fname = stem[:keep] + "_" + _random_suffix(4) + ext
            _warn(f"Texture name too long, shortened: '{tex_fname_lc}' -> '{safe_fname}'")
        else:
            safe_fname = tex_fname_lc

    dst       = os.path.join(material_dir, safe_fname)
    game_path = f"{game_prefix}/{safe_fname}"
    return dst, game_path


# ---------------------------------------------------------------------------
# VMAT builders
# ---------------------------------------------------------------------------

def build_vmat_static(slot_map: dict[str, str], has_opacity: bool) -> str:
    T = "\t"
    ao    = slot_map.get("TextureLayer1AmbientOcclusion", "materials/default/default_ao.tga")
    color = slot_map.get("TextureLayer1Color", "")
    normal = slot_map.get("TextureLayer1Normal", "materials/default/default_normal.tga")
    trans = slot_map.get("TextureLayer1Translucency", "")

    lines = [
        "// THIS FILE IS AUTO-GENERATED",
        "",
        "Layer0",
        "{",
        f'\tshader "csgo_lightmappedgeneric.vfx"',
        "",
    ]

    if has_opacity and trans:
        lines += [
            f"{T}//---- Translucent ----",
            f"{T}F_TRANSLUCENT 1",
            "",
        ]

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

    if has_opacity and trans:
        lines.append(f'{T}TextureLayer1Translucency "{trans}"')

    lines += [
        "",
        f"{T}//---- Texture Address Mode ----",
        f'{T}g_nTextureAddressModeU "0" // Wrap',
        f'{T}g_nTextureAddressModeV "0" // Wrap',
    ]

    if has_opacity and trans:
        lines += [
            "",
            f"{T}//---- Translucent ----",
            f'{T}g_flOpacityScale "1.000"',
        ]

    pbr_children = [
        f'{T*2}\t"Albedo" 0',
        f'{T*2}\t"Normal" 0',
        f'{T*2}\t"Roughness" 0',
        f'{T*2}\t"Ambient Occlusion" 0',
    ]
    if has_opacity and trans:
        pbr_children.insert(1, f'{T*2}\t"Albedo Translucency" 0')

    vs_lines = [
        "",
        f"{T}VariableState",
        f"{T}{{",
        f'{T}\t"Color"',
        f"{T}\t{{",
        f"{T}\t}}",
        f'{T}\t"Fog"',
        f"{T}\t{{",
        f"{T}\t}}",
        f'{T}\t"Lighting"',
        f"{T}\t{{",
        f'{T}\t\t"Metalness" 0',
        f"{T}\t}}",
        f'{T}\t"PBR 1"',
        f"{T}\t{{",
    ]
    vs_lines.extend(pbr_children)
    vs_lines += [
        f"{T}\t}}",
        f'{T}\t"Texture Address Mode"',
        f"{T}\t{{",
        f"{T}\t}}",
    ]
    if has_opacity and trans:
        vs_lines += [
            f'{T}\t"Translucent"',
            f"{T}\t{{",
            f"{T}\t}}",
        ]
    vs_lines.append(f"{T}}}")
    lines.extend(vs_lines)
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def build_vmat_foliage(slot_map: dict[str, str], model_path: str) -> str:
    T = "\t"
    color  = slot_map.get("TextureLayer1Color", "materials/default/default_color.tga")
    normal = slot_map.get("TextureLayer1Normal", "materials/default/default_normal.tga")
    trans  = slot_map.get("TextureLayer1Translucency", "")
    ao     = slot_map.get("TextureLayer1AmbientOcclusion", "materials/default/default_ao.tga")

    lines = [
        "// THIS FILE IS AUTO-GENERATED",
        "",
        "Layer0",
        "{",
        f'\tshader "csgo_foliage.vfx"',
        "",
        f"{T}//---- 2-Sided Rendering ----",
        f"{T}F_RENDER_BACKFACES 1",
        "",
        f"{T}//---- Foliage Animation ----",
        f"{T}F_FOLIAGE_ANIMATION 1",
        "",
        f"{T}//---- Translucent ----",
        f"{T}F_ALPHA_TEST 1",
        "",
        f"{T}//---- Ambient Occlusion ----",
        f'{T}TextureAmbientOcclusion "{ao}"',
        "",
        f"{T}//---- Color ----",
        f'{T}g_flModelTintAmount "1.000"',
        f'{T}g_fTextureColorBrightness "1.000"',
        f'{T}g_fTextureColorContrast "1.000"',
        f'{T}g_fTextureColorSaturation "1.000"',
        f'{T}g_vColorTint "[1.000000 1.000000 1.000000 0.000000]"',
        f'{T}g_vTexCoordScrollSpeed "[0.000 0.000]"',
        f'{T}g_vTextureColorCorrectionTint "[1.000000 1.000000 1.000000 0.000000]"',
        f'{T}TextureColor "{color}"',
        "",
        f"{T}//---- Fog ----",
        f'{T}g_bFogEnabled "1"',
        "",
        f"{T}//---- Foliage Animation ----",
        f'{T}g_bBranchMotionPerpendicular "1"',
        f'{T}g_bFrondTipUseEnvWind "0"',
        f'{T}g_bSmootherNoiseBranchAndTrunk "1"',
        f'{T}g_flBranchAnimSpeed "1.000"',
        f'{T}g_flBranchDeflection "0.000"',
        f'{T}g_flBranchDeflectionEnd "150.000"',
        f'{T}g_flBranchDeflectionStart "0.000"',
        f'{T}g_flBranchNoiseLevel "0.500"',
        f'{T}g_flFrondTipAnimSpeed "1.000"',
        f'{T}g_flFrondTipMaskFalloff "1.000"',
        f'{T}g_flFrondTipSpatialIncoherence "1.000"',
        f'{T}g_flFrondTipStrength "2.000"',
        f'{T}g_flTrunkAnimSpeed "1.000"',
        f'{T}g_flTrunkDeflection "0.500"',
        f'{T}g_flTrunkDeflectionEnd "300.000"',
        f'{T}g_flTrunkDeflectionStart "0.000"',
        f'{T}g_flTrunkNoiseLevel "0.500"',
        f'{T}g_vNoiseUvScale "[1.000 1.000 1.000]"',
        f'{T}g_vNoiseUvSpeed "[0.250 0.250 0.250]"',
        "",
        f"{T}//---- Lighting ----",
        f'{T}g_fTextureRoughnessBrightness "1.000"',
        f'{T}g_fTextureRoughnessContrast "1.000"',
        f'{T}TextureRoughness "[1.000000 1.000000 1.000000 0.000000]"',
        "",
        f"{T}//---- Normal Map ----",
        f'{T}g_fTextureNormalContrast "1.000"',
        f'{T}TextureNormal "{normal}"',
        "",
        f"{T}//---- Texture Address Mode ----",
        f'{T}g_nTextureAddressModeU "0" // Wrap',
        f'{T}g_nTextureAddressModeV "0" // Wrap',
    ]

    if trans:
        lines += [
            "",
            f"{T}//---- Translucent ----",
            f'{T}g_flAlphaBoostDistance "20000.000"',
            f'{T}g_flAlphaBoostStrength "3.000"',
            f'{T}g_flAlphaTestReference "0.100"',
            f'{T}g_flGrazingAngleMask "0.000"',
            f'{T}TextureTranslucency "{trans}"',
        ]

    lines += [
        "",
        f"{T}//---- Vertex Color ----",
        f'{T}g_flHeightGradientAmount "0.000"',
        f'{T}g_flHeightGradientPower "1.000"',
        f'{T}g_flVertexAmbientOcclusionAmount "0.000"',
        f'{T}g_flVertexAmbientOcclusionPower "1.000"',
    ]

    if model_path:
        lines += [
            "",
            f"{T}Attributes",
            f"{T}{{",
            f'{T}\tPreviewModel "{model_path}"',
            f"{T}}}",
        ]

    lines += [
        "",
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
        f'{T}\t"Foliage Animation"',
        f"{T}\t{{",
        f'{T}\t\t"Branch Animation" 0',
        f'{T}\t\t"Common Noise Parameters" 0',
        f'{T}\t\t"Frond Tip Anim (Red Channel)" 0',
        f'{T}\t\t"Trunk Animation" 0',
        f"{T}\t}}",
        f'{T}\t"Lighting"',
        f"{T}\t{{",
        f'{T}\t\t"Roughness" 0',
        f"{T}\t}}",
        f'{T}\t"Normal Map"',
        f"{T}\t{{",
        f"{T}\t}}",
        f'{T}\t"Texture Address Mode"',
        f"{T}\t{{",
        f"{T}\t}}",
        f'{T}\t"Translucent"',
        f"{T}\t{{",
        f'{T}\t\t"Alpha Boost Mask" 0',
        f'{T}\t\t"Grazing Angle Mask" 0',
        f"{T}\t}}",
        f'{T}\t"Vertex Color"',
        f"{T}\t{{",
        f'{T}\t\t"Ambient Occlusion (Alpha Channel)" 0',
        f'{T}\t\t"Height Gradient (Red Channel)" 0',
        f"{T}\t}}",
        f"{T}}}",
        "}",
        "",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# VMDL builder
# ---------------------------------------------------------------------------

def _lod_group_block(mesh_name: str, threshold: float) -> str:
    T = "\t"
    return (
        f"{T*4}{{\n"
        f"{T*5}_class = \"LODGroup\"\n"
        f"{T*5}mesh_references = \n"
        f"{T*5}[\n"
        f"{T*6}{{\n"
        f"{T*7}mesh_name = \"{mesh_name}\"\n"
        f"{T*7}copy_and_simplify = false\n"
        f"{T*7}simplify_params = \n"
        f"{T*7}{{\n"
        f"{T*8}targetTrianglePercent = 0.5\n"
        f"{T*8}targetError = 0.01\n"
        f"{T*8}attributeAware = true\n"
        f"{T*8}normalWeight = 1.25\n"
        f"{T*8}uvWeight = 0.5\n"
        f"{T*8}jointWeightWeight = 1.0\n"
        f"{T*8}colorWeight = 0.0\n"
        f"{T*8}lockBorder = false\n"
        f"{T*8}removeSmallComponents = false\n"
        f"{T*8}smallComponentSize = 0.025\n"
        f"{T*8}weldVertices = true\n"
        f"{T*8}weldPositionTolerance = 0.001\n"
        f"{T*8}visualizeEdges = false\n"
        f"{T*8}recomputeVertices = false\n"
        f"{T*8}regularization = \"default\"\n"
        f"{T*8}permissive = false\n"
        f"{T*8}prune = false\n"
        f"{T*7}}}\n"
        f"{T*6}}},\n"
        f"{T*5}]\n"
        f"{T*5}switch_threshold = {threshold:.1f}\n"
        f"{T*4}}},\n"
    )


def _render_mesh_block(fbx_game_path: str) -> str:
    T = "\t"
    return (
        f"{T*4}{{\n"
        f"{T*5}_class = \"RenderMeshFile\"\n"
        f"{T*5}filename = \"{fbx_game_path}\"\n"
        f"{T*5}import_scale = 1.0\n"
        f"{T*5}import_filter = \n"
        f"{T*5}{{\n"
        f"{T*6}exclude_by_default = false\n"
        f"{T*6}exception_list = [  ]\n"
        f"{T*5}}}\n"
        f"{T*4}}},\n"
    )


def _physics_section_static_complex(max_verts: int, T: str) -> str:
    """PhysicsMeshFromRender block for static_complex / foliage_complex."""
    return (
        f"{T*2}{{\n"
        f"{T*3}_class = \"PhysicsShapeList\"\n"
        f"{T*3}children = \n"
        f"{T*3}[\n"
        f"{T*4}{{\n"
        f"{T*5}_class = \"PhysicsMeshFromRender\"\n"
        f"{T*5}parent_bone = \"\"\n"
        f"{T*5}surface_prop = \"default\"\n"
        f"{T*5}collision_prop = \"default\"\n"
        f"{T*5}tool_material = \"\"\n"
        f"{T*5}renderMeshList = [  ]\n"
        f"{T*5}simplification_params = \n"
        f"{T*5}{{\n"
        f"{T*6}qemError = 0.0\n"
        f"{T*6}maxMeshVertices = {max_verts}\n"
        f"{T*6}small_element_threshold = 0.0\n"
        f"{T*6}thin_element_threshold = 0.0\n"
        f"{T*5}}}\n"
        f"{T*4}}},\n"
        f"{T*3}]\n"
        f"{T*3}leave_body_collision_unmodified = false\n"
        f"{T*2}}},\n"
    )


def _physics_section_static_basic(T: str) -> str:
    """PhysicsHullFromRender block for static / static_basic / foliage_basic."""
    return (
        f"{T*2}{{\n"
        f"{T*3}_class = \"PhysicsShapeList\"\n"
        f"{T*3}children = \n"
        f"{T*3}[\n"
        f"{T*4}{{\n"
        f"{T*5}_class = \"PhysicsHullFromRender\"\n"
        f"{T*5}parent_bone = \"\"\n"
        f"{T*5}surface_prop = \"default\"\n"
        f"{T*5}collision_prop = \"default\"\n"
        f"{T*5}tool_material = \"\"\n"
        f"{T*5}renderMeshList = [  ]\n"
        f"{T*5}faceMergeAngle = 20.0\n"
        f"{T*5}maxHullVertices = 64\n"
        f"{T*5}optimization_algorithm = \"IFR\"\n"
        f"{T*4}}},\n"
        f"{T*3}]\n"
        f"{T*3}leave_body_collision_unmodified = false\n"
        f"{T*2}}},\n"
    )


def build_vmdl(
    fbx_game_paths: list[str],
    material_remaps: list[tuple[str, str]],
    has_lod: bool,
    asset_type: str = "static",
    max_mesh_vertices: int = 384,
) -> str:
    T = "\t"
    n_lod = len(fbx_game_paths)
    thresholds = calc_lod_thresholds(n_lod)

    remap_lines = []
    for from_mat, to_path in material_remaps:
        remap_lines.append(
            f"{T*6}{{\n"
            f"{T*7}from = \"{from_mat}.vmat\"\n"
            f"{T*7}to = \"{to_path}\"\n"
            f"{T*6}}},"
        )
    remaps_block = "\n".join(remap_lines)

    mat_group = (
        f"{T*2}{{\n"
        f"{T*3}_class = \"MaterialGroupList\"\n"
        f"{T*3}children = \n"
        f"{T*3}[\n"
        f"{T*4}{{\n"
        f"{T*5}_class = \"DefaultMaterialGroup\"\n"
        f"{T*5}remaps = \n"
        f"{T*5}[\n"
        f"{remaps_block}\n"
        f"{T*5}]\n"
        f"{T*5}use_global_default = false\n"
        f"{T*5}global_default_material = \"\"\n"
        f"{T*4}}},\n"
        f"{T*3}]\n"
        f"{T*2}}},\n"
    )

    lod_group_section = ""
    if has_lod and n_lod > 1:
        lod_blocks = ""
        for i, (path, thresh) in enumerate(zip(fbx_game_paths, thresholds)):
            mesh_name = f"unnamed_{i + 1}"
            lod_blocks += _lod_group_block(mesh_name, thresh)
        lod_group_section = (
            f"{T*2}{{\n"
            f"{T*3}_class = \"LODGroupList\"\n"
            f"{T*3}children = \n"
            f"{T*3}[\n"
            f"{lod_blocks}"
            f"{T*3}]\n"
            f"{T*2}}},\n"
        )

    # Physics section: pick by type
    # foliage / foliage_basic / static / static_basic  -> PhysicsHullFromRender (basic)
    # foliage_complex / static_complex                 -> PhysicsMeshFromRender (complex)
    # foliage (pure)                                   -> no physics
    if asset_type in ("foliage_complex", "static_complex"):
        physics_section = _physics_section_static_complex(max_mesh_vertices, T)
    elif asset_type in ("foliage", "static"):
        physics_section = ""   # foliage: no PhysicsShapeList block
    else:
        # static, static_basic, foliage_basic
        physics_section = _physics_section_static_basic(T)

    render_blocks = "".join(_render_mesh_block(p) for p in fbx_game_paths)
    render_section = (
        f"{T*2}{{\n"
        f"{T*3}_class = \"RenderMeshList\"\n"
        f"{T*3}children = \n"
        f"{T*3}[\n"
        f"{render_blocks}"
        f"{T*3}]\n"
        f"{T*2}}},\n"
    )

    children = ""
    if lod_group_section:
        children += lod_group_section
    children += mat_group
    children += physics_section
    children += render_section

    return (
        f"<!-- kv3 encoding:text:version{{e21c7f3c-8a33-41c5-9977-a76d3a32aa0d}} "
        f"format:modeldoc41:version{{12fc9d44-453a-4ae4-b4d9-7e2ac0bbd4e0}} -->\n"
        f"{{\n"
        f"{T}rootNode = \n"
        f"{T}{{\n"
        f"{T*2}_class = \"RootNode\"\n"
        f"{T*2}children = \n"
        f"{T*2}[\n"
        f"{children}"
        f"{T*2}]\n"
        f"{T*2}model_archetype = \"\"\n"
        f"{T*2}primary_associated_entity = \"\"\n"
        f"{T*2}anim_graph_name = \"\"\n"
        f"{T*2}document_sub_type = \"ModelDocSubType_None\"\n"
        f"{T}}}\n"
        f"}}\n"
    )


# ---------------------------------------------------------------------------
# Asset type detection
# ---------------------------------------------------------------------------

def get_asset_type(fbx_path: str, root_dir: str, tmp_dir: str | None = None, zip_path: str | None = None) -> str:
    types = ("foliage", "foliage_basic", "foliage_complex",
             "static",  "static_basic",  "static_complex")
             
    target_path = zip_path if zip_path else fbx_path
        
    rel = os.path.relpath(target_path, root_dir)
    parts = rel.replace("\\", "/").split("/")
    
    if len(parts) > 0:
        first = parts[0].lower()
        if first in types:
            return first
            
    return "static"


# ---------------------------------------------------------------------------
# ZIP extraction
# ---------------------------------------------------------------------------

def extract_zip_to_temp(zip_path: str) -> str:
    tmp = tempfile.mkdtemp(prefix="fbx2s2_zip_")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(tmp)
    return tmp


def find_fbx_and_zips(root_dir: str) -> list[tuple[str, str | None, str | None]]:
    skip_prefixes = (
        os.path.join(root_dir, "models",    ASSETS_SUBFOLDER),
        os.path.join(root_dir, "materials", ASSETS_SUBFOLDER),
    )
    fbx_list: list[tuple[str, str | None, str | None]] = []

    for dirpath, dirnames, filenames in os.walk(root_dir):
        if any(dirpath.startswith(p) for p in skip_prefixes):
            dirnames.clear()
            continue

        for fname in filenames:
            lname = fname.lower()
            full = os.path.join(dirpath, fname)
            if lname.endswith(".fbx"):
                fbx_list.append((full, None, None))
            elif lname.endswith(".zip"):
                try:
                    tmp_dir = extract_zip_to_temp(full)
                    for dp2, _, fns2 in os.walk(tmp_dir):
                        for fn2 in fns2:
                            if fn2.lower().endswith(".fbx"):
                                # 'full' is added
                                fbx_list.append((os.path.join(dp2, fn2), tmp_dir, full))
                except Exception as e:
                    _warn(f"Could not open ZIP ({fname}): {e}")

    return fbx_list


# ---------------------------------------------------------------------------
# Main group processor
# ---------------------------------------------------------------------------

def process_group(
    group_key: str,
    fbx_paths: list[str],
    root_dir: str,
    tmp_dir: str | None,
    zip_path: str | None,
    original_root: str,
) -> bool:
    has_lod    = len(fbx_paths) > 1
    group_name = os.path.basename(group_key).lower()
    group_name = shorten_group_name(group_name)

    _log(f"\n  [{group_name}]")
    asset_type = get_asset_type(fbx_paths[0], original_root, tmp_dir, zip_path)

    model_dir    = os.path.join(root_dir, "models",    ASSETS_SUBFOLDER, group_name)
    material_dir = os.path.join(root_dir, "materials", ASSETS_SUBFOLDER, group_name)
    os.makedirs(model_dir,    exist_ok=True)
    os.makedirs(material_dir, exist_ok=True)

    # Copy FBX files
    fbx_game_paths: list[str] = []
    for fbx_src in fbx_paths:
        fbx_name = os.path.basename(fbx_src).lower()
        fbx_dst  = os.path.join(model_dir, fbx_name)
        _copy_if_missing(fbx_src, fbx_dst)
        fbx_game_paths.append(
            f"models/{ASSETS_SUBFOLDER}/{group_name}/{fbx_name}"
        )

    # Collect textures from same folder as first FBX
    tex_folder   = os.path.dirname(fbx_paths[0])
    all_textures = find_textures_in_dir(tex_folder)

    # FBX from a ZIP: if there are no textures next to it, try the "textures" subfolder
    if not all_textures and tmp_dir is not None:
        textures_subfolder = os.path.join(tex_folder, "textures")
        if os.path.isdir(textures_subfolder):
            all_textures = find_textures_in_dir(textures_subfolder)
            if all_textures:
                _info(f"  Textures taken from the 'textures/' subfolder")

    _info(f"  Textures: {len(all_textures)}")

    # --- Collect material names from ALL LOD files ---
    all_mat_names: list[str] = []
    seen_mat: set[str] = set()
    for fbx_src in fbx_paths:
        for name in extract_fbx_materials(fbx_src):
            if name not in seen_mat:
                seen_mat.add(name)
                all_mat_names.append(name)

    if not all_mat_names:
        # Fallback 1: textures found -> use the Geometry node name (this is what Source 2 does)
        # No textures -> use the raw material name from the FBX (matid_1 etc.)
        if all_textures:
            geo_names: list[str] = []
            seen_geo: set[str] = set()
            for fbx_src in fbx_paths:
                for n in extract_fbx_geometry_names(fbx_src):
                    if n not in seen_geo:
                        seen_geo.add(n)
                        geo_names.append(n)
            if geo_names:
                # with several geometries take the first one (they are LOD parts sharing the same material)
                all_mat_names = [geo_names[0]]
                _info(f"  Used the geometry node name: {geo_names[0]}")
            else:
                all_mat_names = [group_name]
                _info(f"  Material name taken from the group: {group_name}")
        else:
            # no textures: use the raw FBX material name (matid_1 etc.)
            raw_names: list[str] = []
            seen_raw: set[str] = set()
            for fbx_src in fbx_paths:
                for n in extract_fbx_raw_mat_names(fbx_src):
                    if n not in seen_raw:
                        seen_raw.add(n)
                        raw_names.append(n)
            if raw_names:
                all_mat_names = raw_names
                _info(f"  Used the raw FBX material: {raw_names}")
            else:
                all_mat_names = [group_name]
                _info(f"  Material name taken from the group: {group_name}")
    else:
        _info(f"  FBX materials: {all_mat_names}")

    game_prefix = f"materials/{ASSETS_SUBFOLDER}/{group_name}"

    # --- Build a stable texture-name cache ONCE for this group ---------------
    # This ensures every texture always maps to the same safe filename,
    # so the files copied in Pass 1 and the paths written into .vmat in Pass 2
    # are always consistent.
    all_tex_fnames = [os.path.basename(t) for t in all_textures]
    name_cache = build_texture_name_cache(all_tex_fnames, game_prefix)

    # --- Build a per-material slot map ---
    # First, copy all textures once (deduped); opacity textures get converted if needed
    # Then select per-material

    # Pass 1: copy all textures to material_dir; handle opacity conversion
    # We need to detect opacity textures and convert them if not greyscale
    opacity_remap: dict[str, str] = {}  # original_path -> converted_path (may be same)

    for tex_src in all_textures:
        tex_fname = os.path.basename(tex_src)
        dst, _ = build_safe_texture_dst(material_dir, tex_fname, game_prefix, name_cache)
        _copy_if_missing(tex_src, dst)

        # Check if this is an opacity texture
        clf = _classify_texture(tex_src)
        if clf and clf[0] == "TextureLayer1Translucency" and clf[1] == 0:
            # It's an _opacity texture, check if greyscale conversion needed
            stem, ext = os.path.splitext(name_cache.get(tex_fname.lower(), tex_fname.lower()))
            grey_name = stem + "_grey" + ext
            grey_dst  = os.path.join(material_dir, grey_name)
            if convert_opacity_to_greyscale(dst, grey_dst):
                opacity_remap[dst] = grey_dst
            else:
                opacity_remap[dst] = dst  # fallback: use original

    # Build a remapped texture list where opacity textures point to grey versions
    def _remap_textures(tex_list: list[str]) -> list[str]:
        """Replace opacity texture paths with their greyscale equivalents."""
        result = []
        for t in tex_list:
            tex_fname = os.path.basename(t)
            dst, _ = build_safe_texture_dst(material_dir, tex_fname, game_prefix, name_cache)
            grey = opacity_remap.get(dst)
            if grey and grey != dst:
                result.append(grey)
            else:
                result.append(t)
        return result

    # Pass 2: build per-material slot maps
    material_remaps: list[tuple[str, str]] = []

    for mat_name in all_mat_names:
        # Select textures for this material (filtered by mat name)
        slot_map_raw = select_textures_for_slots(all_textures, mat_name, all_mat_names)

        # Build game paths, remapping opacity->greyscale where applicable
        slot_map_game: dict[str, str] = {}
        for slot, tex_src in slot_map_raw.items():
            tex_fname = os.path.basename(tex_src)
            dst, gpath = build_safe_texture_dst(material_dir, tex_fname, game_prefix, name_cache)
            # Remap opacity textures to greyscale version
            if slot == "TextureLayer1Translucency":
                grey = opacity_remap.get(dst)
                if grey and grey != dst:
                    grey_fname = os.path.basename(grey)
                    _, gpath = build_safe_texture_dst(material_dir, grey_fname, game_prefix, name_cache)
            slot_map_game[slot] = gpath

        has_opacity = "TextureLayer1Translucency" in slot_map_game

        # Build vmat
        vmat_filename = f"{mat_name}.vmat"
        vmat_path     = os.path.join(material_dir, vmat_filename)

        if asset_type in ("foliage", "foliage_basic", "foliage_complex"):
            vmdl_game_path = f"models/{ASSETS_SUBFOLDER}/{group_name}/{group_name}.vmdl"
            vmat_content = build_vmat_foliage(slot_map_game, vmdl_game_path)
        else:
            vmat_content = build_vmat_static(slot_map_game, has_opacity)

        try:
            with open(vmat_path, "w", encoding="utf-8") as f:
                f.write(vmat_content)
            _ok(f"VMAT: {game_prefix}/{vmat_filename}")
        except OSError as e:
            _err(f"Could not write VMAT: {e}")
            return False

        vmat_game_path = f"{game_prefix}/{vmat_filename}"

        # Main remap: real material name -> vmat path
        material_remaps.append((mat_name, vmat_game_path))

        # Extra remaps: send every alternative name in the FBX to the same vmat
        # (geometry name, matid_1 etc.) so it works whichever one is used
        extra_names: set[str] = set()
        for fbx_src in fbx_paths:
            for n in extract_fbx_raw_mat_names(fbx_src):
                extra_names.add(n)
            for n in extract_fbx_geometry_names(fbx_src):
                extra_names.add(n)
        for extra in sorted(extra_names):
            if extra != mat_name:
                material_remaps.append((extra, vmat_game_path))
                _info(f"  Extra remap: {extra}.vmat -> {vmat_game_path}")

    # Build VMDL
    max_mesh_verts = calc_max_mesh_vertices(fbx_paths)
    vmdl_content  = build_vmdl(fbx_game_paths, material_remaps, has_lod, asset_type, max_mesh_verts)
    vmdl_filename = f"{group_name}.vmdl"
    vmdl_path     = os.path.join(model_dir, vmdl_filename)

    try:
        with open(vmdl_path, "w", encoding="utf-8") as f:
            f.write(vmdl_content)
        _ok(f"VMDL: models/{ASSETS_SUBFOLDER}/{group_name}/{vmdl_filename}")
    except OSError as e:
        _err(f"Could not write VMDL: {e}")
        return False

    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _copy_if_missing(src: str, dst: str):
    if not os.path.exists(dst):
        shutil.copy2(src, dst)


def _log(msg: str):  print(msg)
def _ok(msg: str):   print(f"  OK      {msg}")
def _info(msg: str): print(f"  INFO    {msg}")
def _warn(msg: str): print(f"  WARN    {msg}")
def _err(msg: str):  print(f"  ERROR   {msg}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(root_dir=None, out_dir=None):
    root_dir = os.path.abspath(root_dir) if root_dir else os.path.dirname(os.path.abspath(__file__))
    # CS2 Porter: the output folder can be chosen separately (empty means the input folder)
    out_dir = os.path.abspath(out_dir) if out_dir else root_dir

    print("=" * 50)
    print("  fbx_to_source2  -  tehlikeli91")
    print("=" * 50)
    print(f"  Folder: {root_dir}")
    if out_dir != root_dir:
        print(f"  Output: {out_dir}")
    print("=" * 50)

    all_fbx_entries = find_fbx_and_zips(root_dir)
    print(f"  FBX found: {len(all_fbx_entries)}")

    if not all_fbx_entries:
        print("  No FBX found.")
        return

    tmp_dirs_to_clean: list[str] = []

    all_fbx_paths = [e[0] for e in all_fbx_entries]
    groups = group_fbx_files(all_fbx_paths)

    fbx_to_tmp: dict[str, str | None] = {e[0]: e[1] for e in all_fbx_entries}
    fbx_to_zip: dict[str, str | None] = {e[0]: e[2] for e in all_fbx_entries}
    
    group_tmp: dict[str, str | None] = {}
    group_zip: dict[str, str | None] = {}
    
    for gk, glist in groups.items():
        group_tmp[gk] = fbx_to_tmp.get(glist[0])
        group_zip[gk] = fbx_to_zip.get(glist[0])
        if fbx_to_tmp.get(glist[0]):
            tmp_dirs_to_clean.append(fbx_to_tmp[glist[0]])

    print(f"  Models to process: {len(groups)}")

    ok_count = err_count = 0
    for group_key, fbx_list in sorted(groups.items()):
        tmp = group_tmp.get(group_key)
        z_path = group_zip.get(group_key)
        try:
            success = process_group(group_key, fbx_list, out_dir, tmp, z_path, root_dir)
            if success:
                ok_count += 1
            else:
                err_count += 1
        except Exception as e:
            _err(f"Unexpected error - {group_key}: {e}")
            import traceback; traceback.print_exc()
            err_count += 1

    for d in set(tmp_dirs_to_clean):
        try:
            shutil.rmtree(d)
        except Exception:
            pass

    print(f"\n{'=' * 50}")
    print(f"  Done   OK: {ok_count}   Failed: {err_count}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()