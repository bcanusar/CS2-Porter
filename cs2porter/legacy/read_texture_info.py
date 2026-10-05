import os
import re

KEY_LABELS = {
    '$basetexture':  'Base Texture',
    '$basetexture2': 'Base Texture 2',
    '$basetexture3': 'Base Texture 3',
    '$bumpmap':      'Normal Map',
    '$bumpmap2':     'Normal Map 2',
    '$normalmap':    'Normal Map',
    '$normalmap2':   'Normal Map 2',
    '%tooltexture':  'Tool Texture',
}

TEXTURE_KEYS = list(KEY_LABELS.keys())

TEXTURE_EXTENSIONS = ['.tga', '.png', '.jpg', '.bmp', '.psd', '.tif']

VALID_SURFACES = {
    "glass", "plaster", "concrete", "brick", "metal",
    "Wood", "tile", "rock", "sand", "dirt", "grass",
    "water", "snow", "mud", "plastic", "rubber", "carpet",
    "Flesh", "gravel", "cardboard", "foliage",
}

OUTPUT_FILE = 'texture_info.txt'


def parse_vmt(vmt_path):
    shader   = "Unknown"
    textures = {}
    surface  = "default"

    try:
        with open(vmt_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        lines = content.splitlines()
        for line in lines:
            clean_line = line.strip()
            if clean_line and not clean_line.startswith('//') and clean_line != '{':
                shader = clean_line.strip('"')
                break

        for key in TEXTURE_KEYS:
            pattern = re.compile(
                r'"?' + re.escape(key) + r'"?\s+"([^"]+)"',
                re.IGNORECASE
            )
            match = pattern.search(content)
            if match:
                val = match.group(1).replace('\\', '/')
                if not val.lower().startswith('materials/'):
                    val = 'materials/' + val
                textures[key.lower()] = val

        surf_match = re.search(r'"?\$surfaceprop"?\s+"([^"]+)"', content, re.IGNORECASE)
        if surf_match:
            raw_surf = surf_match.group(1).strip()
            matched  = None
            for valid in VALID_SURFACES:
                if valid.lower() == raw_surf.lower():
                    matched = valid
                    break
            surface = matched if matched else "default"

    except Exception as e:
        print(f"Error! Could not read file ({vmt_path}): {e}")

    return shader, textures, surface


def find_texture_file(tex_rel_path, search_root):
    target_dir         = os.path.dirname(tex_rel_path)
    target_base        = os.path.basename(tex_rel_path)
    target_name_no_ext = os.path.splitext(target_base)[0]

    candidate_dir = os.path.join(search_root, target_dir)
    if os.path.isdir(candidate_dir):
        for f in os.listdir(candidate_dir):
            name, ext = os.path.splitext(f)
            if name.lower() == target_name_no_ext.lower() and ext.lower() in TEXTURE_EXTENSIONS:
                return os.path.join(candidate_dir, f)

    for root, dirs, files in os.walk(search_root):
        for file in files:
            name, ext = os.path.splitext(file)
            if name.lower() == target_name_no_ext.lower() and ext.lower() in TEXTURE_EXTENSIONS:
                return os.path.join(root, file)

    return None


def main():
    current_dir = os.getcwd()
    vmt_files   = []

    for root, dirs, files in os.walk(current_dir):
        for file in files:
            if file.lower().endswith('.vmt'):
                vmt_files.append(os.path.join(root, file))

    if not vmt_files:
        print("Warning: No .vmt files found in this directory or its subdirectories.")
        return

    print(f"Found {len(vmt_files)} .vmt files in total. Processing...\n")

    all_entries = []

    for vmt_path in vmt_files:
        rel_path       = os.path.relpath(vmt_path, current_dir).replace('\\', '/')
        vmat_path_str  = os.path.splitext(rel_path)[0] + ".vmat"
        label          = os.path.splitext(os.path.basename(vmt_path))[0]
        target_sub_dir = os.path.dirname(rel_path)

        shader, textures, surface = parse_vmt(vmt_path)

        found_textures = {}
        for key, tex_rel_path in textures.items():
            found_path = find_texture_file(tex_rel_path, current_dir)
            found_textures[key] = found_path

        all_entries.append({
            'label':          label,
            'vmat_path':      vmat_path_str,
            'target_sub_dir': target_sub_dir,
            'shader':         shader,
            'surface':        surface,
            'textures':       textures,
            'found_textures': found_textures,
        })

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as out_f:
        for entry in all_entries:
            out_f.write(f"\"{entry['label']}.vmat\"\n")
            out_f.write("{\n")
            out_f.write(f"   VMat Path = \"{entry['vmat_path']}\"\n")
            out_f.write(f"   Shader = \"{entry['shader']}\"\n")
            out_f.write(f"   Surface Properties = \"{entry['surface']}\"\n")

            for key in TEXTURE_KEYS:
                if key not in entry['textures']:
                    continue

                label_str = KEY_LABELS[key]
                tex_rel   = entry['textures'][key]
                tex_found = entry['found_textures'].get(key)

                if tex_found:
                    real_ext  = os.path.splitext(tex_found)[1]
                    base_no_ext = os.path.splitext(tex_rel)[0]
                    out_path  = base_no_ext + real_ext
                    not_found_comment = ""
                else:
                    out_path          = tex_rel
                    not_found_comment = "  // NOT FOUND"

                out_f.write(f"   {label_str} = \"{out_path}\"{not_found_comment}\n")

            out_f.write("}\n\n")

    print(f"Process complete! Results saved to '{OUTPUT_FILE}' in the current directory.")


if __name__ == "__main__":
    main()
