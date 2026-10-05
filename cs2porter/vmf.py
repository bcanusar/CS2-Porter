"""VMF read / write, content analysis and the fixes made before the CS2 import."""

import collections
import math
import re

_KV_RE = re.compile(r'^"([^"]*)"\s*"(.*)"\s*$')


class VNode:
    __slots__ = ("name", "items")

    def __init__(self, name):
        self.name = name
        self.items = []       # [ [key, value] | VNode ]

    # --- key/value -------------------------------------------------------
    def kvs(self):
        return [i for i in self.items if isinstance(i, list)]

    def children(self, name=None):
        if name is None:
            return [i for i in self.items if isinstance(i, VNode)]
        n = name.lower()
        return [i for i in self.items if isinstance(i, VNode) and i.name.lower() == n]

    def child(self, name):
        n = name.lower()
        for i in self.items:
            if isinstance(i, VNode) and i.name.lower() == n:
                return i
        return None

    def get(self, key, default=None):
        k = key.lower()
        for i in self.items:
            if isinstance(i, list) and i[0].lower() == k:
                return i[1]
        return default

    def set(self, key, value):
        k = key.lower()
        for i in self.items:
            if isinstance(i, list) and i[0].lower() == k:
                i[1] = value
                return
        self.items.append([key, value])

    def remove_key(self, key):
        k = key.lower()
        self.items = [i for i in self.items if not (isinstance(i, list) and i[0].lower() == k)]

    def index_of_key(self, key):
        k = key.lower()
        for idx, i in enumerate(self.items):
            if isinstance(i, list) and i[0].lower() == k:
                return idx
        return -1

    @property
    def classname(self):
        return (self.get("classname") or "").lower()


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

def parse_vmf(path):
    root = VNode("root")
    stack = [root]
    pending = None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("//"):
                continue
            if line == "{":
                node = VNode(pending or "")
                stack[-1].items.append(node)
                stack.append(node)
                pending = None
                continue
            if line == "}":
                if len(stack) > 1:
                    stack.pop()
                pending = None
                continue
            m = _KV_RE.match(line)
            if m:
                stack[-1].items.append([m.group(1), m.group(2)])
                pending = None
            else:
                pending = line
    return root


def write_vmf(root, path):
    out = []

    def emit(node, depth):
        ind = "\t" * depth
        out.append(f"{ind}{node.name}\n{ind}{{\n")
        cind = ind + "\t"
        for item in node.items:
            if isinstance(item, list):
                out.append(f'{cind}"{item[0]}" "{item[1]}"\n')
            else:
                emit(item, depth + 1)
        out.append(f"{ind}}}\n")

    for item in root.items:
        if isinstance(item, VNode):
            emit(item, 0)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(out)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def normalize_material(name):
    n = name.strip().replace("\\", "/").lower()
    while "//" in n:
        n = n.replace("//", "/")
    n = n.lstrip("/")
    if n.startswith("materials/"):
        n = n[len("materials/"):]
    if n.endswith(".vmt"):
        n = n[:-4]
    return n


def normalize_model(name):
    n = name.strip().replace("\\", "/").lower()
    while "//" in n:
        n = n.replace("//", "/")
    n = n.lstrip("/")
    if not n.startswith("models/"):
        n = "models/" + n
    return n


# entity keys -> material usage
_MATERIAL_KEYS = {
    "material": "entity",
    "texture": "entity",
    "ropematerial": "rope",
    "spritename": "sprite",
    "smokematerial": "sprite",
    "detailmaterial": None,     # not used in CS2
}

_SPRITE_CLASSES = {"env_sprite", "env_glow", "env_sprite_oriented", "env_sprite_clientside",
                   "env_lightglow", "env_spritetrail"}
BEAM_CLASSES = ("env_beam", "env_laser")


class VmfInfo:
    def __init__(self):
        self.materials = {}       # mat -> set(usage)
        self.models = {}          # mdl -> count
        self.skyname = ""
        self.brush_count = 0
        self.side_count = 0
        self.disp_count = 0
        self.entity_count = 0
        self.entity_classes = {}

    def add_material(self, name, usage):
        n = normalize_material(name)
        if not n or n.startswith("*"):
            return
        self.materials.setdefault(n, set()).add(usage)

    def add_model(self, name):
        n = normalize_model(name)
        self.models[n] = self.models.get(n, 0) + 1


def _collect_solids(node, info, usage):
    for solid in node.children("solid"):
        info.brush_count += 1
        for side in solid.children("side"):
            info.side_count += 1
            mat = side.get("material")
            if mat:
                info.add_material(mat, usage)
            if side.child("dispinfo") is not None:
                info.disp_count += 1


def analyze(root):
    info = VmfInfo()
    world = root.child("world")
    if world is not None:
        info.skyname = (world.get("skyname") or "").strip()
        _collect_solids(world, info, "brush")
        # solids inside hidden
        for hidden in world.children("hidden"):
            _collect_solids(hidden, info, "brush")

    for ent in root.children("entity"):
        info.entity_count += 1
        cls = ent.classname
        info.entity_classes[cls] = info.entity_classes.get(cls, 0) + 1
        brush_usage = "brush" if cls == "func_detail" else "brushent"
        _collect_solids(ent, info, brush_usage)
        for hidden in ent.children("hidden"):
            _collect_solids(hidden, info, brush_usage)

        for key, val in ent.kvs():
            if not val:
                continue
            k = key.lower()
            vl = val.lower().strip()
            if vl.endswith(".mdl"):
                info.add_model(val)
                continue
            if k == "model" and cls in _SPRITE_CLASSES:
                if vl.endswith(".vmt") or vl.endswith(".spr"):
                    info.add_material(val[:-4] if vl.endswith(".spr") else val, "sprite")
                continue
            if cls == "info_overlay" and k == "material":
                info.add_material(val, "overlay")
                continue
            if cls == "infodecal" and k == "texture":
                info.add_material(val, "decal")
                continue
            if cls in BEAM_CLASSES and k == "texture":
                continue        # beams become particles (vmap.convert_beams)
            if k in _MATERIAL_KEYS:
                usage = _MATERIAL_KEYS[k]
                if usage is None:
                    continue
                if "/" in vl or "\\" in vl or vl.endswith(".vmt"):
                    info.add_material(val, usage)
                continue
            if vl.endswith(".vmt"):
                info.add_material(val, "entity")
    return info


# ---------------------------------------------------------------------------
# Fixes before the CS2 import
# ---------------------------------------------------------------------------

_RENDERFX = {
    "0": "kRenderFxNone", "1": "kRenderFxPulseSlow", "2": "kRenderFxPulseFast",
    "3": "kRenderFxPulseSlowWide", "4": "kRenderFxPulseFastWide", "5": "kRenderFxFadeSlow",
    "6": "kRenderFxFadeFast", "7": "kRenderFxSolidSlow", "8": "kRenderFxSolidFast",
    "9": "kRenderFxStrobeSlow", "10": "kRenderFxStrobeFast", "11": "kRenderFxStrobeFaster",
    "12": "kRenderFxFlickerSlow", "13": "kRenderFxFlickerFast", "14": "kRenderFxNoDissipation",
}
_RENDERMODE = {
    "0": "kRenderNormal", "1": "kRenderTransColor", "2": "kRenderTransTexture",
    "3": "kRenderGlow", "4": "kRenderTransAlpha", "5": "kRenderTransAdd",
    "7": "kRenderTransAddFrameBlend", "9": "kRenderWorldGlow", "10": "kRenderNone",
}


def _fix_base(root):
    """Makes sure versioninfo / visgroups / viewsettings / cordon exist and completes dispinfo."""
    blocks = [i for i in root.items if isinstance(i, VNode)]
    by_name = {}
    for b in blocks:
        by_name.setdefault(b.name.lower(), b)

    mapversion = None
    vi = by_name.get("versioninfo")
    if vi is not None:
        mapversion = vi.get("mapversion")
    world = by_name.get("world")
    if mapversion is None and world is not None:
        mapversion = world.get("mapversion")
    mapversion = mapversion or "1"

    if vi is None:
        vi = VNode("versioninfo")
        vi.items = [["editorversion", "400"], ["editorbuild", "9999"], ["mapversion", mapversion],
                    ["formatversion", "100"], ["prefab", "0"]]
    vg = by_name.get("visgroups") or VNode("visgroups")
    vs = by_name.get("viewsettings")
    if vs is None:
        vs = VNode("viewsettings")
        vs.items = [["bSnapToGrid", "1"], ["bShowGrid", "1"], ["bShowLogicalGrid", "0"],
                    ["nGridSpacing", "64"], ["bShow3DGrid", "0"]]
    head = [vi, vg, vs]
    rest = [i for i in root.items if not (isinstance(i, VNode) and i in head)]
    root.items = head + rest
    if "cordon" not in by_name:
        cordon = VNode("cordon")
        cordon.items = [["mins", "(-1024 -1024 -1024)"], ["maxs", "(1024 1024 1024)"], ["active", "0"]]
        root.items.append(cordon)

    # dispinfo: add the missing offsets / offset_normals blocks
    def patch(node):
        for ch in node.children():
            if ch.name.lower() == "dispinfo":
                _patch_dispinfo(ch)
            else:
                patch(ch)
    patch(root)


def _patch_dispinfo(disp):
    has_off = disp.child("offsets") is not None
    has_offn = disp.child("offset_normals") is not None
    if has_off and has_offn:
        return
    try:
        power = int(disp.get("power", "3"))
    except ValueError:
        power = 3
    r = (1 << power) + 1
    new_blocks = []
    if not has_off:
        b = VNode("offsets")
        b.items = [[f"row{i}", " ".join(["0"] * (3 * r))] for i in range(r)]
        new_blocks.append(b)
    if not has_offn:
        b = VNode("offset_normals")
        b.items = [[f"row{i}", " ".join(["0 0 1"] * r)] for i in range(r)]
        new_blocks.append(b)
    idx = next((n for n, i in enumerate(disp.items) if isinstance(i, VNode) and i.name.lower() == "alphas"), len(disp.items))
    disp.items[idx:idx] = new_blocks


def _fix_special_targetnames(root):
    found = set()

    def scan(node):
        for item in node.items:
            if isinstance(item, list):
                v = item[1]
                if "!" in v:
                    for t in ("!activator", "!self", "!caller"):
                        if t in v:
                            found.add(t)
            else:
                scan(item)
    scan(root)
    if not found:
        return 0
    max_id = 0
    for ent in root.children("entity"):
        try:
            max_id = max(max_id, int(ent.get("id", "0")))
        except ValueError:
            pass
    added = 0
    insert_at = next((n for n, i in enumerate(root.items) if isinstance(i, VNode) and i.name.lower() in ("cameras", "cordon")), len(root.items))
    for t in ("!activator", "!self", "!caller"):
        if t in found:
            max_id += 1
            e = VNode("entity")
            e.items = [["id", str(max_id)], ["classname", "info_target"], ["angles", "0 0 0"],
                       ["spawnflags", "0"], ["targetname", t], ["origin", "0 0 0"]]
            root.items.insert(insert_at, e)
            insert_at += 1
            added += 1
    return added


def _fix_brush_entity(ent):
    cls = ent.classname
    if cls not in ("func_illusionary", "func_wall", "func_wall_toggle", "func_lod"):
        return False
    solid_val = ent.get("solid", "")
    solidity = {"func_illusionary": "1", "func_wall": "2", "func_wall_toggle": "0"}.get(cls, "2")
    if cls == "func_lod":
        solidity = "1" if solid_val == "1" else "2"
        ent.remove_key("solid")
    idx = ent.index_of_key("classname")
    ent.items[idx] = ["classname", "func_brush"]
    ent.items.insert(idx + 1, ["InputFilter", "32"])
    ent.items.insert(idx + 2, ["Solidity", solidity])
    # func_wall / func_lod that stay entities are named after their old class so they are easy
    # to find in Hammer; an existing name is kept because outputs may target it
    if cls in ("func_wall", "func_lod") and not (ent.get("targetname") or "").strip():
        ent.set("targetname", cls)
    return True


