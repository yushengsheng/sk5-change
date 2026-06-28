# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

playwright_datas, playwright_binaries, playwright_hiddenimports = collect_all("playwright")


def collect_tree(source: str | os.PathLike[str], dest: str) -> list[tuple[str, str]]:
    source_path = Path(source)
    items: list[tuple[str, str]] = []
    for path in source_path.rglob("*"):
        if path.is_file():
            relative_parent = path.parent.relative_to(source_path)
            target_dir = str(Path(dest) / relative_parent)
            items.append((str(path), target_dir))
    return items


browser_cache = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
if not browser_cache.exists():
    raise SystemExit(f"Playwright browser cache was not found: {browser_cache}")

browser_datas = collect_tree(browser_cache, "ms-playwright")


a = Analysis(
    ["ip_exchange_gui.py"],
    pathex=[],
    binaries=playwright_binaries,
    datas=playwright_datas + browser_datas,
    hiddenimports=playwright_hiddenimports,
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
    name="sk5-change-full",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
