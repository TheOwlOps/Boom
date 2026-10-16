# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Boom Python backend (Tauri sidecar).

Entry point: ``gateway/api.py`` — a stdlib-only HTTP server. All agent code
is imported lazily *inside functions* (``build_real_api``, the ``_handle_*``
route handlers), so PyInstaller's static analysis cannot see those imports;
they are declared here via ``collect_submodules`` over every package under
``agent/src`` plus the ``agent`` / ``gateway`` roots.

Result: one-file, windowed (``console=False``) ``boom-backend`` binary.
Tauri launches it as a sidecar; the port comes from the ``BOOM_API_PORT``
environment variable (default 18080), there is no ``--port`` CLI flag.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# ------------------------------------------------------------------ paths
# SPECPATH is this spec file's directory (gateway/).
REPO = Path(SPECPATH).resolve().parent          # repo root
SRC = REPO / "agent" / "src"                    # agent package tree

# The repo runs with both roots on sys.path (see gateway/api.py lines 34-37);
# the spec needs the same layout so collect_submodules() can resolve every
# import spelling used at runtime (``agent.src.cli`` AND bare ``tools.*``).
for _p in (str(REPO), str(SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# --------------------------------------------------------- hidden imports
# Every package dir under agent/src is importable as a top-level package
# (tools.*, providers.*, audit.*, ...) because agent/src sits on sys.path;
# the ``agent`` root additionally covers the ``agent.src.*`` spelling used by
# gateway/api.py's lazy imports. ``gateway`` covers gateway.daemon and
# gateway.broadcast (pulled in by cli.py / tool groups).
_package_roots = ["agent", "gateway"] + sorted(
    p.name for p in SRC.iterdir()
    if p.is_dir() and (p / "__init__.py").exists()
)

hiddenimports: list = []
for _root in _package_roots:
    try:
        hiddenimports += collect_submodules(_root)
    except Exception as e:  # pragma: no cover - defensive only
        print(f"[spec] collect_submodules({_root!r}) skipped: {e}")

# Single-file modules directly under agent/src (cli, config, environment,
# marketplace, tool_groups, version, ...) are not packages, so they need
# explicit entries.
hiddenimports += sorted(
    p.stem for p in SRC.glob("*.py") if p.name != "__init__.py"
)

# Third-party deps pulled in by tool modules (stdlib needs no entry).
hiddenimports += ["yaml"]

# ------------------------------------------------------------------- data
# skills/*/SKILL.md are read from disk at runtime (extensibility.skill_loader
# and GET /api/skills). Bundle them at the archive root so the
# __file__-relative lookups (repo_root / "skills") keep working inside the
# frozen bundle (repo_root resolves to sys._MEIPASS there).
datas: list = []
_skills = REPO / "skills"
if _skills.is_dir():
    datas.append((str(_skills), "skills"))

block_cipher = None

a = Analysis(
    [str(REPO / "gateway" / "api.py")],
    pathex=[str(REPO), str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(REPO / "gateway" / "pyi_runtime_hook.py")],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="boom-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed: Tauri sidecar, no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
