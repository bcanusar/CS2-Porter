"""Moves a material between the CS2 world shaders csgo_lightmappedgeneric, csgo_environment and
csgo_complex without losing its textures.

A material is read into its parts (color, normal, roughness, ambient occlusion, translucency,
metalness, tint, tint mask, height, texture transform ...) under each shader's own parameter
names, then written in the target shader's layout. Parts the target shader has no slot for are
not dropped: they stay in the file as '//~ Name "value"' lines under a header naming the shader
they belong to, so converting back brings them back.

Materials that use something the target cannot show (layers / blending, self illumination,
animation, detail textures, vertex color ...) are left as they are; convert() says why.
"""

import re

LMG = "csgo_lightmappedgeneric.vfx"
ENV = "csgo_environment.vfx"
COMPLEX = "csgo_complex.vfx"
SHADERS = (LMG, ENV, COMPLEX)

DEFAULT_NORMAL = "materials/default/default_normal.tga"
DEFAULT_AO = "materials/default/default_ao.tga"
ROUGH_FULL = "[1.000000 1.000000 1.000000 0.000000]"
WHITE = "[1.000000 1.000000 1.000000 0.000000]"

# part -> parameter name in each shader (None: the shader has no slot for it)
PARAMS = {
    "color":       {LMG: "TextureLayer1Color", ENV: "TextureColor1", COMPLEX: "TextureColor"},
    "normal":      {LMG: "TextureLayer1Normal", ENV: "TextureNormal1", COMPLEX: "TextureNormal"},
    "roughness":   {LMG: "TextureLayer1Roughness", ENV: "TextureRoughness1", COMPLEX: "TextureRoughness"},
    "ao":          {LMG: "TextureLayer1AmbientOcclusion", ENV: "TextureAmbientOcclusion1",
                    COMPLEX: "TextureAmbientOcclusion"},
    "translucency": {LMG: "TextureLayer1Translucency", ENV: "TextureTranslucency1",
                     COMPLEX: "TextureTranslucency"},
    "metal_tex":   {LMG: None, ENV: "TextureMetalness1", COMPLEX: "TextureMetalness"},
    "tintmask":    {LMG: None, ENV: "TextureTintMask1", COMPLEX: "TextureTintMask"},
    "height":      {LMG: None, ENV: "TextureHeight1", COMPLEX: None},
    "tint":        {LMG: "g_vColorTint", ENV: "g_vColorTint", COMPLEX: "g_vColorTint"},
    "layertint":   {LMG: "g_vLayer1Tint", ENV: None, COMPLEX: None},
    "tintamount":  {LMG: "g_flModelTintAmount", ENV: "g_flModelTintAmount", COMPLEX: "g_flModelTintAmount"},
    "metalness":   {LMG: "g_flMetalness", ENV: None, COMPLEX: "g_flMetalness"},
    "opacity":     {LMG: "g_flOpacityScale", ENV: None, COMPLEX: "g_flOpacityScale"},
    "fog":         {LMG: "g_bFogEnabled", ENV: "g_bFogEnabled", COMPLEX: "g_bFogEnabled"},
    "address_u":   {LMG: "g_nTextureAddressModeU", ENV: "g_nTextureAddressModeU", COMPLEX: "g_nTextureAddressModeU"},
    "address_v":   {LMG: "g_nTextureAddressModeV", ENV: "g_nTextureAddressModeV", COMPLEX: "g_nTextureAddressModeV"},
    "tc_scale":    {LMG: None, ENV: "g_vTexCoordScale1", COMPLEX: "g_vTexCoordScale"},
    "tc_offset":   {LMG: None, ENV: "g_vTexCoordOffset1", COMPLEX: "g_vTexCoordOffset"},
    "tc_center":   {LMG: None, ENV: "g_vTexCoordCenter1", COMPLEX: "g_vTexCoordCenter"},
    "tc_rotation": {LMG: None, ENV: "g_flTexCoordRotation1", COMPLEX: "g_flTexCoordRotation"},
    "tc_scroll":   {LMG: None, ENV: None, COMPLEX: "g_vTexCoordScrollSpeed"},
    "scale_u":     {LMG: None, ENV: "g_nScaleTexCoordUByModelScaleAxis", COMPLEX: "g_nScaleTexCoordUByModelScaleAxis"},
    "scale_v":     {LMG: None, ENV: "g_nScaleTexCoordVByModelScaleAxis", COMPLEX: "g_nScaleTexCoordVByModelScaleAxis"},
    "vc_opacity":  {LMG: "g_flVertexColorOpacityScale", ENV: None, COMPLEX: None},
}
_ALIAS = {}
for _part, _names in PARAMS.items():
    for _sh, _n in _names.items():
        if _n:
            _ALIAS.setdefault((_sh, _n.lower()), _part)

