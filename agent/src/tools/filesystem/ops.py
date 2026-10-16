"""filesystem.ops — unified file/directory operations for Boom.

Merged from two legacy modules (deduped):
- agent/src/tools/file_ops.py  (read_file, write_file, fuzzy patch_file, search_files)
- agent/src/tools/fileops.py    (strict edit_file, append_file, file_info)

Merge decisions:
- Keep ONE safety mechanism: every path is resolved and must stay inside the
  current working directory (blocks path traversal).
- Drop fuzzy patch_file (risky "close enough" edits, no backup) — keep only
  strict edit(): exactly 1 match required, always backs up to <path>.bak.
- Drop search_files (content search is the research tool's job, not fs core).
- Normalize: every function returns {"ok": True, ...} or {"ok": False, "error": ...},
  never raising exceptions outward.

Stdlib only.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_CHUNK = 1024  # leading bytes of a file used for binary detection
_MAX_READ_LINES = 10000  # read() limit cap


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _resolve_safe(path: str) -> tuple[Optional[Path], Optional[str]]:
    """Resolve *path* and verify it stays inside the current working directory.

    Returns (resolved_path, None) on success, otherwise (None, error_message).
    """
    if not isinstance(path, str) or not path.strip():
        return None, "path must be a non-empty string"
    try:
        root = Path.cwd().resolve()
        target = (root / path).resolve()
    except (OSError, ValueError) as exc:  # path could not be resolved
        return None, f"invalid path: {exc}"
    try:
        target.relative_to(root)
    except ValueError:
        return None, "path traversal blocked: path is outside the working directory"
    return target, None


def _is_binary(path: Path) -> bool:
    """True if the first 1KB of the file contains a null byte (binary hint)."""
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(_CHUNK)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def read(path: str, offset: int = 1, limit: int = 2000) -> Dict[str, Any]:
    """Read a text file page by page, with line numbers.

    Description:
        Reads *limit* lines starting at line *offset* (1-based). Each returned
        line is prefixed with "<line number>|" so the agent can reference it
        when editing.
    Args:
        path: file path (relative to cwd).
        offset: starting line (1-based), must be >= 1.
        limit: max lines, 1..10000.
    Returns:
        {"ok": True, "content": str, "total_lines": int, "offset": int, "limit": int}
        or {"ok": False, "error": str}.
    Raises:
        Never raises — all errors are returned as dicts.
    """
    target, err = _resolve_safe(path)
    if err:
        return {"ok": False, "error": err}
    assert target is not None
    if not isinstance(offset, int) or offset < 1:
        return {"ok": False, "error": "offset must be an integer >= 1"}
    if not isinstance(limit, int) or not 1 <= limit <= _MAX_READ_LINES:
        return {"ok": False, "error": f"limit must be within 1..{_MAX_READ_LINES}"}
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": f"file not found: {path}"}
    if _is_binary(target):
        return {"ok": False, "error": "binary file refused"}
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return {"ok": False, "error": f"could not read file: {exc}"}
    total = len(lines)
    start = offset - 1
    selected = lines[start:start + limit]
    formatted = "\n".join(f"{start + i + 1}|{line}" for i, line in enumerate(selected))
    return {
        "ok": True,
        "content": formatted,
        "total_lines": total,
        "offset": offset,
        "limit": limit,
    }


def write(path: str, content: str) -> Dict[str, Any]:
    """Overwrite (or create) a text file.

    Description:
        Writes the full *content* to the file. Creates parent directories
        if missing.
    Args:
        path: file path (relative to cwd).
        content: text content to write.
    Returns:
        {"ok": True, "path": str, "bytes_written": int}
        or {"ok": False, "error": str}.
    Raises:
        Never raises — all errors are returned as dicts.
    """
    target, err = _resolve_safe(path)
    if err:
        return {"ok": False, "error": err}
    assert target is not None
    if not isinstance(content, str):
        return {"ok": False, "error": "content must be a string"}
    if target.exists() and not target.is_file():
        return {"ok": False, "error": "path is a directory, cannot overwrite"}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"could not write file: {exc}"}
    return {"ok": True, "path": str(target),
            "bytes_written": len(content.encode("utf-8"))}


def edit(path: str, old: str, new: str) -> Dict[str, Any]:
    """Edit a file: replace exactly one absolute-match occurrence.

    Description:
        Finds the *old* string in the file. Edits only when there is exactly
        1 match:
        - 0 matches -> "not found" error
        - >1 match  -> "ambiguous" error, asking for a more specific string
        Backs up the original to <path>.bak before writing.
    Args:
        path: file path.
        old: string to replace (must be non-empty, exact match).
        new: replacement string.
    Returns:
        {"ok": True, "path": str, "backup": str}
        or {"ok": False, "error": str}.
    Raises:
        Never raises — all errors are returned as dicts.
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
    if not isinstance(new, str):
        return {"ok": False, "error": "new must be a string"}
    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"ok": False, "error": f"could not read file: {exc}"}
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
        return {"ok": False, "error": f"could not write file: {exc}"}
    return {"ok": True, "path": str(target), "backup": str(backup)}


