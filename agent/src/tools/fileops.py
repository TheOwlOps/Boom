"""fileops.py — supplemental file operations for Boom (stdlib only).

This module does NOT replace ``agent/src/tools/file_ops.py``; it adds three
functions that the existing tools lack:

- ``edit_file`` — strict exact-match single edit with auto-backup
  (``patch_file`` in file_ops.py is *fuzzy*: it tries whitespace-normalized
  matching and can silently pick a near-match; it also has no backup).
- ``append_file`` — append text to a file (creates it if missing).
- ``file_info`` — stat-like metadata for a path.

Safety rules (shared by all functions):
- Path traversal: every path is resolved with ``Path.resolve()`` and must
  stay inside the current working directory. Anything escaping it is rejected
  with ``{"ok": False, "error": "path traversal blocked"}``.
- Binary files: ``edit_file`` and ``append_file`` refuse files whose first
  1 KB contains a null byte (``{"ok": False, "error": "binary file refused"}``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any
import shutil

_CHUNK = 1024  # bytes inspected for null-byte binary detection


def _resolve_safe(path: str) -> tuple[Path | None, str | None]:
    """Resolve *path* and verify it stays inside the cwd.

    Returns (resolved_path, None) on success, or (None, error_message).
    """
    try:
        root = Path.cwd().resolve()
        target = (root / path).resolve()
    except (OSError, ValueError) as exc:  # e.g. unresolvable path
        return None, f"invalid path: {exc}"
    try:
        target.relative_to(root)
    except ValueError:
        return None, "path traversal blocked"
    return target, None


def _is_binary(path: Path) -> bool:
    """True if the file's first 1 KB contains a null byte."""
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(_CHUNK)
    except OSError:
        return False


def edit_file(path: str, old: str, new: str) -> Dict[str, Any]:
    """Replace exactly ONE occurrence of ``old`` with ``new`` in the file.

    - 0 matches -> ``{"ok": False, "error": "not found"}``
    - >1 match  -> ``{"ok": False, "error": "ambiguous: N matches, be more specific"}``
    - exactly 1 -> writes the file, after backing the original up to
      ``<path>.bak``. Returns ``{"ok": True, "path": ...}``.
    """
    target, err = _resolve_safe(path)
    if err:
        return {"ok": False, "error": err}
    assert target is not None

    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "not found"}
    if _is_binary(target):
        return {"ok": False, "error": "binary file refused"}
    if not old:
        return {"ok": False, "error": "old must be non-empty"}

    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"ok": False, "error": f"cannot read file: {exc}"}

    count = content.count(old)
    if count == 0:
        return {"ok": False, "error": "not found"}
    if count > 1:
        return {"ok": False, "error": f"ambiguous: {count} matches, be more specific"}

    backup = target.with_name(target.name + ".bak")
    try:
        shutil.copy2(target, backup)
        target.write_text(content.replace(old, new, 1), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"cannot write file: {exc}"}

    return {"ok": True, "path": str(target), "backup": str(backup)}


def append_file(path: str, text: str, newline: bool = True) -> Dict[str, Any]:
    """Append ``text`` to the end of the file; create the file if missing.

    If ``newline`` is true, text is appended starting on a new line unless
    the file already ends with one. Returns ``{"ok": True, "path": ...,
    "bytes_added": N}``.
    """
    target, err = _resolve_safe(path)
    if err:
        return {"ok": False, "error": err}
    assert target is not None

    if target.exists() and target.is_file() and _is_binary(target):
        return {"ok": False, "error": "binary file refused"}
    if target.exists() and not target.is_file():
        return {"ok": False, "error": "path is a directory"}

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = text
        if newline and target.exists() and target.stat().st_size > 0:
            with open(target, "rb") as fh:
                fh.seek(-1, 2)
                last = fh.read(1)
            if last not in (b"\n", b"\r"):
                payload = "\n" + text
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError as exc:
        return {"ok": False, "error": f"cannot append: {exc}"}

    return {
        "ok": True,
        "path": str(target),
        "bytes_added": len(payload.encode("utf-8")),
    }


def file_info(path: str) -> Dict[str, Any]:
    """Return metadata: exists, size (bytes), mtime (ISO 8601 UTC), lines, is_dir.

    If the path does not exist, returns just ``{"exists": False}``.
    """
    target, err = _resolve_safe(path)
    if err:
        return {"exists": False, "error": err}
    assert target is not None

    if not target.exists():
        return {"exists": False}

    is_dir = target.is_dir()
    try:
        stat = target.stat()
    except OSError as exc:
        return {"exists": False, "error": f"cannot stat: {exc}"}

    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    lines = 0
    if not is_dir and not _is_binary(target):
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as fh:
                lines = sum(1 for _ in fh)
        except OSError:
            lines = 0

    return {
        "exists": True,
        "size": stat.st_size,
        "mtime": mtime,
        "lines": lines,
        "is_dir": is_dir,
    }