# features every one of the three keeps (where the shader has it)
_SHARED_FEATURES = {"F_TRANSLUCENT", "F_ALPHA_TEST", "F_RENDER_BACKFACES", "F_METALNESS_TEXTURE", "F_TINT_MASK",
                    "F_DO_NOT_CAST_SHADOWS", "F_SPECULAR"}
_NO_SHADOWS = {ENV, COMPLEX}

_KEPT_HEAD_RE = re.compile(r"^\s*//---- Kept for (\S+\.vfx)")
_KEPT_RE = re.compile(r'^\s*//~\s*(\w+)\s+"([^"]*)"')
# keys may be written bare (shader "x") or quoted ("Shader" "x"), both are valid vmat text
_PARAM_RE = re.compile(r'^\s*"?(\w+)"?\s+(?:"([^"]*)"|(\S+))')
_SHADER_RE = re.compile(r'^\s*"?shader"?\s+"([^"]+)"', re.I)


class Material:
    """The parts of a material read from its vmat text."""

    def __init__(self):
        self.shader = ""
        self.parts = {}         # part -> (value, shader, name)
        self.features = {}      # F_NAME -> value
        self.raw = []           # (shader, name, value) parameters without a part
        self.blocks = []        # nested blocks (text), VariableState left out


def shader_name(name):
    name = name.strip().lower()
    return name if name.endswith(".vfx") else name + ".vfx"


def parse(text):
    """Reads vmat text into a Material."""
    m = Material()
    lines = text.replace("\r\n", "\n").split("\n")
    depth = 0
    kept_shader = None
    i = 0
    pending_name = None
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if depth == 1 and s == "{" and pending_name is not None:
            # a nested block: copy it whole
            start = i - 1
            d = 0
            j = i
            while j < len(lines):
                d += lines[j].count("{") - lines[j].count("}")
                if d <= 0:
                    break
                j += 1
            if pending_name.lower() != "variablestate":
                m.blocks.append("\n".join(lines[start:j + 1]))
            pending_name = None
            i = j + 1
            continue
        if pending_name is not None and depth == 1:
            # the previous word was a parameter without a value: keep it
            m.raw.append((m.shader, pending_name, ""))
            pending_name = None
        if s == "{":
            depth += 1
        elif s == "}":
            depth -= 1
        elif depth == 1:
            h = _KEPT_HEAD_RE.match(line)
            if h:
                kept_shader = shader_name(h.group(1))
            elif s.startswith("//"):
                k = _KEPT_RE.match(line)
                if k and kept_shader:
                    _add(m, kept_shader, k.group(1), k.group(2), kept=True)
                elif not s.startswith("//~"):
                    kept_shader = None if s.startswith("//----") else kept_shader
            else:
                kept_shader = None
                sh = _SHADER_RE.match(line)
                if sh:
                    m.shader = shader_name(sh.group(1))
                else:
                    p = _PARAM_RE.match(line)
                    if p:
                        val = p.group(2) if p.group(2) is not None else p.group(3)
                        if val.startswith("//"):
                            val = None
                        if val is None:
                            pending_name = p.group(1)
                        else:
                            _add(m, m.shader, p.group(1), val)
                    elif re.fullmatch(r'"?\w+"?', s):
                        pending_name = s.strip('"')
        i += 1
    return m


def _add(m, shader, name, value, kept=False):
    if not kept and name.upper().startswith("F_"):
        m.features[name.upper()] = value
        return
    part = _ALIAS.get((shader, name.lower()))
    if part:
        if part not in m.parts or not kept:
            m.parts[part] = (value, shader, name)
    else:
        m.raw.append((shader, name, value))