# --- func_brush -> mesh ------------------------------------------------------------

MESH_MARKER = "cs2porter_mesh_"


def _outputs(ent):
    """[(output, target, input, param, delay, times)] of an entity."""
    out = []
    for conn in ent.children("connections"):
        for key, val in conn.kvs():
            sep = "\x1b" if "\x1b" in val else ","
            parts = val.split(sep)
            if len(parts) >= 2:
                out.append([key] + parts)
    return out


def _referenced_names(root):
    """Lower case names that something points at: output targets (with wildcards),
    parentname and target keys."""
    exact, prefixes = set(), []
    for ent in root.children("entity"):
        for o in _outputs(ent):
            name = o[1].strip().lower()
            if name.endswith("*"):
                prefixes.append(name[:-1])
            elif name:
                exact.add(name)
        for key in ("parentname", "target", "damagefilter", "lightingorigin"):
            v = (ent.get(key) or "").strip().lower()
            if v:
                exact.add(v.split(",")[0])
    return exact, prefixes


def _is_referenced(name, refs):
    n = name.strip().lower()
    if not n:
        return False
    exact, prefixes = refs
    return n in exact or any(n.startswith(p) for p in prefixes)


_DEFAULT_RENDER = {
    "renderamt": ("", "255"),
    "rendermode": ("", "0", "krendernormal"),
    "renderfx": ("", "0", "krenderfxnone"),
    "rendercolor": ("", "255 255 255"),
    "startdisabled": ("", "0"),
    "parentname": ("",),
}


def _plain_brush(ent, refs):
    """True when the func_brush has no inputs / outputs and default render settings."""
    if _outputs(ent) or _is_referenced(ent.get("targetname") or "", refs):
        return False
    for key, ok in _DEFAULT_RENDER.items():
        if (ent.get(key) or "").strip().lower() not in ok:
            return False
    return True


def _float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _brushes_to_meshes(root, meshes):
    """func_brush / func_wall / func_lod (and the other brush classes that became func_brush)
    without inputs and outputs are marked; vmap.unwrap_entities replaces them with their
    meshes. func_lod always becomes a mesh. meshes receives marker -> settings."""
    refs = _referenced_names(root)
    count = 0
    for ent in root.children("entity"):
        cls = ent.classname
        if cls == "func_lod":
            dist = _float(ent.get("DisappearMaxDist")) or _float(ent.get("DisappearDist"), 2000.0)
            opts = {"fade": (0, dist), "physics_none": (ent.get("solid") or "").strip() == "1"}
        elif cls in ("func_brush", "func_wall", "func_illusionary", "func_wall_toggle"):
            if not _plain_brush(ent, refs):
                continue
            solidity = {"func_wall": "2", "func_illusionary": "1", "func_wall_toggle": "0"}.get(
                cls, (ent.get("Solidity") or "0").strip())
            opts = {"physics_none": solidity == "1"}
        else:
            continue
        if (ent.get("disableshadows") or "").strip() == "1":
            opts["no_shadows"] = True
        count += 1
        marker = f"{MESH_MARKER}{count}"
        meshes[marker] = opts
        ent.items = [i for i in ent.items if not isinstance(i, list) or i[0].lower() in ("id", "origin")]
        ent.items[0:0] = [["classname", "func_brush"], ["targetname", marker], ["Solidity", "2"]]
    return count


# --- trigger_teleport with a landmark -------------------------------------------------

def _teleports_to_bhop(root, teleport_key):
    """trigger_teleport with a landmark -> trigger_multiple whose OnStartTouch sends
    'teleport <target> <landmark>' to the bhop script. The output goes to !activator here
    (the importer drops outputs to entities it does not know) and is redirected in the .vmap."""
    count = 0
    for ent in root.children("entity"):
        if ent.classname != "trigger_teleport":
            continue
        landmark = (ent.get("landmark") or "").strip()
        target = (ent.get("target") or "").strip()
        if not landmark or not target:
            continue
        idx = ent.index_of_key("classname")
        ent.items[idx] = ["classname", "trigger_multiple"]
        for k in ("landmark", "target", "use_landmark_angles", "mirror_player", "check_if_dest_clear_for_player"):
            ent.remove_key(k)
        conns = ent.child("connections")
        if conns is None:
            conns = VNode("connections")
            ent.items.append(conns)
        conns.items.append(["OnStartTouch", f"!activator,AddOutput,{teleport_key} {target} {landmark},0,-1"])
        count += 1
    return count


# --- bhop blocks -------------------------------------------------------------------------

BHOP_BLOCK_NAME = "bhop_block"


def _flag_names(ent):
    """(flag, reset) when every output of a trigger is '!activator AddOutput targetname X' and
    it gives the player one name and then another one a little later (the Source 1 bhop block
    flag), else None."""
    outs = _outputs(ent)
    if len(outs) < 2:
        return None
    timed = []
    for o in outs:
        if len(o) < 4 or o[1].strip().lower() != "!activator" or o[2].strip().lower() != "addoutput":
            return None
        words = o[3].strip().split(None, 1)
        if len(words) != 2 or words[0].lower() != "targetname":
            return None
        timed.append((_float(o[4] if len(o) > 4 else 0, 0.0), words[1].strip()))
    timed.sort(key=lambda x: x[0])
    names = {n.lower() for _d, n in timed}
    if len(names) != 2 or timed[0][0] >= timed[-1][0]:
        return None
    return timed[0][1].lower(), timed[-1][1].lower()


def _bhop_blocks(root, teleport_key):
    """Source 1 bhop blocks: a trigger_multiple on the block gives the player a flag name for a
    moment, and a trigger_teleport with a name filter for that flag teleports a player who
    stays on the block. The flag triggers are removed and every such teleport becomes a
    trigger_multiple 'bhop_block' that the bhop script fires (OnUser1 -> teleport <target>
    [<landmark>]). Returns (blocks, flag triggers removed)."""
    flag_triggers = {}
    for ent in root.children("entity"):
        if ent.classname == "trigger_multiple":
            names = _flag_names(ent)
            if names:
                flag_triggers[id(ent)] = names[0]
    if not flag_triggers:
        return 0, 0
    flags = set(flag_triggers.values())
    filters = set()
    for ent in root.children("entity"):
        if ent.classname == "filter_activator_name" and (ent.get("filtername") or "").strip().lower() in flags \
                and (ent.get("Negated") or "0").strip().lower() in ("0", "allow entities that match criteria"):
            name = (ent.get("targetname") or "").strip().lower()
            if name:
                filters.add(name)
    blocks = 0
    used = set()
    for ent in root.children("entity"):
        if ent.classname != "trigger_teleport":
            continue
        filt = (ent.get("filtername") or "").strip().lower()
        target = (ent.get("target") or "").strip()
        if filt not in filters or not target:
            continue
        landmark = (ent.get("landmark") or "").strip()
        ent.items[ent.index_of_key("classname")] = ["classname", "trigger_multiple"]
        for k in ("targetname", "filtername", "landmark", "target", "use_landmark_angles", "mirror_player",
                  "check_if_dest_clear_for_player"):
            ent.remove_key(k)
        ent.items.insert(ent.index_of_key("classname") + 1, ["targetname", BHOP_BLOCK_NAME])
        ent.items = [i for i in ent.items if not (isinstance(i, VNode) and i.name.lower() == "connections")]
        conns = VNode("connections")
        conns.items.append(["OnUser1", f"!activator,AddOutput,{teleport_key} {target} {landmark}".rstrip()
                            + ",0,-1"])
        ent.items.append(conns)
        used.add(filt)
        blocks += 1
    if not blocks:
        return 0, 0
    # flags checked by the converted teleports: their triggers have nothing left to do
    used_flags = {(f.get("filtername") or "").strip().lower() for f in root.children("entity")
                  if f.classname == "filter_activator_name" and (f.get("targetname") or "").strip().lower() in used}
    before = len(root.items)
    root.items = [i for i in root.items
                  if not (isinstance(i, VNode) and id(i) in flag_triggers and flag_triggers[id(i)] in used_flags)]
    return blocks, before - len(root.items)


def _fix_dynamic_prop(ent):
    cls = ent.classname
    if cls not in ("prop_dynamic", "prop_dynamic_glow", "prop_dynamic_override"):
        return False
    has_hold = ent.index_of_key("HoldAnimation") != -1
    new_items = []
    for item in ent.items:
        if not isinstance(item, list):
            new_items.append(item)
            continue
        k, v = item[0], item[1]
        kl = k.lower()
        if kl == "classname":
            new_items.append(["classname", "prop_dynamic" if v.lower() == "prop_dynamic_glow" else v])
        elif kl == "defaultanim":
            if v:
                new_items.append(["IdleAnim", v])
                if not has_hold:
                    new_items.append(["IdleAnimationLoopMode", "ANIM_LOOP_MODE_NOT_LOOPING"])
        elif kl == "holdanimation":
            new_items.append(["IdleAnimationLoopMode", "ANIM_LOOP_MODE_LOOPING" if v == "1" else "ANIM_LOOP_MODE_NOT_LOOPING"])
        elif kl == "randomanimation":
            new_items.append(["randomizecycle", v])
        elif kl == "animateeveryframe":
            new_items.append(["AnimateOnServer", v])
        elif kl == "glowdist":
            new_items.append(["glowrange", v])
        elif kl == "glowenabled":
            new_items.append(["glowstate", "3" if v == "1" else "0"])
        elif kl in ("minanimtime", "maxanimtime"):
            continue
        else:
            new_items.append(item)
    ent.items = new_items
    return True


def _is_skip_or_hint(mat):
    m = mat.lower().replace("\\", "/")
    return m in ("tools/toolsskip", "tools/toolshint")


def _is_hint_brush(solid):
    """A hint brush has only skip / hint faces. Brushes that also have real faces (decompiled
    func_detail walls often have a skip face) are kept."""
    sides = solid.children("side")
    return bool(sides) and all(_is_skip_or_hint(side.get("material", "")) for side in sides)


def _remove_skip_hint(root):
    """Hint brushes are removed; skip / hint faces on other brushes become nodraw."""
    removed = 0
    replaced = 0
    owners = [root.child("world")] + root.children("entity")
    for owner in owners:
        if owner is None or not owner.children("solid"):
            continue
        before = len(owner.items)
        owner.items = [i for i in owner.items
                       if not (isinstance(i, VNode) and i.name.lower() == "solid" and _is_hint_brush(i))]
        removed += before - len(owner.items)
        for solid in owner.children("solid"):
            for side in solid.children("side"):
                if _is_skip_or_hint(side.get("material", "")):
                    side.set("material", "tools/toolsnodraw")
                    replaced += 1
    return removed, replaced


# ---------------------------------------------------------------------------
# toolsskybox, bhop outputs and light brightness
# ---------------------------------------------------------------------------

SKYBOX_MAT = "tools/toolsskybox"
# toolsskybox2d (sky without the 3D skybox) is a sky face in CS2 too
SKYBOX_MATS = (SKYBOX_MAT, "tools/toolsskybox2d")


def has_sky_faces(materials):
    return any(m in materials for m in SKYBOX_MATS)


def pick_sky_alias(materials):
    """Name of a material the map does not use. Faces with toolsskybox get it during the
    import (the importer drops every face whose material name contains "toolsskybox")
    and get toolsskybox back in the .vmap."""
    used = {m.lower() for m in materials}
    name, n = "cs2porter/brushfix", 1
    while name in used:
        n += 1
        name = f"cs2porter/brushfix{n}"
    return name


def _swap_sky_material(root, alias):
    count = 0
    stack = [root]
    while stack:
        node = stack.pop()
        for ch in node.children():
            if ch.name.lower() == "side":
                if normalize_material(ch.get("material", "")) in SKYBOX_MATS:
                    ch.set("material", alias)
                    count += 1
            else:
                stack.append(ch)
    return count


