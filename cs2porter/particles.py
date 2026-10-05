"""Source 1 particle systems (.pcf) used by the map's info_particle_system entities -> CS2
particle systems (.vpcf).

The particle files that define the systems the map uses are found (the map's own files first,
then the installed games) and given to the Source 1 importer of CS2 in a temporary game folder,
with the materials and textures they use. It writes particles/<pcf name>/<system>.vpcf and the
textures (tga + vtex) into the addon. Every particle file holds many systems, so only the ones
the map uses and their children are kept, with the textures they draw. The .vmap step then
gives the entities the paths of their new systems (see vmap.set_particle_names).
"""

import os
import re
import shutil

from .materials import norm_tex, parse_vmt
from .s1stub import GAMEINFO

STUB_DIR = "s1particles"
REMAP_FILE = "source1import_name_remap_table.txt"
REMAP_FILES = (REMAP_FILE, "source1import_econitems_name_remap_table.txt")
WHITE_TEXTURE = "materials/cs2porter/particle_white"
# texture keys of particle materials (spritecard, unlitgeneric ...)
_TEX_KEYS = ("$basetexture", "$basetexture2", "$normalmap", "$bumpmap", "$ramptexture", "$dualsequence")

_VMT_RE = re.compile(rb"([A-Za-z0-9_\-./\\ ]+\.vmt)\x00", re.I)
_CHILD_RE = re.compile(r'm_ChildRef\s*=\s*resource:"([^"]+)"')
_TEXTURE_RE = re.compile(r'resource:"(materials/[^"]+\.vtex)"')
_REMAP_RE = re.compile(r'"([^"]+)"\s+"(particles/[^"]+\.vpcf)"')


def wanted_systems(root):
    """{lower case name: name} of the Source 1 systems the map's info_particle_system entities
    show (the ones the program made itself already point at a .vpcf)."""
    out = {}
    for ent in root.children("entity"):
        if ent.classname != "info_particle_system":
            continue
        name = (ent.get("effect_name") or "").strip()
        if name and not name.lower().endswith(".vpcf"):
            out.setdefault(name.lower(), name)
    return out


def _defines(data, names):
    """Names (lower case) of the systems a particle file holds (a plain search of its strings)."""
    low = data.lower()
    return {n for n in names if re.search(rb"(?<![\x20-\x7e])" + re.escape(n.encode("latin-1", "replace"))
                                          + rb"\x00", low)}


def _pcf_files(sources, embedded_dir):
    """(label, relative path, reader) of every particle file: the map's own ones first, then the
    ones of the installed games."""
    out = []
    if embedded_dir:
        base = os.path.join(embedded_dir, "particles")
        for dp, _dn, fn in os.walk(base):
            for f in sorted(fn):
                if f.lower().endswith(".pcf"):
                    full = os.path.join(dp, f)
                    rel = os.path.relpath(full, embedded_dir).replace("\\", "/").lower()
                    out.append((rel, lambda p=full: open(p, "rb").read()))
    for src in sources.sources:
        if src.role == "embedded":
            continue
        if src.kind == "vpk":
            try:
                rels = [r for r in src.arc.entries if r.startswith("particles/") and r.endswith(".pcf")]
            except Exception:  # noqa: BLE001 - a broken package has no particles for us
                continue
            for rel in sorted(rels):
                out.append((rel, lambda s=src, r=rel: s.read(r)))
        else:
            base = os.path.join(src.root, "particles")
            for dp, _dn, fn in os.walk(base):
                for f in sorted(fn):
                    if f.lower().endswith(".pcf"):
                        full = os.path.join(dp, f)
                        rel = os.path.relpath(full, src.root).replace("\\", "/").lower()
                        out.append((rel, lambda p=full: open(p, "rb").read()))
    return out


def find_pcfs(sources, embedded_dir, names):
    """{relative path: data} of the particle files that define the wanted systems (the first
    file that has a system wins), and the set of names that were found."""
    left = set(names)
    picked, taken = {}, set()
    for rel, read in _pcf_files(sources, embedded_dir):
        if not left:
            break
        name = os.path.basename(rel)
        if name in taken:
            continue        # a file of a later source with the same name is not loaded by the game
        try:
            data = read()
        except Exception:  # noqa: BLE001
            continue
        if not data:
            continue
        hit = _defines(data, left)
        if hit:
            picked[rel] = data
            taken.add(name)
            left -= hit
    return picked, set(names) - left


