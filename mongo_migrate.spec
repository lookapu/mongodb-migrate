# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.compat import is_win

a = Analysis(
    ["mongo_migrate.py"],
    pathex=[],
    binaries=[],
    datas=[("LICENSE", ".")],
    hiddenimports=["dns", "bson", "pymongo"],
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
    name="mongodb-migrate",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    version="windows_version_info.txt" if is_win else None,
)