LADDER_MATS = ("tools/toolsinvisibleladder", "tools/toolsinvisibleladder_wood")


def _ladders_to_world(root):
    """func_ladder does not work in CS2: its brushes become world brushes with a ladder material."""
    world = root.child("world")
    if world is None:
        return 0
    count = 0
    keep = []
    for item in root.items:
        if isinstance(item, VNode) and item.name.lower() == "entity" and item.classname == "func_ladder":
            solids = item.children("solid") + [s for h in item.children("hidden") for s in h.children("solid")]
            for solid in solids:
                for side in solid.children("side"):
                    if normalize_material(side.get("material", "")) not in LADDER_MATS:
                        side.set("material", "TOOLS/TOOLSINVISIBLELADDER")
                world.items.append(solid)
                count += 1
            continue
        keep.append(item)
    root.items = keep
    return count


TRIGGER_MAT = "tools/toolstrigger"
PLAYERCLIP_MAT = "TOOLS/TOOLSPLAYERCLIP"
CONTENTS_PLAYERCLIP = 0x10000
CONTENTS_MONSTERCLIP = 0x20000
_CLIP_MATS = {CONTENTS_PLAYERCLIP: PLAYERCLIP_MAT, CONTENTS_MONSTERCLIP: "TOOLS/TOOLSNPCCLIP",
              CONTENTS_PLAYERCLIP | CONTENTS_MONSTERCLIP: "TOOLS/TOOLSCLIP"}
# brush entities whose toolstrigger brushes are solid walls in Source 1 (a trigger needs a
# trigger entity)
_SOLID_BRUSH_CLASSES = ("func_detail", "func_brush", "func_wall", "func_wall_toggle")


def restore_clip_brushes(root, bsp_brushes):
    """The map decompiler guesses the tool texture of a brush without visible faces and it is
    not always right: player clip brushes come out as toolstrigger or, from one run to the
    next, as toolshint (and hint brushes are removed later). The brushes of the .bsp tell what
    they are: a clip brush gets its clip material back, a hint or skip side of a brush that is
    not a hint brush gets the texture the .bsp has for it.
    bsp_brushes: bsp.BSPInfo.brushes(). Returns (clip brushes, other sides fixed)."""
    from .blend import _outward_planes
    from .bsp import plane_key
    clips = sides_fixed = 0
    by_plane = {}

    def find(keys):
        hit = bsp_brushes.get(frozenset(keys))
        if hit or not keys:
            return hit
        # the compiler can add sides that do not change the shape (the decompiler leaves them out)
        if not by_plane:
            for fs in bsp_brushes:
                for k in fs:
                    by_plane.setdefault(k, []).append(fs)
        cands = None
        for k in keys:
            cands = set(by_plane.get(k, ())) if cands is None else cands & set(by_plane.get(k, ()))
            if not cands:
                return None
        return bsp_brushes[min(cands, key=len)]

    holders = [(root.child("world"), None)]
    for ent in root.children("entity"):
        org = _vec(ent.get("origin")) if ent.children("solid") or ent.children("hidden") else None
        holders.append((ent, org if org and any(org) else None))
    for holder, org in holders:
        if holder is None:
            continue
        # only brushes that are solid walls can be clip brushes (an areaportal or a trigger can
        # sit exactly where a clip brush is)
        can_clip = holder.name.lower() == "world" or holder.classname in _SOLID_BRUSH_CLASSES
        solids = holder.children("solid") + [s for h in holder.children("hidden") for s in h.children("solid")]
        for solid in solids:
            sides = solid.children("side")
            if not sides or any(s.child("dispinfo") is not None for s in sides):
                continue
            planes = _outward_planes(sides)
            if planes is None:
                continue
            match = None
            # brushes of an entity with an origin are stored around that origin in the .bsp
            for off in ((None, org) if org else (None,)):
                keys = [plane_key(n, d - (sum(n[k] * off[k] for k in range(3)) if off else 0.0))
                        for n, d in planes]
                match = find(keys)
                if match:
                    break
            if not match:
                continue
            contents, textures = match
            clip = _CLIP_MATS.get(contents & (CONTENTS_PLAYERCLIP | CONTENTS_MONSTERCLIP)) if can_clip else None
            if clip and all(t.startswith("tools/") for t in textures.values()):
                for side in sides:
                    side.set("material", clip)
                clips += 1
                continue
            for side, key in zip(sides, keys):
                real = textures.get(key, "")
                if _is_skip_or_hint(side.get("material", "")) and real and not _is_skip_or_hint(real):
                    side.set("material", real.upper())
                    sides_fixed += 1
    return clips, sides_fixed


def restore_nonsolid(root, bsp_brushes):
    """Brushes that players walked through in Source 1: the compiler keeps no collision brush for
    a nonsolid brush (only its faces), so a world / func_detail brush that matches no brush of the
    .bsp and whose materials are on no brush of the .bsp was not solid. They are moved into a
    func_illusionary, which becomes a mesh without collision. Returns the number of brushes."""
    from .blend import _outward_planes
    from .bsp import plane_key
    from .texlights import _unpatched
    on_brushes = {_unpatched(t) for _c, tex in bsp_brushes.values() for t in tex.values()}
    by_plane = {}
    for fs in bsp_brushes:
        for k in fs:
            by_plane.setdefault(k, []).append(fs)

    def matched(keys):
        cands = None
        for k in keys:
            cands = set(by_plane.get(k, ())) if cands is None else cands & set(by_plane.get(k, ()))
            if not cands:
                return False
        return bool(cands)

    def nonsolid(sides):
        mats = {normalize_material(s.get("material", "")) for s in sides}
        visible = {m for m in mats if m and not m.startswith("tools/")}
        if not visible or visible & on_brushes or any(s.child("dispinfo") is not None for s in sides):
            return False
        planes = _outward_planes(sides)
        return bool(planes) and not matched([plane_key(n, d) for n, d in planes])

    return _to_illusionary(root, nonsolid)


DISP_NO_HULL_COLL = 0x4         # dispinfo flags: players pass through the displacement


def _nonsolid_disps(root):
    """Displacements players walk through in Source 1 (No Hull Collision) become meshes without
    collision. Returns the number of brushes."""
    def nonsolid(sides):
        disps = [s.child("dispinfo") for s in sides if s.child("dispinfo") is not None]
        if not disps:
            return False
        try:
            return all(int(d.get("flags", "0") or 0) & DISP_NO_HULL_COLL for d in disps)
        except ValueError:
            return False

    return _to_illusionary(root, nonsolid)


def _to_illusionary(root, test):
    """World / func_detail brushes for which test(sides) is true are moved into one new
    func_illusionary (later a mesh without collision, see _brushes_to_meshes)."""
    moved = []
    holders = [root.child("world")] + [e for e in root.children("entity") if e.classname == "func_detail"]
    for holder in holders:
        if holder is None:
            continue
        keep = []
        for item in holder.items:
            if isinstance(item, VNode) and item.name.lower() == "solid":
                sides = item.children("side")
                if sides and test(sides):
                    moved.append(item)
                    continue
            keep.append(item)
        holder.items = keep
    if moved:
        max_id = max([_max_solid_id(root)] + [int(e.get("id", "0")) for e in root.children("entity")
                                               if (e.get("id") or "").isdigit()])
        ent = VNode("entity")
        ent.items = [["id", str(max_id + 1)], ["classname", "func_illusionary"]] + moved
        insert_at = next((n for n, i in enumerate(root.items) if isinstance(i, VNode)
                          and i.name.lower() in ("cameras", "cordon")), len(root.items))
        root.items.insert(insert_at, ent)
    return len(moved)


def _box_solid(lo, hi, material, next_id):
    """Axis aligned box brush lo..hi (planes wound like Hammer writes them). next_id() gives ids."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    faces = (
        (((x0, y1, z1), (x1, y1, z1), (x1, y0, z1)), "[1 0 0 0] 0.25", "[0 -1 0 0] 0.25"),
        (((x0, y0, z0), (x1, y0, z0), (x1, y1, z0)), "[1 0 0 0] 0.25", "[0 -1 0 0] 0.25"),
        (((x0, y1, z1), (x0, y0, z1), (x0, y0, z0)), "[0 1 0 0] 0.25", "[0 0 -1 0] 0.25"),
        (((x1, y1, z0), (x1, y0, z0), (x1, y0, z1)), "[0 1 0 0] 0.25", "[0 0 -1 0] 0.25"),
        (((x1, y1, z1), (x0, y1, z1), (x0, y1, z0)), "[1 0 0 0] 0.25", "[0 0 -1 0] 0.25"),
        (((x1, y0, z0), (x0, y0, z0), (x0, y0, z1)), "[1 0 0 0] 0.25", "[0 0 -1 0] 0.25"))
    solid = VNode("solid")
    solid.items.append(["id", next_id()])
    for pts, u, v in faces:
        side = VNode("side")
        side.items = [["id", next_id()],
                      ["plane", " ".join("(" + " ".join(_fmt(c) for c in p) + ")" for p in pts)],
                      ["material", material], ["uaxis", u], ["vaxis", v], ["rotation", "0"],
                      ["lightmapscale", "16"], ["smoothing_groups", "0"]]
        solid.items.append(side)
    return solid


SHELL_GAP = 4.0         # space between a light room and its sky shell
SHELL_THICK = 8.0


def add_room_shells(root, rooms, half, material):
    """Sky brushes around every light room (probes.plan()): a room players can see from far
    away looks like the sky. half: half the room's size. Returns the number of rooms."""
    world = root.child("world")
    if world is None or not rooms:
        return 0
    max_id = _max_solid_id(root)

    def next_id():
        nonlocal max_id
        max_id += 1
        return str(max_id)

    a, b = half + SHELL_GAP, half + SHELL_GAP + SHELL_THICK
    n = 0
    for _lo, _hi, c in rooms:
        if not c:
            continue
        n += 1
        for k in range(3):
            for sign in (-1, 1):
                lo = [c[j] - b for j in range(3)]
                hi = [c[j] + b for j in range(3)]
                # one slab per side, cut so that the six do not overlap
                for j in range(k):
                    lo[j], hi[j] = c[j] - a, c[j] + a
                if sign < 0:
                    hi[k] = c[k] - a
                else:
                    lo[k] = c[k] + a
                world.items.append(_box_solid(lo, hi, material, next_id))
    return n


ZONE_TRIGGERS = {"mod_zone_start": "map_start", "mod_zone_end": "map_end",
                 "mod_zone_start_bonus": "bonus1_start", "mod_zone_end_bonus": "bonus1_end"}


def _zone_kind(ent):
    """The mod_zone_* name of a timer zone entity (a trigger named like one counts too)."""
    if ent.classname.startswith("trigger_"):
        name = (ent.get("targetname") or "").strip().lower()
        return name if name in ZONE_TRIGGERS else None
    return ent.classname if ent.classname in ZONE_TRIGGERS and ent.children("solid") else None


def _zones_to_triggers(root):
    """Timer zones of Source 1 bhop maps become the trigger_multiple names CS2 timers look for.
    A map with more than one bonus start or end zone keeps all its bonus zones as they are:
    there is no telling which bonus each one belongs to."""
    kinds = [(ent, _zone_kind(ent)) for ent in root.children("entity")]
    keep_bonus = any(sum(1 for _e, k in kinds if k == bonus) > 1
                     for bonus in ("mod_zone_start_bonus", "mod_zone_end_bonus"))
    n = 0
    for ent, kind in kinds:
        if kind is None or keep_bonus and kind.endswith("_bonus"):
            continue
        name = ZONE_TRIGGERS[kind]
        # a trigger named like a zone only gets the new name
        if ent.classname.startswith("trigger_"):
            ent.set("targetname", name)
            n += 1
            continue
        ent.items = [i for i in ent.items if not isinstance(i, list) or i[0].lower() in ("id", "origin")]
        ent.items[1:1] = [["classname", "trigger_multiple"], ["targetname", name], ["spawnflags", "1"],
                          ["StartDisabled", "0"], ["wait", "0"]]
        n += 1
    return n


