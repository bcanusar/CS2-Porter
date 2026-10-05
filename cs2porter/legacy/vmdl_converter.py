import os
import re
import sys

def find_qc_files(root_dir):
    qc_files = []
    for dirpath, _, filenames in os.walk(root_dir):
        for f in filenames:
            if f.lower().endswith('.qc'):
                qc_files.append(os.path.join(dirpath, f))
    return qc_files


def build_vmat_index(root_dir):
    index = {}
    materials_dir = os.path.join(root_dir, "materials")
    if not os.path.isdir(materials_dir):
        return index
    for dirpath, _, filenames in os.walk(materials_dir):
        for f in filenames:
            if f.lower().endswith('.vmat'):
                abs_path = os.path.join(dirpath, f)
                rel = os.path.relpath(abs_path, root_dir).replace("\\", "/")
                key = f.lower()
                if key not in index:
                    index[key] = []
                index[key].append(rel)
    return index


def resolve_to(mat_name, model_path, vmat_index, cdmaterials=None):
    key = (mat_name + ".vmat").lower()
    if key in vmat_index and vmat_index[key]:
        found = vmat_index[key][0]
        if len(vmat_index[key]) > 1:
            print(f"    [INFO] Found duplicates of '{mat_name}.vmat'. First one is selected: {found}")
        return found

    if cdmaterials:
        return f"materials/{cdmaterials}/{mat_name}.vmat"

    mat_subpath = model_path
    if mat_subpath.startswith("models/"):
        mat_subpath = mat_subpath[len("models/"):]
    elif mat_subpath.lower() == "models":
        mat_subpath = ""
    if mat_subpath:
        return f"materials/{mat_subpath}/{mat_name}.vmat"
    return f"materials/{mat_name}.vmat"


def parse_bodygroup_smd(qc_content):
    pattern = r'\$bodygroup\s+"[^"]*"\s*\{[^}]*studio\s+"([^"]+\.smd)"[^}]*\}'
    m = re.search(pattern, qc_content, re.IGNORECASE | re.DOTALL)
    return m.group(1) if m else None


def parse_collision_smd(qc_content):
    pattern = r'\$collisionmodel\s+"([^"]+\.smd)"'
    m = re.search(pattern, qc_content, re.IGNORECASE)
    return m.group(1) if m else None


def parse_cdmaterials(qc_content):
    pattern = r'\$cdmaterials\s+"([^"]*)"'
    m = re.search(pattern, qc_content, re.IGNORECASE)
    if not m:
        return None
    raw = m.group(1).replace("\\", "/").strip("/")
    return raw if raw else None


def get_model_path(qc_dir, root_dir):
    rel = os.path.relpath(qc_dir, root_dir).replace("\\", "/")
    if rel == ".":
        return "models"
    parts = rel.split("/")
    for i, part in enumerate(parts):
        if part.lower() == "models":
            return "/".join(parts[i:])
    return "models/" + rel


