# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


matplotlib_datas = collect_data_files("matplotlib")
matplotlib_binaries = collect_dynamic_libs("matplotlib")
matplotlib_hiddenimports = collect_submodules(
    "matplotlib",
    filter=lambda name: not name.startswith("matplotlib.tests"),
)


def _collect_sqlite_binary():
    candidates = [
        Path(sys.base_prefix) / "DLLs" / "_sqlite3.pyd",
        Path(sys.prefix) / "DLLs" / "_sqlite3.pyd",
        Path(sys.executable).resolve().parent / "DLLs" / "_sqlite3.pyd",
    ]
    for candidate in candidates:
        if candidate.exists():
            return [(str(candidate), ".")]
    return []

a = Analysis(
    ['report_launcher_gui.py'],
    pathex=[],
    binaries=_collect_sqlite_binary() + matplotlib_binaries,
    datas=[('generate_daily_report.py', '.'), ('auth_repair.py', '.'), ('browser_auth_refresh.py', '.'), ('fenxi_auth_from_har.py', '.'), ('pc_auth_from_har.py', '.'), ('auth_recovery_playwright.py', '.'), ('templates', 'templates'), ('scripts', 'scripts'), ('config.example.yaml', '.'), ('hosts_870.example.yaml', '.'), ('hosts_505.example.yaml', '.'), ('extra_auth.example.json', '.')] + matplotlib_datas,
    hiddenimports=['sqlite3', '_sqlite3', 'playwright.sync_api'] + matplotlib_hiddenimports,
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
    [],
    exclude_binaries=True,
    name='autodatareport-gui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app.ico'],
    version='version_info.txt',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='autodatareport-gui',
)
