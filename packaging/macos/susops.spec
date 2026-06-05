# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all

# PyInstaller 6 resolves Analysis() paths relative to the spec's directory,
# not the cwd. Anchor everything to SPECPATH so the spec keeps working
# regardless of where pyinstaller is invoked from.
SPEC_DIR = os.path.abspath(SPECPATH)
REPO_ROOT = os.path.abspath(os.path.join(SPEC_DIR, "..", ".."))
ASSETS = os.path.join(REPO_ROOT, "src", "susops", "assets")
# .icns is a PyInstaller build asset (not used at runtime), kept next to
# the spec and committed so CI doesn't need to regenerate it on each run.
ICNS = os.path.join(SPEC_DIR, "susops.icns")

import sys
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
from susops import __version__ as SUSOPS_VERSION

rumps_datas, rumps_binaries, rumps_hiddenimports = collect_all("rumps")

a = Analysis(
    [os.path.join(SPEC_DIR, "entry_tray.py")],
    pathex=[REPO_ROOT],
    binaries=rumps_binaries,
    # Bundle the package's assets at susops/assets/ inside the app so the
    # runtime Path(__file__).parent.parent / "assets" resolution lands them.
    datas=rumps_datas + [(ASSETS, "susops/assets")],
    hiddenimports=rumps_hiddenimports + [
        "objc",
        "Foundation",
        "AppKit",
        "Cocoa",
        "susops",
        "susops.client",
        "susops.tray",
        "susops.tray.mac",
        "susops.tray.base",
        "susops.facade",
        "susops.core.config",
        "susops.core.ssh",
        "susops.core.ports",
        "susops.core.types",
        "susops.core.process",
        # Daemon path — the bundle re-execs itself with -m
        # susops.core.services_daemon to spawn the daemon, so the
        # daemon module + everything it pulls in must be bundled.
        "susops.core.services_daemon",
        "susops.core.rpc_server",
        "susops.core.rpc_protocol",
        "susops.core.status",
        "susops.core.pac",
        "susops.core.share",
        "susops.core.socat",
        "susops.core.ssh_config",
        "susops.core.browsers",
        "aiohttp",
        "cryptography",
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
    icon=ICNS,
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
    icon=ICNS,
    bundle_identifier="net.odt.susops",
    info_plist={
        "CFBundleShortVersionString": SUSOPS_VERSION,
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    },
)
