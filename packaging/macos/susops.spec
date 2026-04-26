# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

rumps_datas, rumps_binaries, rumps_hiddenimports = collect_all("rumps")

a = Analysis(
    ["packaging/macos/entry_tray.py"],
    pathex=["."],
    binaries=rumps_binaries,
    datas=rumps_datas,
    hiddenimports=rumps_hiddenimports + [
        "objc",
        "Foundation",
        "AppKit",
        "Cocoa",
        "susops.tray",
        "susops.tray.mac",
        "susops.tray.base",
        "susops.facade",
        "susops.core.config",
        "susops.core.ssh",
        "susops.core.ports",
        "susops.core.types",
        "susops.core.process",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["textual", "textual_plotext", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SusOps",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=True,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/susops.icns",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SusOps",
)

app = BUNDLE(
    coll,
    name="SusOps.app",
    icon="assets/susops.icns",
    bundle_identifier="net.odt.susops",
    info_plist={
        "CFBundleShortVersionString": "3.0.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    },
)