def build_stub(work_dir, sources, pcfs):
    """Temporary Source 1 game folder with the particle files, their manifest and the materials
    and textures they use. Returns the gameinfo folder."""
    root = os.path.join(work_dir, STUB_DIR)
    shutil.rmtree(root, ignore_errors=True)
    gi = os.path.join(root, "csgo")
    os.makedirs(os.path.join(gi, "particles"), exist_ok=True)
    with open(os.path.join(gi, "gameinfo.txt"), "w", encoding="utf-8") as f:
        f.write(GAMEINFO)

    def put(rel, data):
        p = os.path.join(gi, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    lines = []
    for rel, data in pcfs.items():
        name = os.path.basename(rel)
        put(f"particles/{name}", data)
        lines.append(f'\t"file"\t"particles/{name}"')
    with open(os.path.join(gi, "particles", "particles_manifest.txt"), "w", encoding="utf-8") as f:
        f.write("particles_manifest\n{\n" + "\n".join(lines) + "\n}\n")

    def load(rel):
        data = sources.read(rel)[0]
        if data is not None:
            put(rel.lower(), data)
        return data

    done = set()
    for data in pcfs.values():
        for m in _VMT_RE.finditer(data):
            rel = "materials/" + m.group(1).decode("latin-1").replace("\\", "/").lower().lstrip("/")
            if rel in done:
                continue
            done.add(rel)
            vdata = load(rel)
            if vdata is None:
                continue
            try:
                vmt = parse_vmt(vdata, rel, load)
            except Exception:  # noqa: BLE001
                continue
            for key in _TEX_KEYS:
                val = vmt.get(key)
                tex = norm_tex(val) if val else None
                if tex:
                    tdata = sources.read(f"materials/{tex}.vtf")[0]
                    if tdata is not None:
                        put(f"materials/{tex}.vtf", tdata)
    return gi


def remove_stub(work_dir):
    shutil.rmtree(os.path.join(work_dir, STUB_DIR), ignore_errors=True)


def _files(folder):
    out = set()
    for dp, _dn, fn in os.walk(folder):
        for f in fn:
            out.add(os.path.join(dp, f))
    return out


def snapshot(content_dir):
    """Files of the addon before the import (only the new ones are cleaned up afterwards)."""
    return {os.path.normcase(p) for sub in ("particles", "materials")
            for p in _files(os.path.join(content_dir, sub))} | \
        {os.path.normcase(os.path.join(content_dir, f)) for f in REMAP_FILES
         if os.path.isfile(os.path.join(content_dir, f))}


def read_remap(content_dir):
    """{lower case system name: 'particles/x/y.vpcf'} the importer wrote."""
    path = os.path.join(content_dir, REMAP_FILE)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return {}
    return {m.group(1).lower(): m.group(2) for m in _REMAP_RE.finditer(text)}


def _read(content_dir, rel):
    try:
        with open(os.path.join(content_dir, *rel.split("/")), "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _white_texture(content_dir):
    """Plain white sprite texture for systems whose Source 1 material had no texture (drawn
    white there)."""
    from PIL import Image
    tga = os.path.join(content_dir, *WHITE_TEXTURE.split("/")) + ".tga"
    os.makedirs(os.path.dirname(tga), exist_ok=True)
    Image.new("RGBA", (16, 16), (255, 255, 255, 255)).save(tga)
    with open(tga[:-4] + ".vtex", "w", encoding="utf-8", newline="\n") as f:
        f.write(VTEX_TEMPLATE.replace("$FILE", WHITE_TEXTURE + ".tga"))
    return WHITE_TEXTURE + ".vtex"


def _fix_empty_textures(text, white):
    """A renderer whose material had no texture is written switched off with an empty texture:
    it gets the white texture and is switched on (Source 1 draws such a sprite white)."""
    if 'resource:""' not in text:
        return text, False
    out, pos = [], 0
    for m in re.finditer(r'm_hTexture\s*=\s*resource:""', text):
        start = text.rfind('_class = "C_OP_Render', 0, m.start())
        seg = text[pos:m.start()]
        if start >= pos:
            head, tail = text[pos:start], text[start:m.start()]
            tail = re.sub(r'\n[ \t]*m_bDisableOperator = true[ \t]*(?=\n)', "", tail)
            seg = head + tail
        out.append(seg)
        out.append(f'm_hTexture = resource:"{white}"')
        pos = m.end()
    out.append(text[pos:])
    return "".join(out), True


_VISIBILITY_RE = re.compile(r'\n[ \t]*VisibilityInputs\s*=\s*\{[^{}]*\}')


def _drop_visibility(text):
    """Renderers whose visibility is measured at a control point the map entity does not set
    (only control point 0 is placed) draw nothing in the map, while the particle editor shows
    them: that visibility input is dropped."""
    out, changed = [], False
    pos = 0
    for m in _VISIBILITY_RE.finditer(text):
        cp = re.search(r"m_nCPin\s*=\s*(-?\d+)", m.group(0))
        if cp and int(cp.group(1)) > 0:
            out.append(text[pos:m.start()])
            pos = m.end()
            changed = True
    out.append(text[pos:])
    return "".join(out), changed


def finish(content_dir, before, wanted):
    """Keeps the imported systems the map uses (with their children and textures) and removes
    the other new files. Returns ({lower case name: vpcf}, number of kept systems)."""
    remap = read_remap(content_dir)
    names = {n: remap[n] for n in wanted if n in remap}
    keep, todo = set(), list(names.values())
    while todo:
        rel = todo.pop()
        if rel in keep:
            continue
        keep.add(rel)
        text = _read(content_dir, rel)
        if text:
            todo += [c for c in _CHILD_RE.findall(text) if c not in keep]
    textures = set()
    white = None
    for rel in keep:
        text = _read(content_dir, rel)
        if text is None:
            continue
        new, changed = _fix_empty_textures(text, white or WHITE_TEXTURE + ".vtex")
        if changed:
            white = white or _white_texture(content_dir)
        new, hidden = _drop_visibility(new)
        if changed or hidden:
            with open(os.path.join(content_dir, *rel.split("/")), "w", encoding="utf-8", newline="\n") as f:
                f.write(new)
        textures.update(_TEXTURE_RE.findall(new))
    # a texture is kept with every file of its name (the tga, the frames of a sheet, the mks)
    stems = {os.path.normcase(os.path.join(content_dir, *t[:-5].split("/"))) for t in textures}
    keep_paths = {os.path.normcase(os.path.join(content_dir, *r.split("/"))) for r in keep}
    for path in sorted(_files(os.path.join(content_dir, "particles")) | _files(os.path.join(content_dir, "materials"))):
        key = os.path.normcase(path)
        if key in before or key in keep_paths:
            continue
        if key.endswith(".vpcf"):
            os.remove(path)
            continue
        if not any(key.startswith(s) for s in stems):
            os.remove(path)
    for f in REMAP_FILES:
        p = os.path.join(content_dir, f)
        if os.path.normcase(p) not in before and os.path.isfile(p):
            os.remove(p)
    # folders the clean up left empty
    for sub in ("particles", "materials"):
        for dp, _dn, _fn in sorted(os.walk(os.path.join(content_dir, sub)), key=lambda x: -len(x[0])):
            try:
                os.rmdir(dp)
            except OSError:
                pass
    return names, len(keep)


VTEX_TEMPLATE = """<!-- dmx encoding keyvalues2_noids 1 format vtex 1 -->
"CDmeVtex"
{
	"m_inputTextureArray" "element_array"
	[
		"CDmeInputTexture"
		{
			"m_name" "string" "0"
			"m_fileName" "string" "$FILE"
			"m_colorSpace" "string" "srgb"
			"m_typeString" "string" "2D"
		}
	]
	"m_outputTypeString" "string" "2D"
	"m_outputFormat" "string" "DXT5"
	"m_textureOutputChannelArray" "element_array"
	[
		"CDmeTextureOutputChannel"
		{
			"m_inputTextureArray" "string_array"
				[
					"0"
				]
			"m_srcChannels" "string" "rgba"
			"m_dstChannels" "string" "rgba"
			"m_mipAlgorithm" "CDmeImageProcessor"
			{
				"m_algorithm" "string" "Box"
				"m_stringArg" "string" ""
				"m_vFloat4Arg" "vector4" "0 0 0 0"
			}
			"m_outputColorSpace" "string" "srgb"
		}
	]
}
"""
