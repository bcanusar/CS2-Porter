# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec of CS2 Porter.exe (one file, no console). Run build.bat in the project folder.
import os

ROOT = os.path.dirname(SPECPATH)
PKG = os.path.join(ROOT, "cs2porter")

a = Analysis(
    [os.path.join(ROOT, "CS2 Porter.pyw")],
    pathex=[ROOT],
    binaries=[],
    datas=[(os.path.join(PKG, "lang"), "cs2porter/lang"), (os.path.join(PKG, "assets"), "cs2porter/assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CS2 Porter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(PKG, "assets", "icon.ico")],
    version=os.path.join(SPECPATH, "version_info.txt"),
)
