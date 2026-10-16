"""Python wrapper for the boom-search Rust binary.

Destination in repo: agent/src/rust_bridge/search.py

Attempts to call the compiled Rust binary for fast parallel regex search.
Falls back to a pure-Python implementation (os.walk + re) when the binary
is missing or fails. Never raises because of binary problems.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

# Directories skipped during the pure-Python fallback walk (mirrors Rust).
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "target",
    "dist", "build", ".venv", "venv",
}

_TIMEOUT_S = 60


def _find_binary() -> Path | None:
    """Locate the boom-search binary.

    Priority: BOOM_SEARCH_BINARY env override -> $CARGO_TARGET_DIR/release ->
    repo crates/perf/search/target/release.
    """
    override = os.environ.get("BOOM_SEARCH_BINARY")
    if override:
        p = Path(override)
        return p if p.is_file() else None

    target_dir = os.environ.get("CARGO_TARGET_DIR")
    if target_dir:
        p = Path(target_dir) / "release" / "boom-search"
        if p.is_file():
            return p

    here = Path(__file__).resolve()
    # search.py will live at agent/src/rust_bridge/search.py, so parents[4]
    # is the repo root.
    try:
        repo_root = here.parent.parent.parent.parent
    except IndexError:  # very shallow path, cannot resolve repo root
        return None
    # Workspace builds (cargo build -p from repo root) place binaries in the
    # workspace-level target dir; standalone builds use the per-crate dir.
    for cand in (
        repo_root / "target" / "release" / "boom-search",
        repo_root / "crates" / "perf" / "search" / "target" / "release" / "boom-search",
    ):
        if cand.is_file():
            return cand
    return None


def _glob_to_regex(file_pattern: str) -> re.Pattern:
    """Convert a simple glob (*, ?, and one-level {a,b}) to a regex.

    Matched against the file name (basename), like the Rust side.
    """
    # One-level brace expansion: *.{py,rs} -> (*.py|*.rs)
    def expand(pat: str) -> list[str]:
        m = re.search(r"\{([^{}]*,[^{}]*)\}", pat)
        if not m:
            return [pat]
        inner = m.group(1)
        prefix, suffix = pat[: m.start()], pat[m.end():]
        return [prefix + alt + suffix for alt in inner.split(",")]

    alternatives = []
    for pat in expand(file_pattern or "*"):
        buf = []
        for ch in pat:
            if ch == "*":
                buf.append(".*")
            elif ch == "?":
                buf.append(".")
            else:
                buf.append(re.escape(ch))
        alternatives.append("".join(buf))
    return re.compile("^(?:" + "|".join(alternatives) + ")$")


def _fallback_search(
    root: str, pattern: str, file_pattern: str = "*.py", max_results: int = 50
) -> list[dict]:
    """Pure-Python search used when the Rust binary is unavailable."""
    try:
        rx = re.compile(pattern)
    except re.error:
        return []
    root_path = Path(root)
    if not root_path.is_dir():
        return []

    name_rx = _glob_to_regex(file_pattern)
    hits: list[dict] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root_path):
        # Prune skipped dirs in place so os.walk does not descend into them.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if not name_rx.match(fname):
                continue
            fpath = Path(dirpath) / fname
            try:
                with open(fpath, "rb") as fh:
                    head = fh.read(8192)
                    if b"\x00" in head:
                        continue  # binary file
                    fh.seek(0)
                    raw = fh.read()
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # unreadable or non-UTF8
            scanned += 1
            for lineno, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    hits.append(
                        {
                            "file": str(fpath.resolve()),
                            "line": lineno,
                            "text": line.strip(),
                        }
                    )
    hits.sort(key=lambda h: (h["file"], h["line"]))
    return hits[:max_results]


def rust_search(
    root: str, pattern: str, file_pattern: str = "*.py", max_results: int = 50
) -> list[dict]:
    """Search files under root for regex matches.

    Returns a list of {"file", "line", "text"} dicts, sorted by file then
    line, truncated to max_results. Returns [] on any error (never raises
    because of binary problems).
    """
    # Validate the regex early so both paths behave the same on bad patterns.
    try:
        re.compile(pattern)
    except re.error:
        return []

    binary = _find_binary()
    if binary is None:
        return _fallback_search(root, pattern, file_pattern, max_results)

    payload = json.dumps(
        {
            "root": root,
            "pattern": pattern,
            "file_pattern": file_pattern,
            "max_results": max_results,
        }
    )
    try:
        proc = subprocess.run(
            [str(binary)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
        data = json.loads(proc.stdout)
    except Exception:
        return _fallback_search(root, pattern, file_pattern, max_results)

    if not isinstance(data, dict) or data.get("error"):
        # Binary reported an error (bad regex / missing root / bad input):
        # contract says return [] without raising.
        return _fallback_search(root, pattern, file_pattern, max_results)

    results = data.get("results", [])
    if not isinstance(results, list):
        return []
    results.sort(key=lambda h: (h.get("file", ""), h.get("line", 0)))
    return results[:max_results]
