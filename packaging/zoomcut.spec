# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build: a self-contained folder per platform.

    pyinstaller packaging/zoomcut.spec --noconfirm

Deliberately a one-FOLDER build, not one-file. A one-file bundle unpacks
itself and re-links every extension module on each launch, which costs about
ten seconds before the app answers - unacceptable for something you start to
record a quick demo. The folder build starts in about a second.
"""
import os, sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
DOCS = os.path.join(ROOT, "docs")

icon = None
if sys.platform == "darwin" and os.path.exists(os.path.join(DOCS, "zoomcut.icns")):
    icon = os.path.join(DOCS, "zoomcut.icns")
elif os.name == "nt" and os.path.exists(os.path.join(DOCS, "zoomcut.ico")):
    icon = os.path.join(DOCS, "zoomcut.ico")

a = Analysis(
    [os.path.join(ROOT, "packaging", "launcher.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "zoomcut", "web", "index.html"), "zoomcut/web")],
    hiddenimports=["zoomcut", "zoomcut.cli", "zoomcut.server", "zoomcut.recorder",
                   "zoomcut.winlist", "zoomcut.wallpapers", "zoomcut.render",
                   "zoomcut.analyze", "zoomcut.director", "zoomcut.project"],
    hookspath=[], runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "setuptools", "pip"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="zoomcut",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True,                 # it prints the URL it is serving on
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
    icon=icon,
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False, upx_exclude=[],
    name="zoomcut",
)