SPAWNS_WANTED = 20


def _fill_spawns(root):
    """Both teams get at least SPAWNS_WANTED spawn points, the added ones all on one existing
    spawn point (a terrorist one first). Returns the number added."""
    ents = root.children("entity")
    by_class = {c: [e for e in ents if e.classname == c]
                for c in ("info_player_terrorist", "info_player_counterterrorist", "info_player_start")}
    ref = next((lst[0] for lst in by_class.values() if lst and lst[0].get("origin")), None)
    if ref is None:
        return 0
    max_id = max([0] + [int(e.get("id", "0")) for e in ents if (e.get("id") or "").isdigit()])
    insert_at = next((n for n, i in enumerate(root.items) if isinstance(i, VNode)
                      and i.name.lower() in ("cameras", "cordon")), len(root.items))
    added = 0
    for cls in ("info_player_terrorist", "info_player_counterterrorist"):
        for _ in range(SPAWNS_WANTED - len(by_class[cls])):
            max_id += 1
            e = VNode("entity")
            e.items = [["id", str(max_id)], ["classname", cls], ["angles", ref.get("angles") or "0 0 0"],
                       ["origin", ref.get("origin")]]
            root.items.insert(insert_at, e)
            insert_at += 1
            added += 1
    return added


def _world_triggers_to_clip(root):
    """toolstrigger faces of brushes that are not part of a trigger entity (world, func_detail,
    which is world geometry in Source 1, and solid brush entities) become toolsplayerclip.
    Returns the number of faces."""
    holders = [root.child("world")] + [
        e for e in root.children("entity") if e.classname in _SOLID_BRUSH_CLASSES
        and not (e.classname == "func_brush" and (e.get("Solidity") or "0").strip() == "1")]
    count = 0
    for holder in holders:
        if holder is None:
            continue
        solids = holder.children("solid") + [s for h in holder.children("hidden") for s in h.children("solid")]
        for solid in solids:
            for side in solid.children("side"):
                if normalize_material(side.get("material", "")) == TRIGGER_MAT:
                    side.set("material", PLAYERCLIP_MAT)
                    count += 1
    return count


def map_bounds(root):
    """(mins, maxs) of the brushes the ported map keeps (world and brush entities, displaced
    surfaces included; not the brushes of entities that become particles), None without
    brushes."""
    from .blend import displacement_triangles
    lo, hi = [math.inf] * 3, [-math.inf] * 3

    def add(p):
        for k in range(3):
            lo[k], hi[k] = min(lo[k], p[k]), max(hi[k], p[k])

    holders = [root.child("world")] + [e for e in root.children("entity") if e.classname not in EFFECT_CLASSES]
    for holder in holders:
        if holder is None:
            continue
        for solid in holder.children("solid") + [s for h in holder.children("hidden") for s in h.children("solid")]:
            sides = solid.children("side")
            if any(s.child("dispinfo") is not None for s in sides):
                for tri in displacement_triangles(solid):
                    for p in tri:
                        add(p)
                continue
            for side in sides:
                for m in re.findall(r"\(([^)]*)\)", side.get("plane", "")):
                    p = _vec(m)
                    if p:
                        add(p)
    if lo[0] == math.inf:
        return None
    return tuple(lo), tuple(hi)


BOOSTER_FILTER = "boosterfixisgay"


def _remove_booster_filter(root):
    """Drops the filter of entities that use the booster filter, or a filter that is not in the
    map (CS2 would then let nothing through)."""
    ents = root.children("entity")
    filters = {(e.get("targetname") or "").strip().lower() for e in ents if e.classname.startswith("filter_")}
    count = 0
    for ent in ents:
        if ent.classname.startswith("filter_"):
            continue            # filter_activator_name keeps the name it checks in this key
        name = (ent.get("filtername") or "").strip().lower()
        if name and (name == BOOSTER_FILTER or name not in filters):
            ent.remove_key("filtername")
            count += 1
    return count


# filter class -> (key holding the value it checks, AddOutput key that changes that value)
_ATTRIBUTE_FILTERS = {"filter_activator_name": ("filtername", "targetname"),
                      "filter_activator_class": ("filterclass", "classname")}


def _name_filters_to_attribute(root, attributes):
    """filter_activator_name / filter_activator_class -> filter_activator_attribute_int; the
    name (or class) it checked becomes the attribute name. attributes receives the names by
    the AddOutput key that set them in Source 1: {'targetname': [...], 'classname': [...]}."""
    count = 0
    for ent in root.children("entity"):
        conv = _ATTRIBUTE_FILTERS.get(ent.classname)
        if not conv:
            continue
        key, output_key = conv
        # "state0*" matched every name starting with state0 in Source 1; the attribute keeps the
        # name, and vmap.rewrite_connections also adds it to every name it matched
        name = (ent.get(key) or "").strip()
        idx = ent.index_of_key("classname")
        ent.items[idx] = ["classname", "filter_activator_attribute_int"]
        ent.remove_key(key)
        ent.items.insert(idx + 1, ["filterattribute", name])
        lst = attributes.setdefault(output_key, [])
        if name and name not in lst:
            lst.append(name)
        count += 1
    return count


# --- big flat triggers -----------------------------------------------------------------

# CS2 misses touches on trigger boxes that are very wide for their height. A box at most
# SPLIT_THIN_HEIGHT high is split into pieces of at most SPLIT_RATIO x its height in X and Y
# (never smaller than SPLIT_MIN_PIECE or larger than SPLIT_MAX_PIECE): 2 or 4 high -> 512,
# 8 high -> 1024. Higher boxes (up to SPLIT_MAX_HEIGHT) mostly work; only very long ones are
# split, into pieces of at most SPLIT_LARGE_PIECE (20000 x 4700 x 32 -> 2 pieces).
SPLIT_MAX_HEIGHT = 64
SPLIT_THIN_HEIGHT = 8
SPLIT_RATIO = 128
SPLIT_MIN_PIECE = 512
SPLIT_MAX_PIECE = 1024
SPLIT_LARGE_PIECE = 10000

# boost trigger_multiples (basevelocity outputs) at most BOOST_MAX_RAISE high get
# BOOST_HEIGHT; only the top face moves up
BOOST_MAX_RAISE = 4
BOOST_HEIGHT = 6


def split_piece_size(height):
    if height > SPLIT_THIN_HEIGHT:
        return SPLIT_LARGE_PIECE
    return max(SPLIT_MIN_PIECE, min(SPLIT_MAX_PIECE, height * SPLIT_RATIO))


def _box_bounds(solid):
    """(mins, maxs) when every side of the brush is axis aligned (a box), else None."""
    pts = []
    for side in solid.children("side"):
        p = [tuple(_float(v) for v in m.split()) for m in re.findall(r"\(([^)]*)\)", side.get("plane", ""))]
        if len(p) != 3 or any(len(x) != 3 or None in x for x in p):
            return None
        # axis aligned: two of the coordinates change, one is the same on all three points
        if sum(1 for k in range(3) if abs(p[0][k] - p[1][k]) < 1e-3 and abs(p[0][k] - p[2][k]) < 1e-3) != 1:
            return None
        pts += p
    if len(pts) != 18:
        return None
    mins = tuple(min(p[k] for p in pts) for k in range(3))
    maxs = tuple(max(p[k] for p in pts) for k in range(3))
    return mins, maxs


def surface_rects(root, mats):
    """Faces of box brushes that have one of these materials (texture lights the material
    can not glow with in CS2, like water) -> [(material, lo, hi, axis, sign)]: the face as a
    flat box (lo[axis] == hi[axis]) and the direction it faces (+1 / -1 along axis)."""
    out = []
    if not mats:
        return out
    for node in _walk(root):
        if node.name.lower() != "solid":
            continue
        sides = node.children("side")
        hits = [(s, normalize_material(s.get("material", ""))) for s in sides]
        hits = [(s, m) for s, m in hits if m in mats]
        if not hits:
            continue
        box = _box_bounds(node)
        if box is None:
            continue
        lo, hi = box
        for side, mat in hits:
            p = [tuple(_float(v) for v in m.split()) for m in re.findall(r"\(([^)]*)\)", side.get("plane", ""))]
            axis = next((k for k in range(3) if abs(p[0][k] - p[1][k]) < 1e-3 and abs(p[0][k] - p[2][k]) < 1e-3), None)
            if axis is None:
                continue
            c = p[0][axis]
            sign = 1 if abs(c - hi[axis]) < 0.5 else -1 if abs(c - lo[axis]) < 0.5 else 0
            if not sign:
                continue
            flo, fhi = list(lo), list(hi)
            flo[axis] = fhi[axis] = c
            out.append((mat, tuple(flo), tuple(fhi), axis, sign))
    return out


def _max_solid_id(root):
    max_id = 0
    for node in _walk(root):
        if node.name.lower() in ("solid", "side"):
            try:
                max_id = max(max_id, int(node.get("id", "0")))
            except ValueError:
                pass
    return max_id


def _raise_boost_triggers(root):
    """Boost trigger_multiples (an AddOutput basevelocity output) that are at most
    BOOST_MAX_RAISE high get BOOST_HEIGHT; the bottom stays where it is."""
    count = 0
    for ent in root.children("entity"):
        if ent.classname != "trigger_multiple":
            continue
        if not any(o[2].strip().lower() == "addoutput" and "basevelocity" in o[3].lower()
                   for o in _outputs(ent) if len(o) >= 4):
            continue
        for k, item in enumerate(ent.items):
            if not (isinstance(item, VNode) and item.name.lower() == "solid"):
                continue
            b = _box_bounds(item)
            if b is None:
                continue
            mins, maxs = b
            size = [maxs[i] - mins[i] for i in range(3)]
            if not 0 < size[2] <= BOOST_MAX_RAISE:
                continue
            hi = (maxs[0], maxs[1], mins[2] + BOOST_HEIGHT)
            ent.items[k] = _box_piece(item, mins, size, mins, hi, None)[1]
            count += 1
    return count


def _split_triggers(root):
    """Trigger boxes that are very wide for their height are not detected by CS2. They are split
    into pieces (see split_piece_size) inside the same entity, so the outputs stay the same."""
    max_id = _max_solid_id(root)
    count = 0
    for ent in root.children("entity"):
        if not ent.classname.startswith("trigger_"):
            continue
        new_items = []
        for item in ent.items:
            if not (isinstance(item, VNode) and item.name.lower() == "solid"):
                new_items.append(item)
                continue
            b = _box_bounds(item)
            if b is None:
                new_items.append(item)
                continue
            mins, maxs = b
            size = [maxs[k] - mins[k] for k in range(3)]
            piece = split_piece_size(size[2])
            if size[2] > SPLIT_MAX_HEIGHT or size[2] <= 0 or max(size[0], size[1]) <= piece:
                new_items.append(item)
                continue
            nx = max(1, math.ceil(size[0] / piece))
            ny = max(1, math.ceil(size[1] / piece))
            for ix in range(nx):
                for iy in range(ny):
                    lo = (mins[0] + size[0] * ix / nx, mins[1] + size[1] * iy / ny, mins[2])
                    hi = (mins[0] + size[0] * (ix + 1) / nx, mins[1] + size[1] * (iy + 1) / ny, maxs[2])
                    max_id, piece = _box_piece(item, mins, size, lo, hi, max_id)
                    new_items.append(piece)
            count += 1
        ent.items = new_items
    return count


# --- thin triggers that are not boxes ----------------------------------------------------

# They are cut into pieces along X and Y like boxes are (see split_piece_size). CS2 misses some
# of these, so a piece at most SPLIT_THIN_HEIGHT high also grows down towards the floor under it,
# only where nobody could be without touching it already:
#   - on the floor (at most FLOOR_GAP above it): FLOOR_DEPTH units into the solid floor
#   - at most CROUCH_HEIGHT above the floor (a player under it touches it anyway): down to the floor
#   - higher: FLOOR_REACH units down, keeping PLAYER_HEIGHT + JUMP_HEIGHT free above the floor
FLOOR_DEPTH = 16
FLOOR_GAP = 2
FLOOR_REACH = 64
CROUCH_HEIGHT = 54
PLAYER_HEIGHT = 72
JUMP_HEIGHT = 64
FLOOR_SAMPLE = 64           # spacing of the points the floor is looked for at

