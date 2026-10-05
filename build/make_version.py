"""Writes build/version_info.txt (the .exe file properties) from cs2porter.__version__."""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

with open(os.path.join(ROOT, "cs2porter", "__init__.py"), encoding="utf-8") as f:
    version = re.search(r'__version__\s*=\s*"([^"]+)"', f.read()).group(1)
nums = [int(n) for n in re.findall(r"\d+", version)][:4]
nums += [0] * (4 - len(nums))
tup = ", ".join(str(n) for n in nums)

TEXT = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({tup}),
    prodvers=({tup}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'tehlikeli91'),
        StringStruct('FileDescription', 'CS2 Porter - ports CSS / CS:GO maps to CS2'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', 'CS2 Porter'),
        StringStruct('LegalCopyright', 'Made by tehlikeli91, MIT License'),
        StringStruct('OriginalFilename', 'CS2 Porter.exe'),
        StringStruct('ProductName', 'CS2 Porter'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""

with open(os.path.join(HERE, "version_info.txt"), "w", encoding="utf-8") as f:
    f.write(TEXT)
print(version)
