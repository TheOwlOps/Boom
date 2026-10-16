"""Python wrapper for the `boom-chunk` Rust binary (Unicode-safe text chunker).

Looks for the compiled binary, shells out to it over a JSON stdin/stdout
protocol, and falls back to a pure-Python implementation when the binary is
missing or misbehaves. Never raises because of a binary problem.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_TIMEOUT_S = 60


def _find_binary() -> Path | None:
    """Locate the boom-chunk binary, or return None if it cannot be found."""
    # Explicit override wins.
    override = os.environ.get("BOOM_CHUNK_BINARY")
    if override:
        p = Path(override)
        if p.is_file():
            return p
        log.warning("BOOM_CHUNK_BINARY=%s does not exist, falling back", override)

    # $CARGO_TARGET_DIR/release/boom-chunk
    cargo_target = os.environ.get("CARGO_TARGET_DIR")
    if cargo_target:
        p = Path(cargo_target) / "release" / "boom-chunk"
        if p.is_file():
            return p

    # <repo>/crates/perf/chunker/target/release/boom-chunk, resolved from here
    # (this file will live at agent/src/rust_bridge/chunker.py).
    # Workspace builds (cargo build -p from repo root) place the binary in the
    # workspace-level target dir, so check that first.
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    for p in (
        repo_root / "target" / "release" / "boom-chunk",
        repo_root / "crates" / "perf" / "chunker" / "target" / "release" / "boom-chunk",
    ):
        if p.is_file():
            return p

    return None


def _python_chunk(text: str, max_tokens: int, overlap: int) -> list[str]:
    """Pure-Python fallback: same protocol semantics as the Rust binary."""
    if not text:
        return []
    max_chars = max_tokens * 4
    overlap_chars = min(overlap * 4, max_chars - 1) if max_chars > 1 else 0
    chars = list(text)
    total = len(chars)

    line_starts = [0]
    for i, c in enumerate(chars):
        if c == "\n":
            line_starts.append(i + 1)

    def snap(target: int) -> int | None:
        best = None
        for s in line_starts:
            if s > target:
                break
            best = s
        return best

    chunks: list[str] = []
    nxt = 0
    guard = 0
    while nxt < total:
        guard += 1
        if guard > total * 2 + 16:
            break
        limit = min(nxt + max_chars, total)
        end = limit
        hard_cut = False
        if limit < total:
            last_nl = None
            for i in range(nxt, limit):
                if chars[i] == "\n":
                    last_nl = i
            if last_nl is not None:
                end = last_nl
            else:
                end = limit
                hard_cut = True
        if end == nxt:
            nxt += 1
            continue
        chunks.append("".join(chars[nxt:end]))
        end_raw = end + 1 if (end < total and chars[end] == "\n") else end
        if end_raw >= total:
            break
        target = max(end_raw - overlap_chars, 0)
        s = snap(target)
        if s is not None and nxt < s <= end_raw:
            new_next = s
        elif hard_cut:
            new_next = max(target, nxt + 1)
        else:
            new_next = min(max(target, nxt + 1), end_raw)
        new_next = min(new_next, end_raw)
        nxt = max(new_next, nxt + 1)  # always move forward
    return chunks


def rust_chunk(text: str, max_tokens: int = 500, overlap: int = 50) -> list[str]:
    """Split `text` into chunks of ~`max_tokens` tokens with `overlap` overlap.

    Uses the `boom-chunk` Rust binary when available; falls back to a
    pure-Python implementation otherwise. Never raises because of a binary
    problem -- on any binary failure the fallback (or an empty list for
    invalid input) is returned.
    """
    if not text:
        return []
    if max_tokens <= 0:
        return []

    binary = _find_binary()
    if binary is not None:
        try:
            proc = subprocess.run(
                [str(binary)],
                input=json.dumps(
                    {"text": text, "max_tokens": max_tokens, "overlap": overlap}
                ),
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_S,
            )
            resp = json.loads(proc.stdout)
            if resp.get("error") is None and isinstance(resp.get("chunks"), list):
                return [str(c) for c in resp["chunks"]]
            log.warning(
                "boom-chunk returned error: %s; using Python fallback",
                resp.get("error"),
            )
        except Exception as exc:  # binary missing/crashed/timed out/bad output
            log.warning("boom-chunk failed (%s); using Python fallback", exc)

    return _python_chunk(text, max_tokens, overlap)