# texture axes of a new side by the axis its normal is closest to
_AXIS_UV = {0: ("[0 1 0 0] 0.25", "[0 0 -1 0] 0.25"), 1: ("[1 0 0 0] 0.25", "[0 0 -1 0] 0.25"),
            2: ("[1 0 0 0] 0.25", "[0 -1 0 0] 0.25")}


def _poly_area(poly):
    from .blend import _cross, _sub
    sx = sy = sz = 0.0
    for i in range(1, len(poly) - 1):
        c = _cross(_sub(poly[i], poly[0]), _sub(poly[i + 1], poly[0]))
        sx, sy, sz = sx + c[0], sy + c[1], sz + c[2]
    return 0.5 * math.sqrt(sx * sx + sy * sy + sz * sz)


def _brush_faces(planes, src):
    """Faces of the convex brush [(normal, dist)] (outward): ([(normal, dist)], [source], [polygon])
    without the planes that are the same as an earlier one or have no area."""
    from .blend import _polygon
    uniq, usrc = [], []
    for pl, s in zip(planes, src):
        if any(abs(pl[1] - q[1]) < 0.01 and sum(a * b for a, b in zip(pl[0], q[0])) > 0.99999 for q in uniq):
            continue
        uniq.append(pl)
        usrc.append(s)
    out_p, out_s, out_f = [], [], []
    for k in range(len(uniq)):
        poly = _polygon(uniq, k)
        if len(poly) >= 3 and _poly_area(poly) > 0.05:
            out_p.append(uniq[k])
            out_s.append(usrc[k])
            out_f.append(poly)
    return out_p, out_s, out_f


def _hull_2d(pts):
    pts = sorted(set((round(p[0], 3), round(p[1], 3)) for p in pts))
    if len(pts) < 3:
        return pts

    def half(seq):
        h = []
        for p in seq:
            while len(h) >= 2 and ((h[-1][0] - h[-2][0]) * (p[1] - h[-2][1])
                                   - (h[-1][1] - h[-2][1]) * (p[0] - h[-2][0])) <= 0:
                h.pop()
            h.append(p)
        return h
    lower, upper = half(pts), half(reversed(pts))
    return lower[:-1] + upper[:-1]          # counter clockwise


def _inside_hull(hull, x, y):
    return all((b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0]) >= 0
               for a, b in zip(hull, hull[1:] + hull[:1]))


