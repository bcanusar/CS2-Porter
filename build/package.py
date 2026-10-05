"""Makes release/CS2-Porter-<version>.zip from build/dist/CS2 Porter.exe: the program, the
README, the licenses and the third party tools from the tools folder (BSPSource, VTFCmd), as
they are. --no-tools leaves the tools out."""
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = os.path.join(ROOT, "tools")
VTFCMD_FILES = ("VTFCmd.exe", "VTFLib.dll", "DevIL.dll")

with open(os.path.join(ROOT, "cs2porter", "__init__.py"), encoding="utf-8") as f:
    version = re.search(r'__version__\s*=\s*"([^"]+)"', f.read()).group(1)

exe = os.path.join(HERE, "dist", "CS2 Porter.exe")
if not os.path.isfile(exe):
    raise SystemExit(f"missing {exe}, build it first")

with_tools = "--no-tools" not in sys.argv[1:]
bspsrc = os.path.join(TOOLS, "BSPSource")
if with_tools:
    if not (os.path.isfile(os.path.join(bspsrc, "bin", "java.exe"))
            and os.path.isfile(os.path.join(bspsrc, "lib", "modules"))):
        raise SystemExit("tools/BSPSource is missing or not the Windows release of BSPSource "
                         "(see tools/README.md), or run with --no-tools")
    missing = [n for n in VTFCMD_FILES if not os.path.isfile(os.path.join(TOOLS, "VTFCmd", n))]
    if missing:
        raise SystemExit(f"tools/VTFCmd is missing {', '.join(missing)} (see tools/README.md)")

out_dir = os.path.join(ROOT, "release")
os.makedirs(out_dir, exist_ok=True)
out = os.path.join(out_dir, f"CS2-Porter-{version}.zip")
top = "CS2 Porter"
files = [
    (exe, "CS2 Porter.exe"),
    (os.path.join(ROOT, "README.md"), "README.md"),
    (os.path.join(ROOT, "LICENSE"), "LICENSE.txt"),
    (os.path.join(ROOT, "THIRD_PARTY_NOTICES.md"), "THIRD_PARTY_NOTICES.md"),
    (os.path.join(TOOLS, "README.md"), "tools/README.md"),
]
lic = os.path.join(ROOT, "licenses")
files += [(os.path.join(lic, n), f"licenses/{n}") for n in sorted(os.listdir(lic))]
if with_tools:
    for d, _dirs, names in os.walk(bspsrc):
        for n in names:
            p = os.path.join(d, n)
            files.append((p, "tools/BSPSource/" + os.path.relpath(p, bspsrc).replace("\\", "/")))
    files += [(os.path.join(TOOLS, "VTFCmd", n), f"tools/VTFCmd/{n}") for n in VTFCMD_FILES]

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for src, arc in files:
        z.write(src, f"{top}/{arc}")
    if not with_tools:
        # the folders the user extracts the tools into
        z.writestr(f"{top}/tools/BSPSource/", "")
        z.writestr(f"{top}/tools/VTFCmd/", "")
print(out, f"({len(files)} files)")
