"""Texture files of material packs (Polyhaven, Megascans, Substance exports ...).

parse() tells the surface a file belongs to and the map it holds, whatever the pack's naming:
    rubber_tiles_diff_1k.jpg            -> rubber_tiles, color
    rubber_tiles_nor_gl_1k.exr          -> rubber_tiles, normal (OpenGL)
    Wild_Grass_pjwgW0_1K_BaseColor.jpg  -> wild_grass_pjwgw0, color
write_texture() copies a map into the addon, as a .png when CS2 cannot take the file as it is
(.exr and the like) or when it has to change (a DirectX normal map gets its green turned around,
an AO packed into an ARM map is taken out of it).
"""

import os
import re
import shutil

import numpy as np
from PIL import Image

# map names -> (slot, tier); a lower tier wins when a surface has two maps for the same slot.
# "other" maps are known but not used (displacement, specular ...).
SLOT_WORDS = {
    "basecolor": ("color", 0), "base_color": ("color", 0), "basecolour": ("color", 0),
    "albedo": ("color", 0), "diffuse": ("color", 0), "color": ("color", 0), "colour": ("color", 0),
    "col": ("color", 0), "bc": ("color", 0), "diff": ("color", 0),
    "normalmap": ("normal", 0), "normal": ("normal", 0), "norm": ("normal", 0), "nrm": ("normal", 0),
    "nor": ("normal", 0), "ddna": ("normal", 0), "nm": ("normal", 0),
    "roughness": ("roughness", 0), "rougness": ("roughness", 0), "rough": ("roughness", 0),
    "rgh": ("roughness", 0), "glossiness": ("roughness", 1), "gloss": ("roughness", 1),
    "ambientocclusion": ("ao", 0), "ambient_occlusion": ("ao", 0), "occlusion": ("ao", 0), "ao": ("ao", 0),
    "cavity": ("ao", 1), "fuzz": ("ao", 1),
    # Polyhaven: AO, roughness and metalness in the red, green and blue channels
    "arm": ("ao", 2),
    "opacity": ("translucency", 0), "translucency": ("translucency", 1), "trans": ("translucency", 1),
    "alpha": ("translucency", 1), "mask": ("translucency", 1),
    "displacement": ("other", 0), "disp": ("other", 0), "height": ("other", 0), "bump": ("other", 0),
    "specular": ("other", 0), "spec": ("other", 0), "metal": ("other", 0), "metallic": ("other", 0),
    "metalness": ("other", 0), "emissive": ("other", 0), "emission": ("other", 0),
}
# normal map conventions: CS2 takes OpenGL (green up) normal maps
VARIANTS = {"gl": "gl", "ogl": "gl", "opengl": "gl", "dx": "dx", "directx": "dx"}
_RES_RE = re.compile(r"^(\d+k|\d{3,5}(px)?)$")
_SPLIT_RE = re.compile(r"[_\-\s.]+")

# files CS2 compiles as they are; the others become .png
DIRECT_EXTS = {".jpg", ".jpeg", ".png", ".tga", ".bmp", ".psd"}
TEXTURE_EXTS = DIRECT_EXTS | {".exr", ".tif", ".tiff", ".webp"}


def parse(path):
    """{"group", "slot", "tier", "variant"} of a texture file, or None when its name holds no map."""
    stem = os.path.splitext(os.path.basename(path))[0]
    toks = [x for x in _SPLIT_RE.split(stem) if x]
    low = [x.lower() for x in toks]
    keep = [i for i, x in enumerate(low) if not _RES_RE.match(x)]
    variant = None
    while keep and low[keep[-1]] in VARIANTS:
        variant = VARIANTS[low[keep.pop()]]
    if not keep:
        return None
    slot, used = None, 0
    if len(keep) >= 2:
        pair = low[keep[-2]] + "_" + low[keep[-1]]
        if pair in SLOT_WORDS:
            slot, used = SLOT_WORDS[pair], 2
    if slot is None and low[keep[-1]] in SLOT_WORDS:
        slot, used = SLOT_WORDS[low[keep[-1]]], 1
    if slot is None:
        return None
    rest = [low[i] for i in keep[:-used]]
    group = "_".join(rest) or stem.lower()
    return {"group": group, "slot": slot[0], "tier": slot[1], "variant": variant}