def _vec(v):
    try:
        n = [float(x) for x in v.strip("[] ").split()]
    except ValueError:
        return None
    return n + [0.0] * (4 - len(n)) if len(n) <= 4 else None


def _on(v):
    try:
        return float(v) != 0
    except (TypeError, ValueError):
        return False


def blockers(m, target, alphatest=False):
    """Why m cannot go to the target shader (an empty list when it can)."""
    why = []
    if m.shader not in SHADERS:
        why.append(m.shader or "?")
        return why
    if "color" not in m.parts:
        why.append("no color texture")
    if m.shader != target:
        for f, v in m.features.items():
            if _on(v) and f not in _SHARED_FEATURES:
                why.append(f)
        if target == ENV and _on(m.features.get("F_TRANSLUCENT")) and not alphatest:
            why.append("F_TRANSLUCENT")
    return why


def build(m, target, alphatest=False):
    """vmat text of m in the target shader's layout. alphatest: a translucent material is made
    alpha tested (Source 1 $alphatest) where the target cannot blend it."""
    T = "\t"
    parts = dict(m.parts)
    used = set()

    def val(part, default=None):
        v = parts.get(part)
        return v[0] if v else default

    def name(part):
        return PARAMS[part][target]

    def put(lines, part, default=None):
        v = val(part, default)
        if v is None or not name(part):
            return
        used.add(part)
        lines.append(f'{T}{name(part)} "{v}"')

    # one tint where the target has no layer tint
    if not PARAMS["layertint"][target] and "layertint" in parts:
        a, b = _vec(val("tint", WHITE)), _vec(val("layertint"))
        if a and b:
            c = [a[k] * b[k] for k in range(3)]
            parts["tint"] = (f"[{c[0]:.6f} {c[1]:.6f} {c[2]:.6f} 0.000000]", target, name("tint"))
            used.add("layertint")
    # metalness: environment takes it as a texture
    if target == ENV and "metal_tex" not in parts and "metalness" in parts:
        try:
            f = float(val("metalness"))
            parts["metal_tex"] = (f"[{f:.6f} {f:.6f} {f:.6f} 0.000000]", ENV, name("metal_tex"))
            used.add("metalness")
        except ValueError:
            pass

    # a flat metalness texture ('[f f f 0]') becomes the metalness value where the target has one
    if target != ENV and "metal_tex" in parts and PARAMS["metalness"][target]:
        v = _vec(val("metal_tex"))
        if v and val("metal_tex").startswith("[") and v[0] == v[1] == v[2]:
            parts["metalness"] = (f"{v[0]:.3f}", target, name("metalness"))
            del parts["metal_tex"]
    on = lambda f: _on(m.features.get(f))     # noqa: E731
    translucent = on("F_TRANSLUCENT")
    alpha = on("F_ALPHA_TEST") or (translucent and (alphatest or target == ENV))
    if alpha and alphatest:
        translucent = False
    if target == ENV:
        translucent = False
    feats = []
    if translucent:
        feats.append("F_TRANSLUCENT")
    if alpha and not translucent:
        feats.append("F_ALPHA_TEST")
    if on("F_RENDER_BACKFACES"):
        feats.append("F_RENDER_BACKFACES")
    if target in _NO_SHADOWS and on("F_DO_NOT_CAST_SHADOWS"):
        feats.append("F_DO_NOT_CAST_SHADOWS")
    if target == COMPLEX and "metal_tex" in parts:
        feats.append("F_METALNESS_TEXTURE")
    if target == COMPLEX and "tintmask" in parts:
        feats.append("F_TINT_MASK")
    if target == COMPLEX and on("F_SPECULAR"):
        feats.append("F_SPECULAR")
    if m.shader == target:
        # same shader: every other feature stays as it was
        feats += [f for f, v in m.features.items() if _on(v) and f not in feats and f not in _SHARED_FEATURES]
    feats_off = {f for f, v in m.features.items() if not _on(v)}

    lines = ["// THIS FILE IS AUTO-GENERATED", "", "Layer0", "{", f'{T}shader "{target}"', ""]
    if feats:
        lines += [f"{T}//---- Features ----"] + [f"{T}{f} 1" for f in feats] + [""]

    def section(title, plist):
        body = []
        for p, d in plist:
            put(body, p, d)
        if body:
            lines.extend([f"{T}//---- {title} ----"] + body + [""])

    if target == LMG:
        section("Color", [("tintamount", "1.000"), ("vc_opacity", "1.000"), ("tint", WHITE)])
        section("Fog", [("fog", "1")])
        section("Lighting", [("metalness", "0.000")])
        pbr = [("layertint", WHITE), ("ao", DEFAULT_AO), ("color", None), ("normal", DEFAULT_NORMAL),
               ("roughness", ROUGH_FULL)]
        if translucent or alpha:
            pbr.append(("translucency", None))
        section("PBR 1", pbr)
        section("Texture Address Mode", [("address_u", "0"), ("address_v", "0")])
        if translucent:
            section("Translucent", [("opacity", "1.000")])
    elif target == ENV:
        section("Color", [("tint", WHITE), ("tintamount", "1.000")])
        section("Fog", [("fog", "1")])
        mat = [("color", None), ("normal", DEFAULT_NORMAL), ("roughness", ROUGH_FULL), ("metal_tex", None),
               ("ao", DEFAULT_AO), ("height", None), ("tintmask", None), ("tc_rotation", None),
               ("tc_center", None), ("tc_offset", None), ("tc_scale", None), ("scale_u", None), ("scale_v", None)]
        if alpha:
            mat.insert(5, ("translucency", None))
        body = []
        for p, d in mat:
            put(body, p, d)
        if not any(r[1].lower() == "g_nuvset1" for r in m.raw):
            body.append(f'{T}g_nUVSet1 "1"')
        lines.extend([f"{T}//---- Material1 ----"] + body + [""])
        section("Texture Address Mode", [("address_u", "0"), ("address_v", "0")])
    else:
        section("Ambient Occlusion", [("ao", DEFAULT_AO)])
        section("Color", [("tintamount", "1.000"), ("tc_rotation", None), ("scale_u", None), ("scale_v", None),
                          ("tint", WHITE), ("tc_center", None), ("tc_offset", None), ("tc_scale", None),
                          ("tc_scroll", None), ("color", None)])
        section("Fog", [("fog", "1")])
        section("Lighting", [("metalness", "0.000"), ("roughness", ROUGH_FULL)])
        if "metal_tex" in parts:
            section("Metalness", [("metal_tex", None)])
        section("Normal Map", [("normal", DEFAULT_NORMAL)])
        if "tintmask" in parts:
            section("Tint Mask", [("tintmask", None)])
        if translucent or alpha:
            section("Translucent", [("opacity", "1.000") if translucent else ("opacity", None),
                                    ("translucency", None)])
        section("Texture Address Mode", [("address_u", "0"), ("address_v", "0")])

    # parameters without a part: active on their own shader, kept otherwise
    kept = {}
    active = []
    for sh, n, v in m.raw:
        if sh == target:
            active.append(f'{T}{n} "{v}"')
        else:
            kept.setdefault(sh, []).append((n, v))
    for part, (v, sh, n) in parts.items():
        if part in used:
            continue
        kept.setdefault(sh, []).append((n, v))
    for f in sorted(feats_off):
        if m.shader == target:
            active.append(f"{T}{f} 0")
    if active:
        lines += [f"{T}//---- Other ----"] + active + [""]
    for sh, items in kept.items():
        lines.append(f"{T}//---- Kept for {sh} (this shader does not use them) ----")
        seen = set()
        for n, v in items:
            if n.lower() not in seen:
                seen.add(n.lower())
                lines.append(f'{T}//~ {n} "{v}"')
        lines.append("")
    for b in m.blocks:
        lines += [b, ""]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n}\n"


def convert(text, target, alphatest=False):
    """(new text, []) or (None, reasons) when the material cannot go to the target shader."""
    target = shader_name(target)
    m = parse(text)
    why = blockers(m, target, alphatest)
    if why:
        return None, why
    return build(m, target, alphatest), []


def shader_of(text):
    """Shader file name of vmat text ('csgo_complex.vfx'), '' when it names none."""
    for line in text.splitlines():
        s = _SHADER_RE.match(line)
        if s:
            return shader_name(s.group(1))
    return ""
