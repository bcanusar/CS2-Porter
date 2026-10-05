"""Post processing: Source 1 color correction and an underwater look as CS2 .vpost layers.

Source 1 color correction (.raw) is a 32x32x32 RGB table (red changes fastest). CS2 bakes the
color correction layers of a .vpost into a table of its own, but its lookup table layer can not
be filled from a file, so the Source 1 table is matched with layers CS2 has (measured by
compiling .vpost files and reading the table they get):
- a Curves layer with one curve per channel: what the table does to greys (both work on the
  0..255 display values, so the curves are exact for greys);
- a Saturation layer for what it does to colors on top of that (-50 halves the color,
  +50 makes it about a third stronger).
"""

import os
import re

import numpy as np

LUT_SIZE = 32
CC_LAYER_NAME = "Color Correction (Source 1)"
_CURVE_STEPS = (0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 31)
_SAT_MIN = 3

KV3_HEAD = ("<!-- kv3 encoding:text:version{e21c7f3c-8a33-41c5-9977-a76d3a32aa0d} "
            "format:generic:version{7412167c-06e9-4698-aff2-e63eb59037e7} -->\n")


def read_lut(data):
    """.raw bytes -> float array [b][g][r][rgb] in 0..1, None when it is not a color table."""
    if not data or len(data) != LUT_SIZE ** 3 * 3:
        return None
    return np.frombuffer(data, dtype=np.uint8).reshape(LUT_SIZE, LUT_SIZE, LUT_SIZE, 3).astype(np.float32) / 255.0


def _layer(cls, name, body):
    return ("\t\t{\n"
            f'\t\t\t_class = "{cls}"\n'
            f'\t\t\tm_name = "{name}"\n'
            "\t\t\tm_nOpacityPercent = 100\n"
            "\t\t\tm_bVisible = true\n"
            "\t\t\tm_pLayerMask = null\n"
            f"{body}\n"
            "\t\t},")


def _curves_text(curves):
    """curves: {"R": [(x, y)], ...} with 0..255 ints."""
    lines = []
    for ch in ("R", "G", "B"):
        pts = curves.get(ch)
        if pts:
            lines.append(f"\t\t\tm_curvePoints{ch} = [ " + ", ".join(f"[ {x}, {y} ]" for x, y in pts) + " ]")
    return "\n".join(lines)


def cc_layers(lut, weight=1.0):
    """Layer texts that give about the same look as a Source 1 color correction table.
    weight: how strong it is (maxweight of the entity)."""
    weight = min(max(float(weight), 0.0), 1.0)
    idx = np.arange(LUT_SIZE)
    grey_in = idx / (LUT_SIZE - 1.0)
    grey_out = lut[idx, idx, idx]            # (32, 3)
    grey_out = grey_in[:, None] + (grey_out - grey_in[:, None]) * weight
    curves = {}
    for c, ch in enumerate(("R", "G", "B")):
        pts = []
        for i in _CURVE_STEPS:
            x = int(round(grey_in[i] * 255))
            y = int(round(min(max(grey_out[i, c], 0.0), 1.0) * 255))
            pts.append((x, y))
        if any(abs(x - y) > 1 for x, y in pts):
            curves[ch] = pts
    layers = []
    if curves:
        layers.append(_layer("CCurvesColorCorrectionLayer", CC_LAYER_NAME, _curves_text(curves)))
    # colors: how much stronger or weaker they come out than the curves alone make them
    b, g, r = np.meshgrid(idx, idx, idx, indexing="ij")
    rgb_in = np.stack([r, g, b], axis=-1) / (LUT_SIZE - 1.0)
    curved = np.stack([np.interp(rgb_in[..., c], grey_in, grey_out[:, c]) for c in range(3)], axis=-1)
    out = rgb_in + (lut - rgb_in) * weight
    chroma_curved = curved.max(-1) - curved.min(-1)
    chroma_out = out.max(-1) - out.min(-1)
    m = chroma_curved > 0.15
    if m.any():
        ratio = float(np.median(chroma_out[m] / chroma_curved[m]))
        sat = (ratio - 1.0) / 0.01 if ratio < 1.0 else (ratio - 1.0) / 0.0064
        sat = int(round(max(-100.0, min(100.0, sat))))
        if abs(sat) >= _SAT_MIN:
            layers.append(_layer("CVibranceColorCorrectionLayer", CC_LAYER_NAME + " 2",
                                 f"\t\t\tm_nVibrance = 0\n\t\t\tm_nSaturation = {sat}"))
    return layers


