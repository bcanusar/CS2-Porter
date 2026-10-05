import os
import shutil

TARGET_LIST = [
    "foliage",
    "foliage_basic",
    "foliage_complex",
    "static",
    "static_basic",
    "static_complex",
    "materials",
    "models"
]


def find_existing(folders, base_dir):
    existing_paths = []
    for folder in folders:
        target_path = os.path.join(base_dir, folder)
        if os.path.exists(target_path):
            existing_paths.append((target_path, folder))
    return existing_paths


def clear_target_folders(folders, base_dir=None):
    # CS2 Porter: input() removed because the confirmation is asked in the UI.
    script_dir = os.path.abspath(base_dir) if base_dir else os.path.dirname(os.path.abspath(__file__))

    existing_paths = find_existing(folders, script_dir)

    if not existing_paths:
        print("Target folders were not found.")
        return

    print("Found folders to clear:")
    for _, folder in existing_paths:
        print(f"  - {folder}")

    for target_dir, folder in existing_paths:
        for item in os.listdir(target_dir):
            item_path = os.path.join(target_dir, item)
            try:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.unlink(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            except Exception as e:
                print(f"Failed to delete {item_path}. Error: {e}")
        print(f"Successfully cleared contents of '{folder}'.")

if __name__ == "__main__":
    clear_target_folders(TARGET_LIST)