def read_smd_materials(smd_path):
    materials = []
    seen = set()
    try:
        with open(smd_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except Exception as e:
        print(f"  [WARNING] Couldn't read SMD File: {e}")
        return materials

    lines = content.splitlines()
    in_triangles = False
    skip_next = 0

    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.lower() == 'triangles':
            in_triangles = True
            skip_next = 0
            continue
        if s.lower() == 'end':
            in_triangles = False
            continue
        if not in_triangles:
            continue
        if skip_next > 0:
            skip_next -= 1
            continue
        if len(s.split()) > 1 or s.startswith('#'):
            continue
        if s not in seen:
            seen.add(s)
            materials.append(s)
        skip_next = 3

    return materials


def build_remaps(materials, model_path, vmat_index, vmat_ref_collector, cdmaterials=None):
    T = "\t"
    lines = []
    mats = materials if materials else [model_path.split("/")[-1]]
    for mat in mats:
        to = resolve_to(mat, model_path, vmat_index, cdmaterials)
        from_val = f"{mat}.vmat"
        vmat_ref_collector[to] = from_val
        
        lines.append(
            f"{T*7}{{\n"
            f"{T*8}from = \"{from_val}\"\n"
            f"{T*8}to = \"{to}\"\n"
            f"{T*7}}},"
        )
    return "\n".join(lines)


def build_vmdl_with_physics(model_path, render_smd, physics_smd, materials, vmat_index, vmat_ref_collector, cdmaterials=None):
    T = "\t"
    physics_stem = os.path.splitext(physics_smd)[0]
    remaps = build_remaps(materials, model_path, vmat_index, vmat_ref_collector, cdmaterials)
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
        f'{T*5}{{\n'
        f'{T*6}_class = "DefaultMaterialGroup"\n'
        f'{T*6}remaps = \n'
        f'{T*6}[\n'
        f'{remaps}\n'
        f'{T*6}]\n'
        f'{T*6}use_global_default = false\n'
        f'{T*6}global_default_material = ""\n'
        f'{T*5}}},\n'
        f'{T*4}]\n'
        f'{T*3}}},\n'
        f'{T*3}{{\n'
        f'{T*4}_class = "PhysicsShapeList"\n'
        f'{T*4}children = \n'
        f'{T*4}[\n'
        f'{T*5}{{\n'
        f'{T*6}_class = "PhysicsMeshFile"\n'
        f'{T*6}name = "{physics_stem}"\n'
        f'{T*6}parent_bone = ""\n'
        f'{T*6}surface_prop = "default"\n'
        f'{T*6}collision_prop = "default"\n'
        f'{T*6}tool_material = ""\n'
        f'{T*6}recenter_on_parent_bone = false\n'
        f'{T*6}offset_origin = [ 0.0, 0.0, 0.0 ]\n'
        f'{T*6}offset_angles = [ 0.0, 0.0, 0.0 ]\n'
        f'{T*6}filename = "{model_path}/{physics_smd}"\n'
        f'{T*6}import_scale = 1.0\n'
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
        f'{T*3}{{\n'
        f'{T*4}_class = "RenderMeshList"\n'
        f'{T*4}children = \n'
        f'{T*4}[\n'
        f'{T*5}{{\n'
        f'{T*6}_class = "RenderMeshFile"\n'
        f'{T*6}filename = "{model_path}/{render_smd}"\n'
        f'{T*6}import_scale = 1.0\n'
        f'{T*6}import_filter = \n'
        f'{T*6}{{\n'
        f'{T*7}exclude_by_default = false\n'
        f'{T*7}exception_list = [  ]\n'
        f'{T*6}}}\n'
        f'{T*5}}},\n'
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


def build_vmdl_without_physics(model_path, render_smd, materials, vmat_index, vmat_ref_collector, cdmaterials=None):
    T = "\t"
    remaps = build_remaps(materials, model_path, vmat_index, vmat_ref_collector, cdmaterials)
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
        f'{T*5}{{\n'
        f'{T*6}_class = "DefaultMaterialGroup"\n'
        f'{T*6}remaps = \n'
        f'{T*6}[\n'
        f'{remaps}\n'
        f'{T*6}]\n'
        f'{T*6}use_global_default = false\n'
        f'{T*6}global_default_material = ""\n'
        f'{T*5}}},\n'
        f'{T*4}]\n'
        f'{T*3}}},\n'
        f'{T*3}{{\n'
        f'{T*4}_class = "RenderMeshList"\n'
        f'{T*4}children = \n'
        f'{T*4}[\n'
        f'{T*5}{{\n'
        f'{T*6}_class = "RenderMeshFile"\n'
        f'{T*6}filename = "{model_path}/{render_smd}"\n'
        f'{T*6}import_scale = 1.0\n'
        f'{T*6}import_filter = \n'
        f'{T*6}{{\n'
        f'{T*7}exclude_by_default = false\n'
        f'{T*7}exception_list = [  ]\n'
        f'{T*6}}}\n'
        f'{T*5}}},\n'
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


def process_qc_file(qc_path, root_dir, vmat_index, vmat_ref_collector):
    qc_dir = os.path.dirname(qc_path)
    print(f"\n[QC] {qc_path}")

    try:
        with open(qc_path, 'r', encoding='utf-8', errors='replace') as f:
            qc_content = f.read()
    except Exception as e:
        print(f"  [ERROR] Couldn't read QC File: {e}")
        return False

    render_smd = parse_bodygroup_smd(qc_content)
    if not render_smd:
        print(f"  [SKIP] Couldn't find .smd inside $bodygroup.")
        return False

    render_smd_path = os.path.join(qc_dir, render_smd)
    if not os.path.isfile(render_smd_path):
        print(f"  [SKIP] Render SMD not found in the folder: {render_smd_path}")
        return False

    print(f"  Render SMD : {render_smd}")

    collision_smd = parse_collision_smd(qc_content)
    has_physics = False
    if collision_smd:
        if os.path.isfile(os.path.join(qc_dir, collision_smd)):
            has_physics = True
            print(f"  Physics SMD: {collision_smd}")
        else:
            print(f"  [WARNING] Physics SMD not found in the folder, skipping.")

    model_path = get_model_path(qc_dir, root_dir)
    cdmaterials = parse_cdmaterials(qc_content)
    if cdmaterials:
        print(f"  CD Materials: {cdmaterials}")
    else:
        print(f"  [WARNING] $cdmaterials not found in QC, falling back to model path.")
    materials = read_smd_materials(render_smd_path)

    if materials:
        print(f"  Materials: {', '.join(materials)}")
    else:
        print(f"  [WARNING] Couldn't extract materials from SMD.")

    if has_physics:
        vmdl_content = build_vmdl_with_physics(model_path, render_smd, collision_smd, materials, vmat_index, vmat_ref_collector, cdmaterials)
    else:
        vmdl_content = build_vmdl_without_physics(model_path, render_smd, materials, vmat_index, vmat_ref_collector, cdmaterials)

    file_name_with_ext = os.path.basename(qc_path)
    qc_name = os.path.splitext(file_name_with_ext)[0]

    vmdl_name = os.path.splitext(qc_name)[0] + ".vmdl"
    vmdl_path = os.path.join(qc_dir, vmdl_name)

    try:
        with open(vmdl_path, 'w', encoding='utf-8') as f:
            f.write(vmdl_content)
        print(f"  [OK] Created: {vmdl_path}")
        return True
    except Exception as e:
        print(f"  [ERROR] Couldn't write VMDL: {e}")
        return False


def write_vmat_ref_file(root_dir, vmat_ref_collector):
    txt_path = os.path.join(root_dir, "missing_vmat_ref.txt")
    lines = []
    
    for to_val, from_val in sorted(vmat_ref_collector.items()):
        lines.append(f"{from_val} -> {to_val}")
        
    try:
        with open(txt_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines) + "\n")
        print(f"\n[OK] The necessary .vmat files were written to this folder: {txt_path}")
        print(f"     Unique VMAT Path Count: {len(vmat_ref_collector)}")
    except Exception as e:
        print(f"\n[ERROR] Couldn't write missing .vmat files to .txt: {e}")


def main(root_dir=None):
    root_dir = os.path.abspath(root_dir) if root_dir else os.path.dirname(os.path.abspath(__file__))

    print(f"Root Folder: {root_dir}")
    print(f"{'='*60}")
    print("The materials/ folder is being indexed...")
    vmat_index = build_vmat_index(root_dir)
    total_vmats = sum(len(v) for v in vmat_index.values())
    print(f"Found .vmat file: {total_vmats}")
    print(f"{'='*60}")

    qc_files = find_qc_files(root_dir)
    print(f"Found .qc file: {len(qc_files)}")

    vmat_ref_collector = {}

    created = 0
    skipped = 0

    for qc_path in qc_files:
        ok = process_qc_file(qc_path, root_dir, vmat_index, vmat_ref_collector)
        if ok:
            created += 1
        else:
            skipped += 1

    if vmat_ref_collector:
        write_vmat_ref_file(root_dir, vmat_ref_collector)
    else:
        print("\n[INFO] No missing vmat's were found, and no missing_vmat_ref.txt was created.")

    print(f"\n{'='*60}")
    print(f"DONE. Created: {created} | Skipped: {skipped}")


if __name__ == "__main__":
    main()