def _layers_span(text):
    """(start, end) of the inside of m_layers = [ ... ] in a .vpost text."""
    m = re.search(r"m_layers\s*=\s*\[", text)
    if not m:
        return None
    depth, i = 1, m.end()
    while i < len(text) and depth:
        ch = text[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        i += 1
    return (m.end(), i - 1) if depth == 0 else None


def add_layers(path, layers, first=True):
    """Puts the layers into a .vpost (in front of the ones it has, as they act first). Layers of
    an earlier port (same names) are not added twice. Returns True when the file changed."""
    if not layers or not os.path.isfile(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if CC_LAYER_NAME in text:
        return False
    span = _layers_span(text)
    if span is None:
        return False
    start, end = span
    inner = text[start:end]
    add = "\n" + "\n".join(layers)
    if inner.strip():
        new_inner = (add + inner) if first else (inner.rstrip() + add + "\n\t")
    else:
        new_inner = add + "\n\t"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text[:start] + new_inner + text[end:])
    return True


UNDERWATER_DIR = "postprocess"
UNDERWATER_NAME = "cs2porter_underwater"
# tone mapping and bloom of a hand made underwater post processing that looks right in game
_UNDERWATER_REST = """\t\t{
\t\t\t_class = "CToneMappingLayer"
\t\t\tm_name = "Tone Mapping 1"
\t\t\tm_nOpacityPercent = 100
\t\t\tm_bVisible = true
\t\t\tm_pLayerMask = null
\t\t\tm_params =
\t\t\t{
\t\t\t\tm_flExposureBias = -1.0
\t\t\t\tm_flShoulderStrength = 0.0
\t\t\t\tm_flLinearStrength = 0.001
\t\t\t\tm_flLinearAngle = 0.001
\t\t\t\tm_flToeStrength = 1.0
\t\t\t\tm_flToeNum = 1.0
\t\t\t\tm_flToeDenom = 1.0
\t\t\t\tm_flWhitePoint = 1.4985572
\t\t\t\tm_flLuminanceSource = 0.0
\t\t\t\tm_flExposureBiasShadows = 0.0
\t\t\t\tm_flExposureBiasHighlights = 0.0
\t\t\t\tm_flMinShadowLum = 0.0
\t\t\t\tm_flMaxShadowLum = 0.5
\t\t\t\tm_flMinHighlightLum = 2.0
\t\t\t\tm_flMaxHighlightLum = 8.0
\t\t\t}
\t\t},
\t\t{
\t\t\t_class = "CBloomLayer"
\t\t\tm_name = "Bloom 1"
\t\t\tm_nOpacityPercent = 100
\t\t\tm_bVisible = true
\t\t\tm_pLayerMask = null
\t\t\tm_params =
\t\t\t{
\t\t\t\tm_blendMode = "BLOOM_BLEND_ADD"
\t\t\t\tm_flBloomStrength = 3.3730001
\t\t\t\tm_flScreenBloomStrength = 1.0
\t\t\t\tm_flBlurBloomStrength = 1.0
\t\t\t\tm_flBloomThreshold = 0.0
\t\t\t\tm_flBloomThresholdWidth = 0.0
\t\t\t\tm_flSkyboxBloomStrength = 1.0
\t\t\t\tm_flBloomStartValue = 1.0
\t\t\t\tm_flComputeBloomStrength = 0.03
\t\t\t\tm_flComputeBloomThreshold = 1.0
\t\t\t\tm_flComputeBloomRadius = 0.6
\t\t\t\tm_flComputeBloomEffectsScale = 1.0
\t\t\t\tm_flComputeBloomLensDirtStrength = 0.0
\t\t\t\tm_flComputeBloomLensDirtBlackLevel = 0.1
\t\t\t\tm_flBlurWeight =
\t\t\t\t[
\t\t\t\t\t0.2, 0.2, 0.2, 0.2,
\t\t\t\t\t0.2,
\t\t\t\t]
\t\t\t\tm_vBlurTint =
\t\t\t\t[
\t\t\t\t\t[ 1.0, 1.0, 1.0 ],
\t\t\t\t\t[ 1.0, 1.0, 1.0 ],
\t\t\t\t\t[ 1.0, 1.0, 1.0 ],
\t\t\t\t\t[ 1.0, 1.0, 1.0 ],
\t\t\t\t\t[ 1.0, 1.0, 1.0 ],
\t\t\t\t]
\t\t\t}
\t\t},"""
# how far the picture is pulled toward the water's fog color, and how much color it loses
UNDERWATER_TINT = 0.45
UNDERWATER_SATURATION = -25


def underwater_vpost(fog):
    """.vpost text for the camera under water: the picture tinted toward the fog color (r, g, b
    in 0..1) and a little less colorful, darker, with a strong glow."""
    peak = max(max(fog), 1e-6)
    curves = {}
    for ch, v in zip(("R", "G", "B"), fog):
        k = 1.0 - UNDERWATER_TINT + UNDERWATER_TINT * max(v, 0.0) / peak
        curves[ch] = [(0, 0), (128, int(round(128 * k))), (255, int(round(255 * k)))]
    layers = [
        _layer("CCurvesColorCorrectionLayer", "Water Color", _curves_text(curves)),
        _layer("CVibranceColorCorrectionLayer", "Water Saturation",
               f"\t\t\tm_nVibrance = 0\n\t\t\tm_nSaturation = {UNDERWATER_SATURATION}"),
        _UNDERWATER_REST,
    ]
    return KV3_HEAD + '{\n\t_class = "CPostProcessData"\n\tm_layers = \n\t[\n' + "\n".join(layers) + "\n\t]\n}\n"


def write_underwater(content_dir, fogs):
    """One .vpost per water fog color. fogs: {vmat: (r, g, b)}. Returns {vmat: 'postprocess/x.vpost'}."""
    out, by_color = {}, {}
    for vmat, fog in sorted(fogs.items()):
        key = tuple(round(c, 3) for c in fog)
        if key not in by_color:
            n = len(by_color) + 1
            rel = f"{UNDERWATER_DIR}/{UNDERWATER_NAME}{'' if n == 1 else '_' + str(n)}.vpost"
            path = os.path.join(content_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(underwater_vpost(fog))
            by_color[key] = rel
        out[vmat] = by_color[key]
    return out


_FOG_RE = re.compile(r'"?g_vWaterFogColor"?\s+"\[([^\]]*)\]"')


def water_fog(vmat_path):
    """Fog color of a water .vmat, None when it has none."""
    try:
        with open(vmat_path, "r", encoding="utf-8", errors="replace") as f:
            m = _FOG_RE.search(f.read())
    except OSError:
        return None
    if not m:
        return None
    vals = [float(x) for x in m.group(1).split()[:3]]
    return tuple(vals) if len(vals) == 3 else None
