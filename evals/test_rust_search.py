"""Tests for agent/src/rust_bridge/search.py (boom-search Rust bridge).

Run: python3 test_rust_search.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "agent" / "src" / "rust_bridge"))

import search as search_mod

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def make_tree() -> Path:
    root = Path(tempfile.mkdtemp(prefix="boom_search_test_"))
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text(
        'import os\n\ndef hello():\n    print("hello world")\n\n# TODO: fix this later\nx = 42\n'
    )
    (root / "src" / "util.py").write_text(
        '# helper module\ndef world():\n    return "world"\n\n# TODO another\n'
    )
    (root / "src" / "main.rs").write_text('fn main() {\n    println!("hello rust");\n}\n')
    # skipped dirs
    (root / ".git").mkdir()
    (root / ".git" / "hidden.py").write_text("TODO: must not appear\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.py").write_text("TODO: must not appear\n")
    # fake binary: null byte within first 8KB
    (root / "src" / "fakebin.py").write_bytes(b"# fake\n\x00\x01TODO in binary\n")
    return root


def main():
    root = make_tree()
    rstr = str(root)

    # Point the wrapper at the real compiled binary (also exercises the
    # BOOM_SEARCH_BINARY env override).
    real_bin = HERE.parent / "target" / "release" / "boom-search"
    os.environ["BOOM_SEARCH_BINARY"] = str(real_bin)

    print("== rust_search with real binary ==")
    results = search_mod.rust_search(rstr, "TODO", "*.py", 50)
    check("finds 2 TODO hits", len(results) == 2, f"got {len(results)}")
    files = [r["file"] for r in results]
    check("hit in main.py", any(f.endswith("src/main.py") for f in files), files)
    check("hit in util.py", any(f.endswith("src/util.py") for f in files), files)
    lines = {Path(f).name: r["line"] for f, r in zip(files, results)}
    check("main.py TODO at line 6", lines.get("main.py") == 6, lines)
    check("util.py TODO at line 5", lines.get("util.py") == 5, lines)
    texts = [r["text"] for r in results]
    check("text trimmed", all(t == t.strip() for t in texts), texts)
    check("sorted by file", files == sorted(files), files)
    check(".git skipped", not any(".git" in f for f in files), files)
    check("node_modules skipped", not any("node_modules" in f for f in files), files)
    check("binary skipped", not any("fakebin" in f for f in files), files)

    print("== max_results ==")
    limited = search_mod.rust_search(rstr, "TODO", "*.py", 1)
    check("max_results=1 returns 1", len(limited) == 1, len(limited))

    print("== brace expansion ==")
    both = search_mod.rust_search(rstr, "hello", "*.{py,rs}", 50)
    check("brace expansion finds py+rs", len(both) == 3, f"got {len(both)}")

    print("== error cases (no crash, no raise) ==")
    check("missing root -> []", search_mod.rust_search("/no/such/dir/xyz", "x") == [])
    check("bad regex -> []", search_mod.rust_search(rstr, "[unclosed") == [])

    print("== raw binary protocol edge cases ==")
    binary = search_mod._find_binary()
    check("binary found", binary is not None and binary.is_file(), binary)
    if binary:
        p = subprocess.run(
            [str(binary)], input="not json", capture_output=True, text=True, timeout=30
        )
        data = json.loads(p.stdout)
        check("bad stdin JSON -> error payload", data["error"] is not None and data["results"] == [])
        check("exit code 0 on error", p.returncode == 0, p.returncode)

    print("== pure-Python fallback (BOOM_SEARCH_BINARY=/nonexistent) ==")
    del os.environ["BOOM_SEARCH_BINARY"]  # also covers default lookup path
    fb_default = search_mod.rust_search(rstr, "TODO", "*.py", 50)
    check("default lookup (no binary nearby) falls back", len(fb_default) == 2)
    os.environ["BOOM_SEARCH_BINARY"] = "/nonexistent/boom-search"
    try:
        fb = search_mod.rust_search(rstr, "TODO", "*.py", 50)
        check("fallback finds 2 TODO hits", len(fb) == 2, f"got {len(fb)}")
        fb_files = [r["file"] for r in fb]
        check("fallback sorted", fb_files == sorted(fb_files), fb_files)
        check("fallback skips .git", not any(".git" in f for f in fb_files))
        check("fallback skips binary", not any("fakebin" in f for f in fb_files))
        check("fallback bad regex -> []", search_mod.rust_search(rstr, "[unclosed") == [])
        check("fallback missing root -> []", search_mod.rust_search("/no/such/dir/xyz", "x") == [])
    finally:
        del os.environ["BOOM_SEARCH_BINARY"]

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