def rank(path):
    """Sort key of the maps of one slot: lower tier, an OpenGL normal map, a file CS2 takes as it is."""
    info = parse(path) or {"tier": 9, "variant": None}
    ext = os.path.splitext(path)[1].lower()
    return (info["tier"], info["variant"] == "dx", ext not in DIRECT_EXTS, os.path.basename(path).lower())


def pick_slots(paths, slots=("color", "normal", "ao", "translucency", "roughness")):
    """The best file of every slot among the files of one surface."""
    best = {}
    for p in paths:
        info = parse(p)
        if info is None or info["slot"] not in slots:
            continue
        cur = best.get(info["slot"])
        if cur is None or rank(p) < rank(cur):
            best[info["slot"]] = p
    return best


def group_name(paths):
    """The surface name most of the files share."""
    counts = {}
    for p in paths:
        info = parse(p)
        if info:
            counts[info["group"]] = counts.get(info["group"], 0) + 1
    if not counts:
        return ""
    return max(counts, key=lambda g: (counts[g], len(g)))


def out_name(path):
    """File name of the map in the addon (lower case, .png when it has to be converted)."""
    stem, ext = os.path.splitext(os.path.basename(path).lower())
    info = parse(path) or {}
    if ext not in DIRECT_EXTS or info.get("variant") == "dx" or info.get("tier") == 2:
        return stem + ".png"
    return stem + ext


def open_image(path):
    """A PIL image of any texture file; .exr files are read with OpenEXR (color maps are turned
    from linear into sRGB, data maps keep their values)."""
    if os.path.splitext(path)[1].lower() != ".exr":
        img = Image.open(path)
        img.load()
        return img
    import OpenEXR
    with OpenEXR.File(path) as f:
        chans = {k: np.asarray(v.pixels, dtype=np.float32) for k, v in f.channels().items()}
    if "RGB" in chans or "RGBA" in chans:
        arr = chans.get("RGBA", chans.get("RGB"))
    elif "Y" in chans:
        arr = chans["Y"]
    else:
        names = [c for c in ("R", "G", "B", "A") if c in chans] or list(chans)
        arr = np.stack([chans[c] for c in names], axis=-1) if len(names) > 1 else chans[names[0]]
    if (parse(path) or {}).get("slot") == "color":
        a = np.clip(arr, 0.0, 1.0)
        arr = np.where(a <= 0.0031308, a * 12.92, 1.055 * np.power(a, 1 / 2.4) - 0.055)
    arr = (np.clip(arr, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    if arr.ndim == 2:
        return Image.fromarray(arr, "L")
    if arr.shape[2] == 1:
        return Image.fromarray(arr[:, :, 0], "L")
    if arr.shape[2] == 2:
        arr = np.concatenate([arr, np.zeros_like(arr[:, :, :1])], axis=2)
    return Image.fromarray(arr[:, :, :4], "RGBA" if arr.shape[2] >= 4 else "RGB")


def write_texture(src, dst):
    """Copies (or converts) a map to dst (named with out_name)."""
    info = parse(src) or {}
    if os.path.splitext(src)[1].lower() in DIRECT_EXTS and info.get("variant") != "dx" and info.get("tier") != 2:
        shutil.copy2(src, dst)
        return
    img = open_image(src)
    if info.get("tier") == 2 and info.get("slot") == "ao":
        img = img.convert("RGB").getchannel("R")
    elif info.get("slot") == "normal":
        img = img.convert("RGB")
        if info.get("variant") == "dx":
            r, g, b = img.split()
            img = Image.merge("RGB", (r, g.point(lambda v: 255 - v), b))
    elif img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA" if "A" in img.mode else "RGB")
    img.save(dst, "PNG")
