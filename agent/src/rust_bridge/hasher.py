"""Python wrapper around the boom-hash Rust binary (SHA256 hasher).

The binary reads a JSON request on stdin and writes a JSON response on
stdout. Every public function degrades to a pure-Python ``hashlib``
fallback when the binary is missing or misbehaves -- nothing here raises
because of the binary.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_TIMEOUT_S = 30


def _find_binary() -> Path | None:
    """Locate the boom-hash binary.

    Precedence:
      1. $BOOM_HASH_BINARY (explicit env override)
      2. $CARGO_TARGET_DIR/release/boom-hash
      3. <repo>/crates/perf/hasher/target/release/boom-hash, resolved
         relative to this file's eventual home at
         agent/src/rust_bridge/hasher.py
    """
    override = os.environ.get("BOOM_HASH_BINARY")
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p
        log.warning("BOOM_HASH_BINARY points at non-existent file: %s", override)

    cargo_target = os.environ.get("CARGO_TARGET_DIR")
    if cargo_target:
        p = Path(cargo_target) / "release" / "boom-hash"
        if p.is_file():
            return p

    here = Path(__file__).resolve()
    # hasher.py -> rust_bridge -> src -> agent -> <repo root>
    repo_root = here.parent.parent.parent.parent
    # Workspace builds (cargo build -p from repo root) place the binary in the
    # workspace-level target dir, so check that first.
    for cand in (
        repo_root / "target" / "release" / "boom-hash",
        repo_root / "crates" / "perf" / "hasher" / "target" / "release" / "boom-hash",
    ):
        if cand.is_file():
            return cand

    return None


def _run_binary(payload: dict) -> dict | None:
    """Run the binary once with a JSON payload; None on any failure."""
    binary = _find_binary()
    if binary is None:
        log.debug("boom-hash binary not found; using hashlib fallback")
        return None
    try:
        proc = subprocess.run(
            [str(binary)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("boom-hash binary failed (%s); using hashlib fallback", e)
        return None
    try:
        return json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("boom-hash returned bad JSON (%s); using hashlib fallback", e)
        return None


def _hashlib_file(path: str) -> str:
    """Pure-Python SHA256 of a file, streamed in 64KB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rust_hash_files(paths: list[str]) -> dict[str, str]:
    """Hash files via the Rust binary; returns {path: hex_digest}.

    Files the binary (or fallback) cannot read are skipped with a warning
    instead of raising.
    """
    result = _run_binary({"files": list(paths)})
    if result is not None and isinstance(result.get("hashes"), dict):
        if result.get("errors"):
            for path, msg in result["errors"].items():
                log.warning("boom-hash skipped %s: %s", path, msg)
        return {str(k): str(v) for k, v in result["hashes"].items()}

    # Fallback: hashlib, streaming.
    hashes: dict[str, str] = {}
    for path in paths:
        try:
            hashes[path] = _hashlib_file(path)
        except OSError as e:
            log.warning("hashlib fallback skipped %s: %s", path, e)
    return hashes


def rust_hash_text(text: str) -> str:
    """Hash a string via the Rust binary; returns the hex digest."""
    result = _run_binary({"text": text})
    if result is not None and isinstance(result.get("hash"), str):
        return result["hash"]
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
