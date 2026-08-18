# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["mongo_migrate_gui.py"],
    pathex=[],
    binaries=[],
    datas=[("LICENSE", ".")],
    hiddenimports=["dns", "bson", "pymongo", "tkinter", "tkinter.ttk"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MongoDB-Migrate-GUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=None,
    version="windows_version_info.txt",
)