def _floor_drop(hull, lo, hi, world):
    """How far the brush grows down (see FLOOR_REACH), 0 for not at all."""
    cx = sum(p[0] for p in hull) / len(hull)
    cy = sum(p[1] for p in hull) / len(hull)
    pts = [(cx, cy)] + [(p[0] + (cx - p[0]) * 0.02, p[1] + (cy - p[1]) * 0.02) for p in hull]
    nx = min(8, int((hi[0] - lo[0]) // FLOOR_SAMPLE))
    ny = min(8, int((hi[1] - lo[1]) // FLOOR_SAMPLE))
    for i in range(1, nx + 1):
        for j in range(1, ny + 1):
            x = lo[0] + (hi[0] - lo[0]) * i / (nx + 1)
            y = lo[1] + (hi[1] - lo[1]) * j / (ny + 1)
            if _inside_hull(hull, x, y):
                pts.append((x, y))
    far = PLAYER_HEIGHT + JUMP_HEIGHT + FLOOR_REACH
    hits = world.trace_all((pts[0][0], pts[0][1], lo[2] + 0.05), [(0.0, 0.0, -1.0)], far)
    gap = far if hits[0] is None else hits[0] - 0.05
    for x, y in pts[1:]:
        h = world.trace_all((x, y, lo[2] + 0.05), [(0.0, 0.0, -1.0)], far)[0]
        if h is not None:
            gap = min(gap, h - 0.05)
    if gap <= FLOOR_GAP:
        ok = world.box_in((lo[0] + 1, lo[1] + 1, lo[2] - FLOOR_DEPTH), (hi[0] - 1, hi[1] - 1, lo[2] - FLOOR_GAP),
                          void_only=False)
        return FLOOR_DEPTH if ok else 0
    if gap <= CROUCH_HEIGHT:
        return gap
    return max(0.0, min(FLOOR_REACH, gap - PLAYER_HEIGHT - JUMP_HEIGHT))


def _to_floor(planes, src, polys, world):
    """The brush grown down towards the floor (see FLOOR_REACH), or None: the faces that do not
    look down are kept, walls follow its outline down to a new bottom. world: the compiled map
    (lighting.BspTracer)."""
    pts = [p for poly in polys for p in poly]
    lo = [min(p[k] for p in pts) for k in range(3)]
    hi = [max(p[k] for p in pts) for k in range(3)]
    if not 0 < hi[2] - lo[2] <= SPLIT_THIN_HEIGHT:
        return None
    hull = _hull_2d(pts)
    if len(hull) < 3:
        return None
    drop = round(_floor_drop(hull, lo, hi, world))
    if drop < 1:
        return None
    bottom = lo[2] - drop
    new_p = [pl for pl in planes if pl[0][2] > -0.01]
    new_s = [s for pl, s in zip(planes, src) if pl[0][2] > -0.01]
    walls = [pl for pl in new_p if abs(pl[0][2]) < 1e-4]
    for i, a in enumerate(hull):
        b = hull[(i + 1) % len(hull)]
        n = (b[1] - a[1], a[0] - b[0], 0.0)
        ln = math.hypot(n[0], n[1])
        if ln < 1e-6:
            continue
        # the outline already has a wall here
        if any(all(abs(w[0][0] * q[0] + w[0][1] * q[1] - w[1]) < 0.5 for q in (a, b)) for w in walls):
            continue
        n = (n[0] / ln, n[1] / ln, 0.0)
        new_p.append((n, n[0] * a[0] + n[1] * a[1]))
        new_s.append(None)
    new_p.append(((0.0, 0.0, -1.0), -bottom))
    new_s.append(None)
    out = _brush_faces(new_p, new_s)
    return out if len(out[0]) >= 4 else None


def _copy_node(node):
    out = VNode(node.name)
    out.items = [_copy_node(i) if isinstance(i, VNode) else list(i) for i in node.items]
    return out


def _solid_from(solid, planes, src, polys, max_id):
    """New brush entity solid with the faces (_brush_faces); src: the side each face copies, None
    for a new side (it gets the brush's most used material). Returns (max_id, solid)."""
    mats = collections.Counter(s.get("material", "") for s in solid.children("side"))
    template = next((s for s in solid.children("side") if s.get("material", "") == mats.most_common(1)[0][0]), None)
    piece = VNode(solid.name)
    max_id += 1
    piece.items.append(["id", str(max_id)])
    for pl, s, poly in zip(planes, src, polys):
        side = _copy_node(s if s is not None else template)
        side.items = [i for i in side.items if not (isinstance(i, VNode) and i.name.lower() in ("vertices_plus", "dispinfo"))]
        max_id += 1
        side.set("id", str(max_id))
        if s is None:
            # three corners, wound so the normal points out of the brush
            a, b, c = poly[0], poly[len(poly) // 3], poly[(2 * len(poly)) // 3]
            if b == a or c == b:
                b, c = poly[1], poly[2]
            side.set("plane", " ".join("(" + " ".join(_fmt(v) for v in p) + ")" for p in (a, c, b)))
            u, v = _AXIS_UV[max(range(3), key=lambda k: abs(pl[0][k]))]
            side.set("uaxis", u)
            side.set("vaxis", v)
        piece.items.append(side)
    for item in solid.items:
        if isinstance(item, VNode) and item.name.lower() == "editor":
            piece.items.append(_copy_node(item))
    return max_id, piece


def _solid_ok(solid):
    """Every side of the brush is a face, read the way the editor reads the planes."""
    from .blend import _cross, _dot, _norm, _plane_points, _sub
    sides = solid.children("side")
    planes = []
    for side in sides:
        pts = _plane_points(side.get("plane"))
        if pts is None:
            return False
        n = _norm(_cross(_sub(pts[2], pts[0]), _sub(pts[1], pts[0])))
        planes.append((n, _dot(n, pts[0])))
    return len(_brush_faces(planes, sides)[0]) == len(sides) >= 4


def _fix_odd_triggers(root, world=None):
    """Thin trigger brushes that are not boxes: wide ones are cut into pieces, thin pieces grow
    down towards the floor (see FLOOR_REACH). world: the compiled map (lighting.BspTracer; None:
    nothing grows). Returns (split, grown)."""
    from .blend import _outward_planes
    max_id = _max_solid_id(root)
    split = floor = 0
    for ent in root.children("entity"):
        if not ent.classname.startswith("trigger_"):
            continue
        new_items = []
        for item in ent.items:
            if not (isinstance(item, VNode) and item.name.lower() == "solid") or _box_bounds(item) is not None:
                new_items.append(item)
                continue
            sides = item.children("side")
            planes = _outward_planes(sides) if not any(s.child("dispinfo") is not None for s in sides) else None
            faces = _brush_faces([tuple(p) for p in planes], sides) if planes else None
            if not faces or len(faces[0]) < 4:
                new_items.append(item)
                continue
            pts = [p for poly in faces[2] for p in poly]
            mins = [min(p[k] for p in pts) for k in range(3)]
            size = [max(p[k] for p in pts) - mins[k] for k in range(3)]
            if not 0 < size[2] <= SPLIT_MAX_HEIGHT:
                new_items.append(item)
                continue
            step = split_piece_size(size[2])
            nx = math.ceil(size[0] / step) if size[0] > step else 1
            ny = math.ceil(size[1] / step) if size[1] > step else 1
            pieces = []
            for ix in range(nx):
                for iy in range(ny):
                    cut_p, cut_s = list(faces[0]), list(faces[1])
                    if ix > 0:
                        cut_p.append(((-1.0, 0.0, 0.0), -(mins[0] + size[0] * ix / nx)))
                    if ix < nx - 1:
                        cut_p.append(((1.0, 0.0, 0.0), mins[0] + size[0] * (ix + 1) / nx))
                    if iy > 0:
                        cut_p.append(((0.0, -1.0, 0.0), -(mins[1] + size[1] * iy / ny)))
                    if iy < ny - 1:
                        cut_p.append(((0.0, 1.0, 0.0), mins[1] + size[1] * (iy + 1) / ny))
                    cut_s += [None] * (len(cut_p) - len(cut_s))
                    piece = _brush_faces(cut_p, cut_s) if len(cut_p) > len(faces[0]) else faces
                    if len(piece[0]) >= 4:
                        pieces.append(piece)
            if not pieces:
                new_items.append(item)
                continue
            lowered = False
            for k, piece in enumerate(pieces):
                low = _to_floor(*piece, world) if world is not None else None
                if low is not None:
                    pieces[k] = low
                    lowered = True
            if len(pieces) == 1 and not lowered:
                new_items.append(item)
                continue
            nodes, last = [], max_id
            for piece in pieces:
                last, node = _solid_from(item, *piece, last)
                nodes.append(node)
            if not all(_solid_ok(n) for n in nodes):
                new_items.append(item)          # a piece the editor would read differently
                continue
            max_id = last
            new_items.extend(nodes)
            split += len(pieces) > 1
            floor += lowered
        ent.items = new_items
    return split, floor


def _walk(node):
    for ch in node.children():
        yield ch
        yield from _walk(ch)


def _fmt(v):
    v = round(v, 3)
    return str(int(v)) if v == int(v) else f"{v:g}"


def _box_piece(solid, mins, size, lo, hi, max_id):
    """Copy of the box brush solid scaled to the box lo..hi (keeps every plane's winding).
    New ids above max_id; max_id None keeps the ids (the copy replaces the solid)."""
    def remap(p):
        return tuple(lo[k] + (p[k] - mins[k]) * (hi[k] - lo[k]) / size[k] if size[k] else p[k]
                     for k in range(3))

    def new_id(old):
        nonlocal max_id
        if max_id is None:
            return old
        max_id += 1
        return str(max_id)

    piece = VNode(solid.name)
    for item in solid.items:
        if isinstance(item, list):
            piece.items.append([item[0], new_id(item[1]) if item[0].lower() == "id" else item[1]])
            continue
        node = VNode(item.name)
        for sub in item.items:
            if isinstance(sub, VNode):
                if sub.name.lower() != "vertices_plus":     # stale corner list of the old size
                    node.items.append(sub)
            elif sub[0].lower() == "id":
                node.items.append(["id", new_id(sub[1])])
            elif sub[0].lower() == "plane":
                pts = [remap(tuple(float(v) for v in m.split())) for m in re.findall(r"\(([^)]*)\)", sub[1])]
                node.items.append(["plane", " ".join("(" + " ".join(_fmt(c) for c in p) + ")" for p in pts)])
            else:
                node.items.append(list(sub))
        piece.items.append(node)
    return max_id, piece


# Source 1 light class -> CS2 class made by the importer
_LIGHT_CLASSES = {"light": "light_omni2", "light_spot": "light_barn", "light_dynamic": "light_omni2",
                  "light_environment": "light_environment"}


def _light_value(text):
    """4th value of "r g b brightness" (None when missing)."""
    parts = (text or "").split()
    return _float(parts[3]) if len(parts) >= 4 else None


def _round2(v):
    return f"{round(v, 2):g}"


def _lumens_text(v):
    if v < 10:
        return f"{round(v, 1):g}"
    if v < 1000:
        return str(int(round(v)))
    return str(int(round(v, -1)))


def collect_lights(root, values=None):
    """[(cs2_class, (x, y, z), info)] for vmap.fix_lights.
    Point and spot lights: values from lighting.light_values (by position) give info["lumens"]
    and info["range"] (a spot light's lumens only fill its cone). Without values: info["lumens"]
    = 4th value of _light, info["range"] = _distance or _zero_percent_distance when the map sets
    one.
    light_environment: Source 1 stores the sun and the ambient light in 0-255 units that the
    importer only divides by 255 (200 -> 0.78, 20 -> 0.08), which is far too dark for the sky
    in CS2. Sun: brightness / 400 (200 -> 0.5), ambient: brightness / 40 (20 -> 0.5),
    which lands in the range used by hand made CS2 maps."""
    out = []
    for ent in root.children("entity"):
        cls = _LIGHT_CLASSES.get(ent.classname)
        if not cls:
            continue
        origin = (ent.get("origin") or "").split()
        if len(origin) != 3:
            continue
        try:
            pos = tuple(float(v) for v in origin)
        except ValueError:
            continue
        info = {}
        if cls == "light_environment":
            sun = _light_value(ent.get("_light"))
            amb = _light_value(ent.get("_ambient"))
            if sun is not None:
                info["brightness"] = _round2(min(max(sun, 0.0) / 400.0, 4.0))
            if amb is not None:
                info["skyintensity"] = _round2(min(max(amb / 40.0, 0.2), 2.0))
        elif values is not None:
            val = values.get(pos_key(pos))
            if val:
                info["lumens"] = _lumens_text(val["lumens"] * val.get("cone", 1.0))
                info["range"] = val["range"]
        else:
            parts = (ent.get("_light") or "").split()
            if len(parts) >= 4 and _float(parts[3]) is not None:
                info["lumens"] = parts[3]
            for key in ("_distance", "_zero_percent_distance"):
                d = _float(ent.get(key), 0.0)
                if d and d > 0:
                    info["range"] = d
                    break
        out.append((cls, pos, info))
    return out


def pos_key(values):
    """Rounded position used to find an entity again in the .vmap (same as vmap._pos_key)."""
    return tuple(round(float(v), 1) for v in values)


def _vec(text):
    parts = (text or "").split()
    if len(parts) != 3:
        return None
    try:
        return tuple(float(v) for v in parts)
    except ValueError:
        return None


# Source 1 rope "Type": number of simulated nodes (ROPE_MAX_SEGMENTS, ROPE_TYPE1/2_NUMSEGMENTS)
ROPE_TYPE_NODES = {"": 10, "0": 10, "1": 4, "2": 2}


def collect_ropes(root):
    """Rope chains (move_rope -> keyframe_rope -> ...) for vmap.convert_ropes, keyed by the
    position of the first point: {pos_key: {"points", "slack", "segments", "width", "material", "collide"}}.
    In Source 1 each segment is made by the entity it starts at, with that entity's settings."""
    by_name, starts = {}, []
    for ent in root.children("entity"):
        if ent.classname not in ("move_rope", "keyframe_rope"):
            continue
        pos = _vec(ent.get("origin"))
        if pos is None:
            continue
        name = (ent.get("targetname") or "").strip().lower()
        if name:
            by_name.setdefault(name, (ent, pos))
        if ent.classname == "move_rope":
            starts.append((ent, pos))
    out = {}
    for ent, pos in starts:
        chain, seen, cur = [(ent, pos)], {id(ent)}, ent
        while True:
            hit = by_name.get((cur.get("NextKey") or "").strip().lower())
            if hit is None or id(hit[0]) in seen:
                break
            chain.append(hit)
            seen.add(id(hit[0]))
            cur = hit[0]
        if len(chain) < 2:
            continue
        out[pos_key(pos)] = {
            "points": [p for _e, p in chain],
            "slack": [_float(e.get("Slack"), 25.0) for e, _p in chain[:-1]],
            "segments": [ROPE_TYPE_NODES.get((e.get("Type") or "0").strip(), 2) for e, _p in chain[:-1]],
            "width": [_float(e.get("Width"), 2.0) for e, _p in chain],
            "material": normalize_material(ent.get("RopeMaterial") or "cable/cable"),
            "collide": (ent.get("Collide") or "").strip() == "1",
        }
    return out


# --- env_steam / func_dustmotes / point_spotlight -> info_particle_system ---------------

STEAM_VPCF = "particles/steam_particle.vpcf"
DUSTMOTES_VPCF = "particles/dustmotes.vpcf"
LIGHT_RAY_VPCF = "particles/light_ray.vpcf"
TRAIL_VPCF = "particles/trail_particle.vpcf"
EFFECT_VPCFS = {"steam": STEAM_VPCF, "dustmotes": DUSTMOTES_VPCF, "spotlight": LIGHT_RAY_VPCF,
                "trail": TRAIL_VPCF}
# func_dustcloud and env_embers get a particle file made for their values (several entities with
# the same values share one), from assets/box_sprites_template.vpcf; func_smokevolume and
# env_smoketrail from assets/smoke_template.vpcf; a long func_dustmotes box a copy of
# dustmotes.vpcf drawn from farther away
GENERATED_VPCF_DIR = "particles/cs2porter"
BOX_DRAW_DISTANCE = 5000
EFFECT_CLASSES = ("env_steam", "func_dustmotes", "point_spotlight", "env_spritetrail", "func_dustcloud",
                  "env_embers", "func_smokevolume", "env_smoketrail")
# inputs of the old entities -> info_particle_system inputs (the others are reported)
EFFECT_INPUTS = {
    "steam": {"turnon": "Start", "turnoff": "Stop"},
    "dustmotes": {"turnon": "Start", "turnoff": "Stop"},
    "dustcloud": {"turnon": "Start", "turnoff": "Stop"},
    "spotlight": {"lighton": "Start", "lightoff": "DestroyImmediately"},
}
_EFFECT_KEEP = ("id", "classname", "targetname", "parentname", "origin", "angles")
# a heat wave env_steam only bends the view behind it: it becomes a faint haze
HEATWAVE_ALPHA = 0.15


def _forward(angles):
    p, y = (math.radians(a) for a in (angles or (0.0, 0.0, 0.0))[:2])
    return (math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), -math.sin(p))


def _solid_bounds(ent):
    pts = []
    for solid in ent.children("solid"):
        for side in solid.children("side"):
            pts += [v for v in (_vec(m) for m in re.findall(r"\(([^)]*)\)", side.get("plane", ""))) if v]
    if not pts:
        return None
    return tuple(min(p[k] for p in pts) for k in range(3)), tuple(max(p[k] for p in pts) for k in range(3))


# tool brushes that do not stop a Source 1 spotlight trace (MASK_SOLID_BRUSHONLY)
_TRACE_SKIP_TOOLS = ("toolsplayerclip", "toolsnpcclip", "toolsclip", "toolstrigger", "toolshint", "toolsskip",
                     "toolsareaportal", "toolsoccluder", "toolsfog", "toolsinvisibleladder", "toolsblocklight",
                     "toolsblock_los", "toolsblockbullets")


class BrushTracer:
    """Line traces against the world and func_detail brushes of a VMF."""

    def __init__(self, root):
        from .blend import displacement_triangles
        self.solids = []                     # (mins, maxs, solid, plane points)
        self.disps = []                      # (mins, maxs, triangles): only the displaced surface is solid
        holders = [root.child("world")] + [e for e in root.children("entity") if e.classname == "func_detail"]
        for holder in holders:
            if holder is None:
                continue
            for solid in holder.children("solid"):
                if any(s.child("dispinfo") is not None for s in solid.children("side")):
                    tris = displacement_triangles(solid)
                    if tris:
                        pts = [p for tri in tris for p in tri]
                        self.disps.append((tuple(min(p[k] for p in pts) for k in range(3)),
                                           tuple(max(p[k] for p in pts) for k in range(3)), tris))
                    continue
                mats = [(s.get("material") or "").lower() for s in solid.children("side")]
                if not mats or all(any(m.endswith(t) for t in _TRACE_SKIP_TOOLS) for m in mats):
                    continue
                pts = [v for s in solid.children("side")
                       for v in (_vec(m) for m in re.findall(r"\(([^)]*)\)", s.get("plane", ""))) if v]
                if pts:
                    self.solids.append((tuple(min(p[k] for p in pts) for k in range(3)),
                                        tuple(max(p[k] for p in pts) for k in range(3)), solid, pts))
        self._planes = {}

    def _brush_planes(self, solid, pts):
        planes = self._planes.get(id(solid))
        if planes is None:
            cx, cy, cz = (sum(p[k] for p in pts) / len(pts) for k in range(3))
            planes = []
            for side in solid.children("side"):
                v = [p for p in (_vec(m) for m in re.findall(r"\(([^)]*)\)", side.get("plane", ""))) if p]
                if len(v) < 3:
                    continue
                a = [v[1][k] - v[0][k] for k in range(3)]
                b = [v[2][k] - v[0][k] for k in range(3)]
                n = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
                ln = math.sqrt(sum(x * x for x in n))
                if ln < 1e-9:
                    continue
                n = tuple(x / ln for x in n)
                d = sum(n[k] * v[0][k] for k in range(3))
                if n[0] * cx + n[1] * cy + n[2] * cz > d:       # normals point out of the brush
                    n, d = tuple(-x for x in n), -d
                planes.append((n, d))
            self._planes[id(solid)] = planes
        return planes

    def trace(self, start, direction, length):
        """Distance to the first brush along the line (length when nothing is hit). Brushes that
        hold the start point are ignored."""
        end = tuple(start[k] + direction[k] * length for k in range(3))
        lo = tuple(min(start[k], end[k]) for k in range(3))
        hi = tuple(max(start[k], end[k]) for k in range(3))
        best = length
        for mins, maxs, solid, pts in self.solids:
            if any(maxs[k] < lo[k] - 1 or mins[k] > hi[k] + 1 for k in range(3)):
                continue
            t0, t1, inside = 0.0, best, True
            for n, d in self._brush_planes(solid, pts):
                dist = n[0] * start[0] + n[1] * start[1] + n[2] * start[2] - d
                den = n[0] * direction[0] + n[1] * direction[1] + n[2] * direction[2]
                if dist > 0.01:
                    inside = False
                if abs(den) < 1e-9:
                    if dist > 0:
                        t0, t1 = 1.0, 0.0
                        break
                    continue
                t = -dist / den
                if den < 0:
                    t0 = max(t0, t)
                else:
                    t1 = min(t1, t)
                if t0 > t1:
                    break
            if not inside and t0 <= t1 and t0 < best:
                best = t0
        for mins, maxs, tris in self.disps:
            if any(maxs[k] < lo[k] - 1 or mins[k] > hi[k] + 1 for k in range(3)):
                continue
            for a, b, c in tris:
                hit = _ray_triangle(start, direction, a, b, c)
                if hit is not None and 0.5 < hit < best:
                    best = hit
        return best


def _ray_triangle(o, d, a, b, c):
    """Distance along the ray to the triangle (both sides), or None."""
    e1 = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    e2 = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    p = (d[1] * e2[2] - d[2] * e2[1], d[2] * e2[0] - d[0] * e2[2], d[0] * e2[1] - d[1] * e2[0])
    det = e1[0] * p[0] + e1[1] * p[1] + e1[2] * p[2]
    if abs(det) < 1e-9:
        return None
    inv = 1.0 / det
    s = (o[0] - a[0], o[1] - a[1], o[2] - a[2])
    u = (s[0] * p[0] + s[1] * p[1] + s[2] * p[2]) * inv
    if u < 0.0 or u > 1.0:
        return None
    q = (s[1] * e1[2] - s[2] * e1[1], s[2] * e1[0] - s[0] * e1[2], s[0] * e1[1] - s[1] * e1[0])
    v = (d[0] * q[0] + d[1] * q[1] + d[2] * q[2]) * inv
    if v < 0.0 or u + v > 1.0:
        return None
    return (e2[0] * q[0] + e2[1] * q[1] + e2[2] * q[2]) * inv


LIGHT_RAY_SCALE = 0.78
LIGHT_RAY_FULL = 400.0          # Source 1 light brightness that gets the whole beam
LIGHT_RAY_MIN = 0.35
LIGHT_RAY_NEAR = 64.0           # a light this close belongs to the spotlight


def _light_positions(root):
    out = []
    for e in root.children("entity"):
        if e.classname in ("light_spot", "light", "light_dynamic"):
            pos = _vec(e.get("origin"))
            vals = (e.get("_light") or "").split()
            if pos is not None and len(vals) >= 4:
                try:
                    out.append((pos, float(vals[3])))
                except ValueError:
                    pass
    return out


def _beam_brightness(ent, origin, lights):
    """0.35..1: the brightness of the light next to the spotlight (else of the beam itself)."""
    near = None
    if origin is not None and lights:
        dist, bright = min(((math.dist(p, origin), b) for p, b in lights), key=lambda x: x[0])
        if dist <= LIGHT_RAY_NEAR:
            near = bright
    if near is not None:
        f = math.sqrt(max(near, 0.0) / LIGHT_RAY_FULL)
    else:
        f = math.sqrt(min(max(_float(ent.get("renderamt"), 255.0), 0.0), 255.0) / 255.0)
    return min(max(f, LIGHT_RAY_MIN), 1.0)


def _spawnflags(ent):
    try:
        return int(float(ent.get("spawnflags") or 0))
    except ValueError:
        return 0


ANGLE_BRUSH_CLASSES = ("env_embers",)


def restore_brush_angles(root, bsp_entities):
    """The decompiled VMF has no 'angles' on brush entities, but env_embers fly along them: they
    are taken from the .bsp entity lump (the VMF id is the hammerid there)."""
    by_id = {e.get("hammerid"): e for e in bsp_entities
             if e.get("classname", "").lower() in ANGLE_BRUSH_CLASSES and e.get("angles")}
    n = 0
    for ent in root.children("entity"):
        if ent.classname in ANGLE_BRUSH_CLASSES and not ent.get("angles"):
            src = by_id.get((ent.get("id") or "").strip())
            if src:
                ent.set("angles", src["angles"])
                n += 1
    return n


def _embers_params(num):
    """env_embers (Source 1 C_Embers): 'density' sparks a second, each living 'lifetime' seconds,
    flying along the entity's angles at 'speed' (+-speed/8 on every axis), 1 unit big, shrinking
    and fading out. The particle's control point 0 has the entity's angles, so the velocity is
    given along its x axis."""
    speed = max(num("speed", 32.0), 0.0)
    side = speed / 8.0
    life = max(num("lifetime", 4.0), 0.1)
    return {"rate": max(num("density", 50.0), 1.0), "life_min": life, "life_max": life,
            "radius_min": 2.0, "radius_max": 3.0, "alpha": 1.0, "end_scale": 0.0,
            "fade_in": 0.05, "fade_out": 1.0,
            "vel_min": (speed - side, -side, -side), "vel_max": (speed + side, side, side)}


def _dustcloud_params(ent, num):
    """func_dustcloud (Source 1 C_Func_Dust): SpawnRate a second, LifetimeMin..Max seconds,
    SizeMin..Max, Alpha, drifting at up to SpeedMax (Frozen: not at all), fading in and out."""
    speed = 0.0 if (ent.get("Frozen") or "0").strip() == "1" else max(num("SpeedMax", 13.0), 0.0)
    lo, hi = sorted((max(num("LifetimeMin", 3.0), 0.1), max(num("LifetimeMax", 5.0), 0.1)))
    smin, smax = sorted((max(num("SizeMin", 100.0), 0.5), max(num("SizeMax", 200.0), 0.5)))
    # Source draws a hard dot (particle/sparkles) over the whole sprite; the soft glow used here
    # only fills its middle, so small motes are made twice as big and brighter, or they cannot
    # be seen at all; big soft clouds keep their values
    small = smax < 16.0
    alpha = num("Alpha", 30.0) / 255.0
    return {"rate": max(num("SpawnRate", 40.0), 1.0), "life_min": lo, "life_max": hi,
            "radius_min": smin * (2.0 if small else 1.0), "radius_max": smax * (2.0 if small else 1.0),
            "alpha": min(max(alpha * 4.0, 0.4) if small else max(alpha, 0.01), 1.0), "end_scale": 1.0,
            "fade_in": 0.3, "fade_out": 0.4,
            "vel_min": (-speed, -speed, -speed), "vel_max": (speed, speed, speed)}


def _rgb(text, default):
    vals = (_vec(text) or ())[:3]
    return tuple(min(max(round(v), 0), 255) for v in vals) if len(vals) == 3 else default


def _smoketrail_params(ent, num):
    """env_smoketrail (Source 1 C_SmokeTrail): 'spawnrate' puffs a second within 'spawnradius',
    each living 'lifetime' seconds, moving at minspeed..maxspeed in a random direction plus
    mindirectedspeed..maxdirectedspeed along the entity's angles, growing from startsize to
    endsize and turning from startcolor to endcolor while 'opacity' fades."""
    start = max(num("startsize", 15.0), 1.0)
    lo, hi = sorted((max(num("minspeed", 10.0), 0.0), max(num("maxspeed", 20.0), 0.0)))
    dlo, dhi = sorted((num("mindirectedspeed", 0.0), num("maxdirectedspeed", 0.0)))
    life = max(num("lifetime", 5.0), 0.1)
    return {"_template": "smoke", "rate": max(num("spawnrate", 20.0), 0.1),
            "life_min": life, "life_max": life, "radius_min": start, "radius_max": start,
            "end_scale": max(num("endsize", 50.0), 0.0) / start,
            "alpha": min(max(num("opacity", 0.75), 0.0), 1.0),
            "color_min": _rgb(ent.get("startcolor"), (192, 192, 192)),
            "color_max": _rgb(ent.get("startcolor"), (192, 192, 192)),
            "color_end": _rgb(ent.get("endcolor"), (160, 160, 160)),
            "spin": 20.0, "speed_min": lo, "speed_max": hi,
            "dir_min": (dlo, 0.0, 0.0), "dir_max": (dhi, 0.0, 0.0),
            "spawn_radius": max(num("spawnradius", 15.0), 0.0), "drag": 0.0}


def _smokevolume_params(ent, num, size):
    """func_smokevolume (Source 1 C_FuncSmokeVolume): the box is filled with smoke puffs every
    ParticleSpacingDistance units, ParticleDrawWidth wide, colored between Color1 and Color2,
    as thick as Density and turning at RotationSpeed. They stay for good."""
    spacing = max(num("ParticleSpacingDistance", 80.0), 8.0)
    count = 1
    for k in range(3):
        count *= max(1, round(size[k] / spacing))
    radius = max(num("ParticleDrawWidth", 120.0), 1.0) / 2.0
    c1 = _rgb(ent.get("Color1"), (255, 255, 255))
    c2 = _rgb(ent.get("Color2"), (255, 255, 255))
    return {"_template": "smoke", "count": min(count, 2000), "life_min": 3600.0, "life_max": 3600.0,
            "radius_min": radius * 0.9, "radius_max": radius * 1.1, "end_scale": 1.0,
            "alpha": min(max(num("Density", 1.0), 0.0), 1.0) * 0.7,
            "color_min": tuple(min(a, b) for a, b in zip(c1, c2)),
            "color_max": tuple(max(a, b) for a, b in zip(c1, c2)), "color_end": None,
            "spin": max(abs(num("RotationSpeed", 10.0)), 0.0), "speed_min": 0.0, "speed_max": 0.0,
            "dir_min": (0.0, 0.0, 0.0), "dir_max": (0.0, 0.0, 0.0), "drag": 0.0}


def _generated_vpcf(effects, kind, params):
    """Path of the particle file for these values; effects["generated"] collects them for the
    pipeline, which writes the files."""
    def norm(v):
        if isinstance(v, tuple):
            return tuple(round(x, 3) for x in v)
        return v if v is None or isinstance(v, str) else round(v, 3)
    key = repr(sorted((k, norm(v)) for k, v in params.items()))
    table = effects.setdefault("generated", {})
    for rel, (k, p, saved) in table.items():
        if k == kind and saved == key:
            return rel
    rel = f"{GENERATED_VPCF_DIR}/{kind}_{sum(1 for v in table.values() if v[0] == kind) + 1}.vpcf"
    table[rel] = (kind, params, key)
    return rel


def _effect_props(ent, cls, tracer=None, lights=None):
    """(kind, origin, props) of the info_particle_system that replaces ent, or None."""
    num = lambda key, default: _float(ent.get(key), default)  # noqa: E731
    color = (ent.get("rendercolor") or "255 255 255").strip()
    if cls == "env_steam":
        alpha = min(max(num("renderamt", 255.0) / 255.0, 0.0), 1.0)
        if (ent.get("type") or "0").strip() == "1":
            alpha = min(alpha, HEATWAVE_ALPHA)
        # data control point 1 = particle radius, jet length, alpha
        return "steam", _vec(ent.get("origin")), {
            "start_active": "1" if (ent.get("InitialState") or "0").strip() == "1" else "0",
            "data_cp_value": f"{_fmt(max(num('StartSize', 10.0), 1.0))} {_fmt(max(num('JetLength', 80.0), 1.0))} {_fmt(alpha)}",
            "tint_cp_color": color}
    if cls in ("func_dustmotes", "func_dustcloud", "env_embers"):
        b = _solid_bounds(ent)
        if b is None:
            return None
        mins, maxs = b
        if cls == "env_embers":
            # spawnflag 1 = start on
            kind, active, tint = "embers", _spawnflags(ent) & 1, color
        else:
            kind = "dustmotes" if cls == "func_dustmotes" else "dustcloud"
            active = (ent.get("StartDisabled") or "0").strip() != "1"
            tint = (ent.get("Color") or "255 255 255").strip()
        # data control point 1 = size of the box the particles fill
        props = {"start_active": "1" if active else "0",
                 "data_cp_value": " ".join(_fmt(maxs[k] - mins[k]) for k in range(3)),
                 "tint_cp_color": " ".join((tint.split() + ["255"] * 3)[:3])}
        # a system is not drawn farther than this from its middle, so a long box would vanish
        # while the player is still inside its far end
        half = 0.5 * sum((maxs[k] - mins[k]) ** 2 for k in range(3)) ** 0.5
        draw = max(BOX_DRAW_DISTANCE, round(half + BOX_DRAW_DISTANCE * 0.6))
        if kind == "dustmotes":
            # the bundled dustmotes.vpcf as it is; a long box gets a copy that is drawn
            # from farther away
            if draw > BOX_DRAW_DISTANCE:
                props["_params"] = {"_template": "dustmotes", "draw_distance": draw}
        else:
            params = _embers_params(num) if kind == "embers" else _dustcloud_params(ent, num)
            params["draw_distance"] = draw
            props["_params"] = params
        return kind, tuple((mins[k] + maxs[k]) / 2 for k in range(3)), props
    if cls == "func_smokevolume":
        b = _solid_bounds(ent)
        if b is None:
            return None
        mins, maxs = b
        size = tuple(maxs[k] - mins[k] for k in range(3))
        # data control point 1 = size of the box the puffs fill; the colors are in the particle
        return "smokevolume", tuple((mins[k] + maxs[k]) / 2 for k in range(3)), {
            "start_active": "1", "data_cp_value": " ".join(_fmt(v) for v in size),
            "tint_cp_color": "255 255 255", "_params": _smokevolume_params(ent, num, size)}
    if cls == "env_smoketrail":
        return "smoketrail", _vec(ent.get("origin")), {
            "start_active": "1", "data_cp_value": "0 0 0", "tint_cp_color": "255 255 255",
            "_params": _smoketrail_params(ent, num)}
    if cls == "point_spotlight":
        length = max(num("SpotlightLength", 500.0), 1.0)
        fwd = _forward(_vec(ent.get("angles")))
        origin = _vec(ent.get("origin"))
        if tracer is not None and origin is not None:
            # the Source 1 beam ends where it hits a brush, SpotlightLength is its longest
            length = max(tracer.trace(origin, fwd, length), 1.0)
        # the particle beam looks longer than the Source 1 one, and a dim light gets a short beam
        length = max(length * LIGHT_RAY_SCALE * _beam_brightness(ent, origin, lights), 1.0)
        flags = _spawnflags(ent)
        # data control point 1 = length, width, alpha (light_ray.vpcf); the beam goes along the
        # particle system's own angles, so turning it in Hammer turns the beam. Width and alpha
        # grow with the length like in the earlier version of the particle (3..15, 0.05..0.5).
        width = 3.0 + 12.0 * min(length / 512.0, 1.0) ** 0.8
        alpha = 0.05 + 0.45 * min(length / 128.0, 1.0) ** 1.2
        return "spotlight", _vec(ent.get("origin")), {
            "start_active": "1" if flags & 1 else "0",
            "data_cp_value": f"{_fmt(length)} {_fmt(round(width, 2))} {_fmt(round(alpha, 3))}",
            "tint_cp_color": color}
    if cls == "env_spritetrail":
        # data control point 1 = lifetime, start width, end width; the particles stay where
        # they were made while the entity (and its parent) moves on, which draws the trail
        alpha = min(max(num("renderamt", 255.0) / 255.0, 0.0), 1.0)
        rgb = (_vec(color) or (255.0, 255.0, 255.0))[:3]
        return "trail", _vec(ent.get("origin")), {
            "start_active": "1",
            "data_cp_value": f"{_fmt(max(num('lifetime', 0.5), 0.05))} {_fmt(max(num('startwidth', 8.0), 0.5))} "
                             f"{_fmt(max(num('endwidth', 1.0), 0.0))}",
            # additive: a lower alpha is a darker color
            "tint_cp_color": " ".join(_fmt(round(c * alpha)) for c in rgb)}
    return None


def _effects_to_particles(root, effects):
    """env_steam, func_dustmotes, func_dustcloud, env_embers, point_spotlight and env_spritetrail
    do not exist in CS2. They become info_particle_system entities with the same name (so outputs
    still reach them). effects receives {"props": {pos_key: props}, "names": {name: kind},
    "kinds": {kind: n}, "generated": {vpcf: (kind, params, key)}} for the .vmap step, which sets
    the particle keys again after the import."""
    count = 0
    tracer = lights = None
    for ent in root.children("entity"):
        cls = ent.classname
        if cls not in EFFECT_CLASSES:
            continue
        if cls == "point_spotlight" and tracer is None:
            tracer = BrushTracer(root)
            lights = _light_positions(root)
        made = _effect_props(ent, cls, tracer, lights)
        if made is None or made[1] is None:
            continue
        kind, origin, props = made
        params = props.pop("_params", None)
        effect = _generated_vpcf(effects, kind, params) if params else EFFECT_VPCFS[kind]
        props = dict(props, classname="info_particle_system", effect_name=effect,
                     data_cp="1", tint_cp="2")
        ent.items = [i for i in ent.items if isinstance(i, list) and i[0].lower() in _EFFECT_KEEP
                     or isinstance(i, VNode) and i.name.lower() == "connections"]
        ent.set("origin", " ".join(_fmt(c) for c in origin))
        for k, v in props.items():
            ent.set(k, v)
        effects.setdefault("props", {})[pos_key(origin)] = props
        name = (ent.get("targetname") or "").strip().lower()
        if name:
            effects.setdefault("names", {})[name] = kind
        kinds = effects.setdefault("kinds", {})
        kinds[kind] = kinds.get(kind, 0) + 1
        count += 1
    return count


# entities CS2 does not have and that do nothing without their Source 1 code
REMOVED_CLASSES = ("point_viewcontrol", "point_viewcontrol_multiplayer", "point_viewcontrol_survivor")
# the map's own cubemaps: the light probe volumes the program adds bake the reflections
CUBEMAP_CLASSES = ("env_cubemap",)


def _remove_classes(root, classes=REMOVED_CLASSES):
    before = len(root.items)
    root.items = [i for i in root.items
                  if not (isinstance(i, VNode) and i.name.lower() == "entity" and i.classname in classes)]
    return before - len(root.items)


def _sprite_alpha(ent):
    """Sprites without renderamt are fully visible in Source 1; the importer would make their
    glow alpha 0."""
    if ent.get("renderamt") is None:
        ent.set("renderamt", "255")


def fix_for_cs2(root, sky_alias=None, attributes=None, meshes=None, teleport_key=None,
                split_triggers=False, effects=None, log=None, bhop_blocks=False, world=None):
    """Applies the VMF fixes made before the import. Returns a summary of the changes.
    attributes: dict that receives the attribute names of the converted name / class filters.
    meshes: dict that receives the func_brush entities to turn into meshes (see vmap).
    teleport_key: when set, trigger_teleports with a landmark use the bhop script.
    split_triggers: very wide and thin trigger boxes are split into pieces.
    effects: dict that receives the particle entities made from env_steam etc.
    bhop_blocks: Source 1 bhop blocks become bhop script blocks (needs teleport_key).
    world: the compiled map (lighting.BspTracer), for the thin triggers that grow down towards
    the floor."""
    stats = {"base": 0, "targetnames": 0, "lights": 0, "brushes": 0, "render": 0,
             "dynprops": 0, "skins": 0, "perfmode": 0, "particles": 0, "physbox": 0,
             "skiphint_removed": 0, "skiphint_replaced": 0, "skybox": 0, "ladders": 0,
             "booster": 0, "filters": 0, "meshes": 0, "teleports": 0, "boost": 0, "split": 0,
             "floor": 0, "effects": 0, "removed": 0, "bhop_blocks": 0, "flag_triggers": 0, "trigger_clip": 0,
             "cubemaps": 0, "zones": 0, "spawns": 0, "nonsolid": 0}
    _fix_base(root)
    stats["removed"] = _remove_classes(root)
    stats["cubemaps"] = _remove_classes(root, CUBEMAP_CLASSES)
    stats["zones"] = _zones_to_triggers(root)
    stats["spawns"] = _fill_spawns(root)
    stats["nonsolid"] = _nonsolid_disps(root)
    if sky_alias:
        stats["skybox"] = _swap_sky_material(root, sky_alias)
    stats["ladders"] = _ladders_to_world(root)
    stats["trigger_clip"] = _world_triggers_to_clip(root)
    stats["booster"] = _remove_booster_filter(root)
    if bhop_blocks and teleport_key:
        stats["bhop_blocks"], stats["flag_triggers"] = _bhop_blocks(root, teleport_key)
    stats["filters"] = _name_filters_to_attribute(root, attributes if attributes is not None else {})
    if split_triggers:
        stats["boost"] = _raise_boost_triggers(root)
        stats["split"] = _split_triggers(root)
        odd, stats["floor"] = _fix_odd_triggers(root, world)
        stats["split"] += odd
    if teleport_key:
        stats["teleports"] = _teleports_to_bhop(root, teleport_key)
    stats["meshes"] = _brushes_to_meshes(root, meshes if meshes is not None else {})
    stats["targetnames"] = _fix_special_targetnames(root)

    for ent in root.children("entity"):
        cls = ent.classname
        # light color mode
        if cls in ("light", "light_spot"):
            ent.set("colormode", "0")
            stats["lights"] += 1
        if _fix_brush_entity(ent):
            stats["brushes"] += 1
        # renderfx / rendermode
        for item in ent.items:
            if isinstance(item, list):
                kl = item[0].lower()
                if kl == "renderfx" and item[1] in _RENDERFX:
                    item[1] = _RENDERFX[item[1]]
                    stats["render"] += 1
                elif kl == "rendermode" and item[1] in _RENDERMODE:
                    item[1] = _RENDERMODE[item[1]]
                    stats["render"] += 1
                elif kl == "performancemode":
                    if item[1] in ("0", "2"):
                        item[1] = "PM_NORMAL"
                        stats["perfmode"] += 1
                    elif item[1] in ("1", "3"):
                        item[1] = "PM_NO_GIBS"
                        stats["perfmode"] += 1
                elif kl == "skin" and item[1] == "0":
                    item[1] = "default"
                    stats["skins"] += 1
        if _fix_dynamic_prop(ent):
            stats["dynprops"] += 1
        cls = ent.classname
        if cls == "env_lightglow":
            ent.items[ent.index_of_key("classname")] = ["classname", "env_sprite"]
            _sprite_alpha(ent)
            stats["particles"] += 1
        elif cls == "env_sprite_clientside":
            idx = ent.index_of_key("classname")
            ent.items[idx] = ["classname", "env_sprite"]
            ent.items.insert(idx + 1, ["clientSideEntity", "1"])
            _sprite_alpha(ent)
            stats["particles"] += 1
        elif cls == "env_sprite":
            _sprite_alpha(ent)
        elif cls == "func_physbox_multiplayer":
            ent.items[ent.index_of_key("classname")] = ["classname", "func_physbox"]
            stats["physbox"] += 1

    stats["effects"] = _effects_to_particles(root, effects if effects is not None else {})
    stats["skiphint_removed"], stats["skiphint_replaced"] = _remove_skip_hint(root)
    if log:
        from .i18n import t
        parts = []
        for k in ("targetnames", "lights", "brushes", "render", "dynprops", "skins", "perfmode",
                  "particles", "physbox", "skiphint_removed", "skiphint_replaced", "skybox", "ladders",
                  "booster", "filters", "meshes", "teleports", "boost", "split", "floor", "effects", "removed",
                  "bhop_blocks", "flag_triggers", "trigger_clip", "cubemaps", "zones", "spawns", "nonsolid"):
            if stats.get(k):
                parts.append(t("vf_" + k, n=stats[k]))
        log(t("vf_title", parts=", ".join(parts) if parts else t("vf_none")), "ok")
        for p in parts:
            log("  - " + p, "tool")
    return stats
