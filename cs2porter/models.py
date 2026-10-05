"""Model conversion (with tehlikeli91's vmdl_converter.py template).

1) .mdl from embedded / game files -> mdl.py to QC + SMD -> .vmdl
2) QC + SMD from the Resources folders -> .vmdl
Only animated models go through the CS2 model importer, so their animations are kept.
"""

import json
import os
import re
import shutil
import struct

from . import kv
from . import mdl as mdlmod
from .i18n import t
from .legacy.vmdl_converter import read_smd_materials
from .materials import out_name

_MODELNAME_RE = re.compile(r'^\s*\$modelname\s+"?([^"\r\n]+?)"?\s*$', re.IGNORECASE | re.MULTILINE)
MDL_COMPANION_EXTS = (".mdl", ".vvd", ".dx90.vtx", ".dx80.vtx", ".sw.vtx", ".vtx", ".phy", ".ani")


def norm_model(name):
    n = name.strip().replace("\\", "/").lower().lstrip("/")
    if not n.startswith("models/"):
        n = "models/" + n
    if not n.endswith(".mdl"):
        n = os.path.splitext(n)[0] + ".mdl"
    return n


# ---------------------------------------------------------------------------
# QC index
# ---------------------------------------------------------------------------

class QCIndex:
    def __init__(self, resource_sources, cache_dir, log=None):
        self.by_model = {}     # models/x/y.mdl -> qc_abs
        self.by_stem = {}      # y -> [qc_abs]
        cache_file = os.path.join(cache_dir, "qc_index.json")
        cache = {}
        if os.path.isfile(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cache = json.load(f)
            except (OSError, ValueError):
                cache = {}
        new_cache = {}
        for src in resource_sources:
            for rel, abs_path in src.files_with_ext(".qc"):
                try:
                    mtime = int(os.path.getmtime(abs_path))
                except OSError:
                    continue
                entry = cache.get(abs_path)
                if entry and entry[0] == mtime:
                    modelname = entry[1]
                else:
                    modelname = ""
                    try:
                        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                            m = _MODELNAME_RE.search(f.read())
                        if m:
                            modelname = m.group(1)
                    except OSError:
                        pass
                new_cache[abs_path] = [mtime, modelname]
                if modelname:
                    self.by_model.setdefault(norm_model(modelname), abs_path)
                stem = os.path.splitext(os.path.basename(abs_path))[0].lower()
                self.by_stem.setdefault(stem, []).append(abs_path)
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(new_cache, f)
        except OSError:
            pass

    def find(self, mdl):
        mdl = norm_model(mdl)
        qc = self.by_model.get(mdl)
        if qc:
            return qc
        # if $modelname does not match, only a QC with the same name in the same folder layout is used
        # (so a model with the same name from another folder is never picked by mistake)
        stem = os.path.splitext(os.path.basename(mdl))[0]
        folder = "/" + os.path.dirname(mdl) + "/"
        for p in self.by_stem.get(stem, []):
            if folder in p.replace("\\", "/").lower():
                return p
        return None


# ---------------------------------------------------------------------------
# QC parsing
# ---------------------------------------------------------------------------

class QCInfo:
    def __init__(self):
        self.modelname = ""
        self.cdmaterials = []
        self.bodies = []          # render SMD files
        self.collision = None
        self.scale = 1.0
        self.skins = []           # [[mat, mat], [mat, mat]]
        self.surfaceprop = ""
        self.staticprop = False


def _tokens(text):
    out = []
    for kind, val in kv.tokenize(text):
        out.append((kind, val))
    return out


def _skip_block(toks, i):
    """If toks[i] == '{', returns the index after the matching '}'."""
    if i < len(toks) and toks[i][0] == "{":
        depth = 0
        while i < len(toks):
            if toks[i][0] == "{":
                depth += 1
            elif toks[i][0] == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
    return i


def parse_qc(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        toks = _tokens(f.read())
    q = QCInfo()
    i, n = 0, len(toks)

    def val(j):
        return toks[j][1] if j < n and toks[j][0] == "str" else ""

    while i < n:
        kind, tok = toks[i]
        if kind == "{":
            i = _skip_block(toks, i)
            continue
        t = tok.lower()
        if t == "$modelname":
            q.modelname = val(i + 1)
            i += 2
        elif t == "$cdmaterials":
            q.cdmaterials.append(val(i + 1))
            i += 2
        elif t == "$body":
            q.bodies.append(val(i + 2))
            i += 3
        elif t == "$model":
            q.bodies.append(val(i + 2))
            i = _skip_block(toks, i + 3)
        elif t == "$bodygroup":
            j = i + 2
            first = None
            if j < n and toks[j][0] == "{":
                j += 1
                while j < n and toks[j][0] != "}":
                    if toks[j][0] == "str" and toks[j][1].lower() == "studio":
                        if first is None:
                            first = val(j + 1)
                        j += 2
                    elif toks[j][0] == "{":
                        j = _skip_block(toks, j)
                    else:
                        j += 1
                j += 1
            if first:
                q.bodies.append(first)
            i = j
        elif t in ("$collisionmodel", "$collisionjoints"):
            if q.collision is None:
                q.collision = val(i + 1)
            i = _skip_block(toks, i + 2)
        elif t == "$scale":
            try:
                q.scale = float(val(i + 1))
            except ValueError:
                pass
            i += 2
        elif t == "$surfaceprop":
            q.surfaceprop = val(i + 1)
            i += 2
        elif t == "$staticprop":
            q.staticprop = True
            i += 1
        elif t == "$texturegroup":
            j = i + 2
            rows = []
            if j < n and toks[j][0] == "{":
                j += 1
                while j < n and toks[j][0] != "}":
                    if toks[j][0] == "{":
                        row = []
                        j += 1
                        while j < n and toks[j][0] != "}":
                            if toks[j][0] == "str":
                                row.append(toks[j][1])
                            j += 1
                        rows.append(row)
                    j += 1
                j += 1
            q.skins = rows
            i = j
        else:
            i += 1
    q.bodies = [b for b in q.bodies if b]
    return q


# ---------------------------------------------------------------------------
# VMDL creation (vmdl_converter.py format)
# ---------------------------------------------------------------------------

def _fmt_scale(s):
    return f"{s:.6g}" if "." in f"{s:.6g}" else f"{s:.6g}.0"


def build_vmdl(model_dir, render_smds, physics_smd, remaps, skin_groups, scale=1.0):
    T = "\t"
    sc = _fmt_scale(scale)
    remap_lines = []
    for frm, to in remaps:
        remap_lines.append(f"{T*7}{{\n{T*8}from = \"{frm}\"\n{T*8}to = \"{to}\"\n{T*7}}},")
    groups = (
        f"{T*5}{{\n"
        f"{T*6}_class = \"DefaultMaterialGroup\"\n"
        f"{T*6}remaps = \n"
        f"{T*6}[\n"
        + "\n".join(remap_lines) + "\n"
        f"{T*6}]\n"
        f"{T*6}use_global_default = false\n"
        f"{T*6}global_default_material = \"\"\n"
        f"{T*5}}},\n"
    )
    for idx, group in enumerate(skin_groups, start=1):
        lines = []
        for frm, to in group:
            lines.append(f"{T*7}{{\n{T*8}from = \"{frm}\"\n{T*8}to = \"{to}\"\n{T*7}}},")
        groups += (
            f"{T*5}{{\n"
            f"{T*6}_class = \"MaterialGroup\"\n"
            f"{T*6}name = \"{idx}\"\n"
            f"{T*6}remaps = \n"
            f"{T*6}[\n"
            + "\n".join(lines) + "\n"
            f"{T*6}]\n"
            f"{T*5}}},\n"
        )

    physics = ""
    if physics_smd:
        stem = os.path.splitext(physics_smd)[0]
        physics = (
            f'{T*3}{{\n'
            f'{T*4}_class = "PhysicsShapeList"\n'
            f'{T*4}children = \n'
            f'{T*4}[\n'
            f'{T*5}{{\n'
            f'{T*6}_class = "PhysicsMeshFile"\n'
            f'{T*6}name = "{stem}"\n'
            f'{T*6}parent_bone = ""\n'
            f'{T*6}surface_prop = "default"\n'
            f'{T*6}collision_prop = "default"\n'
            f'{T*6}tool_material = ""\n'
            f'{T*6}recenter_on_parent_bone = false\n'
            f'{T*6}offset_origin = [ 0.0, 0.0, 0.0 ]\n'
            f'{T*6}offset_angles = [ 0.0, 0.0, 0.0 ]\n'
            f'{T*6}filename = "{model_dir}/{physics_smd}"\n'
            f'{T*6}import_scale = {sc}\n'
            f'{T*6}simplification_params = \n'
            f'{T*6}{{\n'
            f'{T*7}qemError = 0.0\n'
            f'{T*7}maxMeshVertices = 0\n'
            f'{T*7}small_element_threshold = 0.0\n'
            f'{T*7}thin_element_threshold = 0.0\n'
            f'{T*6}}}\n'
            f'{T*6}import_filter = \n'
            f'{T*6}{{\n'
            f'{T*7}exclude_by_default = false\n'
            f'{T*7}exception_list = [  ]\n'
            f'{T*6}}}\n'
            f'{T*5}}},\n'
            f'{T*4}]\n'
            f'{T*4}leave_body_collision_unmodified = false\n'
            f'{T*3}}},\n'
        )

    render = ""
    for smd in render_smds:
        render += (
            f'{T*5}{{\n'
            f'{T*6}_class = "RenderMeshFile"\n'
            f'{T*6}filename = "{model_dir}/{smd}"\n'
            f'{T*6}import_scale = {sc}\n'
            f'{T*6}import_filter = \n'
            f'{T*6}{{\n'
            f'{T*7}exclude_by_default = false\n'
            f'{T*7}exception_list = [  ]\n'
            f'{T*6}}}\n'
            f'{T*5}}},\n'
        )

    return (
        f'<!-- kv3 encoding:text:version{{e21c7f3c-8a33-41c5-9977-a76d3a32aa0d}} format:modeldoc41:version{{12fc9d44-453a-4ae4-b4d9-7e2ac0bbd4e0}} -->\n'
        f'{{\n'
        f'{T}rootNode = \n'
        f'{T}{{\n'
        f'{T*2}_class = "RootNode"\n'
        f'{T*2}children = \n'
        f'{T*2}[\n'
        f'{T*3}{{\n'
        f'{T*4}_class = "MaterialGroupList"\n'
        f'{T*4}children = \n'
        f'{T*4}[\n'
        f'{groups}'
        f'{T*4}]\n'
        f'{T*3}}},\n'
        f'{physics}'
        f'{T*3}{{\n'
        f'{T*4}_class = "RenderMeshList"\n'
        f'{T*4}children = \n'
        f'{T*4}[\n'
        f'{render}'
        f'{T*4}]\n'
        f'{T*3}}},\n'
        f'{T*2}]\n'
        f'{T*2}model_archetype = ""\n'
        f'{T*2}primary_associated_entity = ""\n'
        f'{T*2}anim_graph_name = ""\n'
        f'{T*2}document_sub_type = "ModelDocSubType_None"\n'
        f'{T}}}\n'
        f'}}\n'
    )


# ---------------------------------------------------------------------------
# Converter
# ---------------------------------------------------------------------------

class ModelResult:
    def __init__(self, mdl, status, detail="", source="", materials=()):
        self.mdl = mdl
        self.status = status          # created | exists | cs2 | missing | error
        self.detail = detail
        self.source = source
        self.materials = list(materials)


def _norm_cd(cd):
    c = cd.strip().replace("\\", "/").lower().strip("/")
    if c.startswith("materials/"):
        c = c[len("materials/"):]
    return c


class ModelConverter:
    def __init__(self, sources, index, matconv, content_dir, qc_index, valve=None,
                 staging_dir=None, log=None, overwrite=False, convert_cs2_existing=False):
        self.sources = sources
        self.index = index
        self.matconv = matconv
        self.content_dir = content_dir
        self.qc_index = qc_index
        self.valve = valve
        self.staging_dir = staging_dir
        self.log = log or (lambda m, t="info": None)
        self.overwrite = overwrite
        self.convert_cs2_existing = convert_cs2_existing
        self.use_valve = valve is not None and valve.has_cs2_tools
        self.results = {}

    def convert(self, mdl):
        mdl = norm_model(mdl)
        if mdl in self.results:
            return self.results[mdl]
        try:
            res = self._convert(mdl)
        except Exception as e:  # noqa: BLE001
            res = ModelResult(mdl, "error", str(e))
        self.results[mdl] = res
        return res

    def _convert(self, mdl):
        stem_rel = out_name(mdl[:-4])
        vmdl_rel = stem_rel + ".vmdl"
        if not self.overwrite and self.index.in_addon(vmdl_rel):
            return ModelResult(mdl, "exists", t("m_exists"))
        if not self.convert_cs2_existing and self.index.in_cs2(vmdl_rel + "_c"):
            return ModelResult(mdl, "cs2", t("m_cs2"))

        compiled = [s for s in self.sources.compiled_sources() if s.has(mdl)]
        last = None
        # 1) compiled model embedded in the map (or in the extra folder): the map's own version
        for src in compiled:
            if src.role in ("extra", "embedded"):
                r = self._from_mdl(mdl, src)
                if r.status == "created":
                    return r
                last = r
        # 2) QC + SMD from Resources
        qc = self.qc_index.find(mdl) if self.qc_index else None
        if qc:
            r = self._from_qc(mdl, qc, fix_rotation=True)
            if r.status == "created":
                return r
            self.log(t("md_qc_failed", mdl=mdl, detail=r.detail), "warn")
            last = r
        # 3) files of the resource folders, then of the installed games, then any file of the
        #    extra folder with the model's name
        for src in compiled:
            if src.role in ("resources", "game", "loose"):
                r = self._from_mdl(mdl, src)
                if r.status == "created":
                    return r
                last = r
        return last or ModelResult(mdl, "missing", t("md_missing"))

    # --- compiled model (.mdl) -------------------------------------------------
    def _from_mdl(self, mdl, src):
        stage = os.path.join(self.staging_dir, "decompiled", *os.path.dirname(mdl).split("/"))

        def read(rel):
            try:
                return src.read(rel) if src.has(rel) else None
            except (OSError, KeyError):
                return None

        try:
            dec = mdlmod.decompile(read, mdl, stage)
        except (mdlmod.MDLError, ValueError, IndexError, KeyError, OSError) as e:
            self.log(t("md_decompile_fail", mdl=mdl, e=e), "warn")
            if self.use_valve:
                return self._valve(mdl, src)
            return ModelResult(mdl, "error", str(e), src.label)
        if dec.mdl.is_animated and self.use_valve:
            r = self._valve(mdl, src, animated=True)
            if r.status == "created":
                return r
        r = self._from_qc(mdl, dec.qc, fix_rotation=False, label=src.label, decompiled=True)
        return r

    # --- QC + SMD -----------------------------------------------------------------
    def _from_qc(self, mdl, qc_path, fix_rotation, label=None, decompiled=False):
        q = parse_qc(qc_path)
        qc_dir = os.path.dirname(qc_path)
        bodies = []
        for b in q.bodies:
            p = os.path.join(qc_dir, b)
            if os.path.isfile(p):
                bodies.append(b)
        if not bodies:
            return ModelResult(mdl, "error", t("md_no_smd"), label or qc_path)
        model_dir = os.path.dirname(out_name(mdl))           # models/props/de_dust
        dest_dir = os.path.join(self.content_dir, *model_dir.split("/"))
        os.makedirs(dest_dir, exist_ok=True)
        # Non-static models are stored in compiled space and CS2 rotates the SMD 90 degrees,
        # so these copies are fixed (static props are already correct).
        rotate = fix_rotation and not q.staticprop

        def copy(name):
            src = os.path.join(qc_dir, name)
            dst = os.path.join(dest_dir, os.path.basename(name))
            if self.overwrite or decompiled or not os.path.isfile(dst):
                if rotate:
                    mdlmod.rotate_smd_file(src, dst)
                else:
                    shutil.copyfile(src, dst)
            return os.path.basename(name)

        render = [copy(b) for b in bodies]
        physics = None
        if q.collision and os.path.isfile(os.path.join(qc_dir, q.collision)):
            physics = copy(q.collision)
        try:
            shutil.copyfile(qc_path, os.path.join(dest_dir, os.path.basename(qc_path)))
        except OSError:
            pass

        # materials
        smd_mats = []
        seen = set()
        for b in bodies:
            for m in read_smd_materials(os.path.join(qc_dir, b)):
                ml = m.lower()
                if ml not in seen:
                    seen.add(ml)
                    smd_mats.append(m)
        cds = [_norm_cd(c) for c in q.cdmaterials] or [_norm_cd(os.path.dirname(mdl)[len("models/"):])]
        needed = []
        remaps = []
        resolved_cache = {}

        def resolve(name):
            key = name.lower()
            if key in resolved_cache:
                return resolved_cache[key]
            base = os.path.splitext(name.replace("\\", "/").lower())[0]
            cands = []
            for cd in cds:
                cands.append(f"{cd}/{base}" if cd else base)
            if "/" in base:
                cands.append(base)
            chosen = next((c for c in cands if self.matconv.material_available(c)), cands[0])
            resolved_cache[key] = chosen
            if chosen not in needed:
                needed.append(chosen)
            return chosen

        for m in smd_mats:
            remaps.append((f"{m}.vmat", f"materials/{out_name(resolve(m))}.vmat"))
        if not remaps:
            name = os.path.splitext(os.path.basename(mdl))[0]
            remaps.append((f"{name}.vmat", f"materials/{out_name(resolve(name))}.vmat"))

        skin_groups = []
        if len(q.skins) > 1:
            base_row = q.skins[0]
            for row in q.skins[1:]:
                group = []
                for a, b in zip(base_row, row):
                    if a.lower() == b.lower():
                        continue
                    group.append((f"{a}.vmat", f"materials/{out_name(resolve(b))}.vmat"))
                if group:
                    skin_groups.append(group)

        content = build_vmdl(model_dir, render, physics, remaps, skin_groups, q.scale)
        vmdl_abs = os.path.join(self.content_dir, *(out_name(mdl[:-4]) + ".vmdl").split("/"))
        with open(vmdl_abs, "w", encoding="utf-8") as f:
            f.write(content)
        if label is None:
            label = next((s.label for s in self.sources.resource_sources()
                          if os.path.normcase(qc_path).startswith(os.path.normcase(getattr(s, "root", "")))),
                         "Resources")
        detail = t("md_from_mdl" if decompiled else "md_from_qc", n=len(render))
        if physics:
            detail += t("md_phys")
        if skin_groups:
            detail += t("md_skins", n=len(skin_groups) + 1)
        return ModelResult(mdl, "created", detail, label, needed)

    # --- CS2 model importer (animated models and fallback only) -------------------
    def _stage(self, mdl, src):
        stem = mdl[:-4]
        ok = False
        for ext in MDL_COMPANION_EXTS:
            rel = stem + ext
            if src.has(rel):
                try:
                    data = src.read(rel)
                except Exception:  # noqa: BLE001
                    continue
                dest = os.path.join(self.staging_dir, "valve", *rel.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(data)
                if ext == ".mdl":
                    ok = True
                    self._stage_vmts(data)
        return ok

    def _stage_vmts(self, data):
        """Stages the model's VMTs next to it: without them the importer writes bare material
        names (no folder) into the meshes and the materials are never found."""
        try:
            m = mdlmod.MDL(data)
        except (mdlmod.MDLError, struct.error, IndexError, ValueError):
            return
        cds = [c.replace("\\", "/").strip("/").lower() for c in m.cdmaterials] or [""]
        for tex in m.textures:
            name = tex.replace("\\", "/").strip("/").lower()
            for cd in cds:
                rel = "materials/" + (f"{cd}/{name}" if cd else name) + ".vmt"
                vmt, _src = self.sources.read(rel)
                if vmt is None:
                    continue
                dest = os.path.join(self.staging_dir, "valve", *rel.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(vmt)
                break

    def _valve(self, mdl, src, animated=False):
        if not self._stage(mdl, src):
            return ModelResult(mdl, "missing", t("md_missing"), src.label)
        ok, out = self.valve.cs_mdl_import(os.path.join(self.staging_dir, "valve"), self.content_dir, mdl)
        vmdl_abs = os.path.join(self.content_dir, *(mdl[:-4] + ".vmdl").split("/"))
        if not ok or not os.path.isfile(vmdl_abs):
            tail = out.strip().splitlines()[-1] if out.strip() else t("md_no_output")
            return ModelResult(mdl, "error", t("md_valve_fail", tail=tail), src.label)
        mats = []
        refs = os.path.join(self.content_dir, *(mdl[:-4] + "_refs.txt").split("/"))
        if os.path.isfile(refs):
            try:
                root = kv.parse_file(refs)
                for blk in root.blocks():
                    for k, v in blk.values():
                        if k.lower() == "file" and v.lower().endswith(".vmt"):
                            m = v.replace("\\", "/").lower()
                            if m.startswith("materials/"):
                                m = m[len("materials/"):]
                            mats.append(m[:-4])
            except OSError:
                pass
            # only the importer's list of the files it used: not part of the model
            try:
                os.remove(refs)
            except OSError:
                pass
        key = "md_valve_anim" if animated else "md_valve"
        return ModelResult(mdl, "created", t(key, n=len(mats)), src.label, mats)
