# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Boom Zalo Personal bridge.

Builds a fully standalone one-file executable (`zalo-bridge` /
`zalo-bridge.exe`) that runs on a clean machine with no Python installed.

Run from this directory:
    pyinstaller bridge.spec
"""

import glob
import os
import subprocess

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None


def _find_zbar():
    """Locate the native libzbar shared library for bundling (best-effort).

    pyzbar is a ctypes wrapper: on Linux/macOS it resolves libzbar via
    ``ctypes.util.find_library`` at runtime, which fails on machines without
    the system zbar package. Bundling the .so/.dylib plus the
    ``pyi_rth_zbar.py`` runtime hook makes QR-image decoding work everywhere.
    Returns the absolute path, or None when unavailable (the bridge then
    answers /qr-lookup-image with 501 instead of crashing).
    """
    import ctypes.util

    name = ctypes.util.find_library("zbar")
    if name and os.path.isabs(name) and os.path.exists(name):
        return name
    # find_library often returns a bare soname; resolve it via ldconfig.
    try:
        out = subprocess.run(
            ["ldconfig", "-p"], capture_output=True, text=True, timeout=10
        ).stdout
        for line in out.splitlines():
            if "libzbar.so" in line and "=>" in line:
                path = line.split("=>", 1)[1].strip()
                if os.path.exists(path):
                    return path
    except Exception:
        pass
    # macOS Homebrew fallbacks.
    for path in ("/opt/homebrew/lib/libzbar.dylib",
                 "/usr/local/lib/libzbar.dylib"):
        if os.path.exists(path):
            return path
    # Last resort: common filesystem locations.
    for pattern in ("/usr/lib/x86_64-linux-gnu/libzbar.so*",
                    "/usr/lib64/libzbar.so*",
                    "/usr/lib/libzbar.so*"):
        for path in sorted(glob.glob(pattern)):
            if os.path.isfile(path) and not os.path.islink(path):
                return path
    return None


_zbar_path = _find_zbar()
_binaries = [(_zbar_path, ".")] if _zbar_path else []

a = Analysis(
    ["bridge.py"],
    pathex=[],
    binaries=_binaries,
    datas=[],
    # bridge.py imports zlapi / pyzbar / PIL lazily inside functions;
    # PyInstaller's AST scan usually finds them, but list them explicitly
    # so a missed edge never ships a broken binary.
    # NOTE: zlapi loads its ~180 method modules via importlib.import_module
    # at runtime (method/__init__.py: attach_methods) — invisible to static
    # analysis — so collect the whole package tree as hidden imports.
    hiddenimports=collect_submodules("zlapi")
    + [
        "pyzbar",
        "pyzbar.pyzbar",
        "pyzbar.zbar_library",
        "PIL",
        "PIL.Image",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["rthooks/pyi_rth_zbar.py"],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="zalo-bridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Keep the console: the bridge logs to stdout/stderr, and the Tauri
    # backend spawns it with CREATE_NO_WINDOW on Windows anyway.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
