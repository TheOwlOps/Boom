"""Python wrapper for the ``boom-text`` Rust binary.

Provides text statistics, keyword extraction and a lines-around window,
preferring the compiled Rust binary and falling back to a pure-Python
implementation when the binary is missing or misbehaves.

Binary discovery order:
  1. ``BOOM_TEXT_BINARY`` env override
  2. ``$CARGO_TARGET_DIR/release/boom-text``
  3. ``<repo>/crates/perf/textutil/target/release/boom-text``
     (relative to this file, for in-repo builds)

The wrapper never raises because of binary problems; it silently falls
back to the pure-Python implementation.
"""

from __future__ import annotations

import collections
import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

# Keep in sync with STOPWORDS in the Rust source (src/main.rs).
STOPWORDS = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "had",
    "her", "was", "one", "our", "out", "get", "has", "him", "his", "how", "new",
    "now", "see", "two", "way", "who", "did", "its", "let", "put", "say", "she",
    "too", "use", "will", "with", "this", "that", "from",
})

_SUBPROCESS_TIMEOUT = 60


def _find_binary() -> Optional[Path]:
    """Locate the boom-text binary, or return None if not found."""
    env_path = os.environ.get("BOOM_TEXT_BINARY")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p
        return None
    cargo_target = os.environ.get("CARGO_TARGET_DIR")
    if cargo_target:
        p = Path(cargo_target) / "release" / "boom-text"
        if p.is_file():
            return p
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    # Workspace builds (cargo build -p from repo root) place the binary in the
    # workspace-level target dir, so check that first.
    for cand in (
        repo_root / "target" / "release" / "boom-text",
        repo_root / "crates" / "perf" / "textutil" / "target" / "release" / "boom-text",
    ):
        if cand.is_file():
            return cand
    return None


def _call_binary(payload: Dict) -> Optional[Dict]:
    """Send one JSON request to the binary; return the parsed response dict.

    Returns None on any failure (missing binary, timeout, bad output).
    """
    binary = _find_binary()
    if binary is None:
        return None
    try:
        proc = subprocess.run(
            [str(binary)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("error"):
        return None
    return data


# ---------------------------------------------------------------------------
# Pure-Python fallback implementations (mirror the Rust behavior exactly).
# ---------------------------------------------------------------------------

def _py_stats(text: str) -> Dict:
    chars = len(text)  # Unicode code points, same as Rust chars().count()
    return {
        "lines": len(text.splitlines()),
        "words": len(text.split()),
        "chars": chars,
        "est_tokens": (chars + 3) // 4,
        "error": None,
    }


def _py_keywords(text: str) -> Dict:
    counts: "collections.Counter[str]" = collections.Counter()
    for token in text.split():
        word = token.lower()
        if len(word) < 3:
            continue
        if not word.isalpha():
            continue
        if word in STOPWORDS:
            continue
        counts[word] += 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    return {
        "keywords": [{"word": w, "count": n} for w, n in top],
        "error": None,
    }


def _py_lines_around(text: str, line_num: int, context: int = 3) -> Dict:
    lines = text.splitlines()
    total = len(lines)
    clamped = 1 if total == 0 else min(max(line_num, 1), total)
    context = max(context, 0)
    start = max(clamped - context, 1)
    end = min(clamped + context, total)
    window = [
        {"n": n, "text": lines[n - 1]}
        for n in range(start, end + 1)
    ]
    return {
        "lines": window,
        "start_line": start,
        "end_line": end,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rust_text_stats(text: str) -> Dict:
    """Return {"lines", "words", "chars", "est_tokens", "error"}."""
    result = _call_binary({"op": "stats", "text": text})
    if result is not None:
        return result
    return _py_stats(text)


def rust_keywords(text: str) -> List[Dict]:
    """Return [{"word", "count"}, ...], top 10 by frequency."""
    result = _call_binary({"op": "keywords", "text": text})
    if result is not None:
        return result.get("keywords", [])
    return _py_keywords(text)["keywords"]


def rust_lines_around(text: str, line_num: int, context: int = 3) -> Dict:
    """Return {"lines": [{"n", "text"}], "start_line", "end_line", "error"}."""
    result = _call_binary(
        {"op": "lines_around", "text": text, "line_num": line_num, "context": context}
    )
    if result is not None:
        return result
    return _py_lines_around(text, line_num, context)