def append(path: str, text: str, newline: bool = True) -> Dict[str, Any]:
    """Append text to the end of a file (creates it if missing).

    Description:
        Appends *text* to the end of the file. If newline=True and the file
        does not already end with a newline, one is inserted first.
    Args:
        path: file path.
        text: text to append.
        newline: insert a newline before appending if needed.
    Returns:
        {"ok": True, "path": str, "bytes_added": int}
        or {"ok": False, "error": str}.
    Raises:
        Never raises — all errors are returned as dicts.
    """
    target, err = _resolve_safe(path)
    if err:
        return {"ok": False, "error": err}
    assert target is not None
    if not isinstance(text, str):
        return {"ok": False, "error": "text must be a string"}
    if target.exists() and not target.is_file():
        return {"ok": False, "error": "path is a directory"}
    if target.is_file() and _is_binary(target):
        return {"ok": False, "error": "binary file refused"}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = text
        if newline and target.exists() and target.stat().st_size > 0:
            with open(target, "rb") as fh:
                fh.seek(-1, 2)
                if fh.read(1) not in (b"\n", b"\r"):
                    payload = "\n" + text
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError as exc:
        return {"ok": False, "error": f"could not append: {exc}"}
    return {"ok": True, "path": str(target),
            "bytes_added": len(payload.encode("utf-8"))}


def delete(path: str) -> Dict[str, Any]:
    """Delete a file or an EMPTY directory.

    Description:
        Deletes regular files. Directories are only deleted when empty.
        Refuses: symlinks, subdirectories of cwd, non-empty directories,
        and cwd itself.
    Args:
        path: path to delete.
    Returns:
        {"ok": True, "path": str} or {"ok": False, "error": str}.
    Raises:
        Never raises — all errors are returned as dicts.
    """
    target, err = _resolve_safe(path)
    if err:
        return {"ok": False, "error": err}
    assert target is not None
    root = Path.cwd().resolve()
    if target == root:
        return {"ok": False, "error": "refusing to delete the current working directory"}
    if not target.exists() and not target.is_symlink():
        return {"ok": False, "error": f"not found: {path}"}
    if target.is_symlink():
        return {"ok": False, "error": "refusing to delete symlink (may point outside)"}
    try:
        if target.is_dir():
            if any(target.iterdir()):
                return {"ok": False, "error": "refusing to delete non-empty directory"}
            target.rmdir()
        else:
            target.unlink()
    except OSError as exc:
        return {"ok": False, "error": f"could not delete: {exc}"}
    return {"ok": True, "path": str(target)}


def list_dir(path: str = ".", recursive: bool = False,
             limit: int = 200) -> Dict[str, Any]:
    """List directory contents.

    Description:
        Lists files/subdirectories (sorted by name). Supports recursive
        listing.
    Args:
        path: directory to list (default ".").
        recursive: True for recursive listing.
        limit: max entries (1..5000).
    Returns:
        {"ok": True, "path": str, "entries": [{"name","type","size"}], "truncated": bool}
        or {"ok": False, "error": str}.
    Raises:
        Never raises — all errors are returned as dicts.
    """
    target, err = _resolve_safe(path)
    if err:
        return {"ok": False, "error": err}
    assert target is not None
    if not target.exists():
        return {"ok": False, "error": f"not found: {path}"}
    if not target.is_dir():
        return {"ok": False, "error": f"not a directory: {path}"}
    if not isinstance(limit, int) or not 1 <= limit <= 5000:
        return {"ok": False, "error": "limit must be within 1..5000"}
    try:
        items = target.rglob("*") if recursive else target.iterdir()
        entries: List[Dict[str, Any]] = []
        truncated = False
        for p in sorted(items, key=lambda x: x.name):
            if len(entries) >= limit:
                truncated = True
                break
            try:
                is_dir = p.is_dir()
                entries.append({
                    "name": str(p.relative_to(target)),
                    "type": "dir" if is_dir else ("file" if p.is_file() else "other"),
                    "size": 0 if is_dir else p.stat().st_size,
                })
            except OSError:
                continue
    except OSError as exc:
        return {"ok": False, "error": f"could not list directory: {exc}"}
    return {"ok": True, "path": str(target), "entries": entries,
            "truncated": truncated}


def info(path: str) -> Dict[str, Any]:
    """Get path metadata (exists, size, mtime, line count...).

    Description:
        A compact stat equivalent: whether it exists, size (bytes),
        mtime (ISO 8601 UTC), line count (text files), whether it's a
        directory.
    Args:
        path: path to inspect.
    Returns:
        {"ok": True, "exists": bool, "size": int, "mtime": str,
         "lines": int, "is_dir": bool}
        or {"ok": False, "error": str} for an invalid path.
        A nonexistent path -> {"ok": True, "exists": False}.
    Raises:
        Never raises — all errors are returned as dicts.
    """
    target, err = _resolve_safe(path)
    if err:
        return {"ok": False, "error": err}
    assert target is not None
    if not target.exists():
        return {"ok": True, "exists": False}
    is_dir = target.is_dir()
    try:
        stat = target.stat()
    except OSError as exc:
        return {"ok": False, "error": f"could not stat: {exc}"}
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    lines = 0
    if not is_dir and not _is_binary(target):
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as fh:
                lines = sum(1 for _ in fh)
        except OSError:
            lines = 0
    return {
        "ok": True,
        "exists": True,
        "path": str(target),
        "size": stat.st_size,
        "mtime": mtime,
        "lines": lines,
        "is_dir": is_dir,
    }


__all__ = ["read", "write", "edit", "append", "delete", "list_dir", "info"]
