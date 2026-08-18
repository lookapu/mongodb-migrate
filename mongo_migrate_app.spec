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
    [],
    exclude_binaries=True,
    name="MongoDB Migrate",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file="macos.entitlements",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="MongoDB Migrate",
)
app = BUNDLE(
    coll,
    name="MongoDB Migrate.app",
    icon=None,
    bundle_identifier="com.mavis.mongodbmigrate",
    info_plist={
        "CFBundleDisplayName": "MongoDB Migrate",
        "CFBundleShortVersionString": "2.0.0",
        "CFBundleVersion": "2.0.0",
        "NSHighResolutionCapable": True,
    },
)
