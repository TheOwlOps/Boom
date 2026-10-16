"""Tests for the boom-hash Rust binary + hasher.py wrapper.

Run with: python3 test_rust_hasher.py
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "agent" / "src" / "rust_bridge"))

import hasher  # noqa: E402

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        failures.append(name)


def run_binary(payload: dict) -> dict:
    binary = hasher._find_binary()
    assert binary is not None, "boom-hash binary not found"
    proc = subprocess.run(
        [str(binary)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"binary exited {proc.returncode}"
    return json.loads(proc.stdout)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="boom_hash_test_"))
    text_file = tmp / "a.txt"
    text_file.write_text("hello boom hasher\n")
    empty_file = tmp / "empty.bin"
    empty_file.write_bytes(b"")
    bin_file = tmp / "rand.bin"
    bin_file.write_bytes(os.urandom(200_000))  # > 64KB: exercises streaming
    missing = str(tmp / "nope.txt")

    # --- 1. binary: files form matches hashlib ---
    out = run_binary({"files": [str(text_file), str(empty_file), str(bin_file), missing]})
    for p in (text_file, empty_file, bin_file):
        want = hashlib.sha256(p.read_bytes()).hexdigest()
        check(f"binary hash matches hashlib for {p.name}", out["hashes"][str(p)] == want)
    check("binary error key is null", out["error"] is None)
    check("missing file reported in errors", missing in out["errors"])

    # --- 2. binary: empty file constant ---
    check("empty file == SHA256-empty constant", out["hashes"][str(empty_file)] == EMPTY_SHA256)

    # --- 3. binary: text form ---
    out2 = run_binary({"text": "hello boom"})
    check("binary text hash matches hashlib", out2["hash"] == hashlib.sha256(b"hello boom").hexdigest())
    check("binary text error null", out2["error"] is None)

    # --- 4. binary: empty request -> graceful error, exit 0 ---
    out3 = run_binary({})
    check("empty request yields error message", bool(out3.get("error")))

    # --- 5. wrapper: happy path through real binary ---
    os.environ["BOOM_HASH_BINARY"] = str(hasher._find_binary())
    w = hasher.rust_hash_files([str(text_file), str(empty_file), missing])
    check("wrapper skips missing file", missing not in w and len(w) == 2)
    check(
        "wrapper file hash == hashlib",
        w[str(text_file)] == hashlib.sha256(text_file.read_bytes()).hexdigest(),
    )
    t = hasher.rust_hash_text("some text")
    check("wrapper text hash == hashlib", t == hashlib.sha256(b"some text").hexdigest())

    # --- 6. wrapper: forced fallback when binary is bogus ---
    os.environ["BOOM_HASH_BINARY"] = "/nonexistent/boom-hash"
    fb_files = hasher.rust_hash_files([str(text_file), missing])
    check("fallback still hashes good files", fb_files[str(text_file)] == hashlib.sha256(text_file.read_bytes()).hexdigest())
    check("fallback skips missing file", missing not in fb_files)
    fb_text = hasher.rust_hash_text("fallback")
    check("fallback text hash == hashlib", fb_text == hashlib.sha256(b"fallback").hexdigest())
    del os.environ["BOOM_HASH_BINARY"]

    # --- 7. wrapper: no raise on missing binary ---
    check("no BOOM_HASH_BINARY set is fine", hasher.rust_hash_text("x") == hashlib.sha256(b"x").hexdigest())

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        return 1
    print("All assertions passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
