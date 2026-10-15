"""Tests for fileops.py — run with: python3 test_fileops.py"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fileops import edit_file, append_file, file_info

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name} {detail}")


with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)  # cwd is the sandbox root; traversal checks run against it

    # ---- Test 1: edit_file replaces exactly one occurrence ----
    p = Path("t1.txt")
    p.write_text("alpha beta gamma\nbeta delta\n", encoding="utf-8")
    r = edit_file("t1.txt", "beta delta", "beta CHANGED")
    check("T1 ok", r.get("ok") is True, r)
    body = p.read_text(encoding="utf-8")
    check("T1 content replaced at right spot",
          body == "alpha beta gamma\nbeta CHANGED\n", repr(body))

    # ---- Test 2: ambiguous (2 occurrences) -> no change ----
    p2 = Path("t2.txt")
    original = "foo bar foo\n"
    p2.write_text(original, encoding="utf-8")
    r = edit_file("t2.txt", "foo", "baz")
    check("T2 ambiguous reported",
          r.get("ok") is False and "ambiguous: 2 matches" in r.get("error", ""), r)
    check("T2 file untouched", p2.read_text(encoding="utf-8") == original)

    # ---- Test 3: not found ----
    r = edit_file("t2.txt", "zzz-no-such-text", "x")
    check("T3 not found", r.get("ok") is False and r.get("error") == "not found", r)

    # ---- Test 4: .bak backup created, identical to original ----
    p4 = Path("t4.txt")
    orig4 = "line one\nline two\n"
    p4.write_text(orig4, encoding="utf-8")
    r = edit_file("t4.txt", "line two", "line TWO!")
    bak = Path("t4.txt.bak")
    check("T4 backup exists", bak.exists(), r)
    check("T4 backup == original", bak.read_text(encoding="utf-8") == orig4, r)
    check("T4 edited file correct",
          p4.read_text(encoding="utf-8") == "line one\nline TWO!\n")

    # ---- Test 5a: append to existing file ----
    p5 = Path("t5.txt")
    p5.write_text("first", encoding="utf-8")  # no trailing newline
    r = append_file("t5.txt", "second")
    check("T5a ok", r.get("ok") is True, r)
    check("T5a newline inserted", p5.read_text(encoding="utf-8") == "first\nsecond")
    check("T5a bytes_added", r.get("bytes_added") == len("first\nsecond".encode()) - len("first".encode()), r)

    # ---- Test 5b: append creates missing file ----
    r = append_file("brand_new.txt", "hello")
    check("T5b ok + created",
          r.get("ok") is True and Path("brand_new.txt").exists(), r)
    check("T5b content", Path("brand_new.txt").read_text(encoding="utf-8") == "hello")
    # newline=False appends verbatim
    r = append_file("brand_new.txt", "!", newline=False)
    check("T5b no-newline verbatim",
          Path("brand_new.txt").read_text(encoding="utf-8") == "hello!")

    # ---- Test 6: file_info accuracy ----
    p6 = Path("t6.txt")
    data = "a\nb\nc\n"
    p6.write_text(data, encoding="utf-8")
    r = file_info("t6.txt")
    check("T6 exists", r.get("exists") is True, r)
    check("T6 size", r.get("size") == len(data.encode("utf-8")), r)
    check("T6 lines", r.get("lines") == 3, r)
    check("T6 mtime is ISO string", isinstance(r.get("mtime"), str) and "T" in r.get("mtime"), r)
    check("T6 not dir", r.get("is_dir") is False, r)
    r = file_info("nope.txt")
    check("T6 missing", r == {"exists": False}, r)
    rd = file_info(".")
    check("T6 dir", rd.get("exists") is True and rd.get("is_dir") is True, rd)

    # ---- Test 7: path traversal blocked ----
    for fn in (edit_file, append_file):
        r = fn("../../etc/passwd", "x", "y") if fn is edit_file else fn("../../etc/passwd", "x")
        check(f"T7 traversal blocked ({fn.__name__})",
              r.get("ok") is False and r.get("error") == "path traversal blocked", r)
    r = file_info("../../etc/passwd")
    check("T7 traversal blocked (file_info)", r.get("exists") is False and r.get("error") == "path traversal blocked", r)
    # absolute escape also blocked
    r = edit_file("/etc/hostname", "x", "y")
    check("T7 absolute escape blocked", r.get("ok") is False and r.get("error") == "path traversal blocked", r)

    # ---- Bonus: binary file refused ----
    pb = Path("bin.dat")
    pb.write_bytes(b"\x00\x01\x02binary\x00junk")
    r = edit_file("bin.dat", "junk", "x")
    check("T8 binary edit refused", r.get("ok") is False and r.get("error") == "binary file refused", r)
    r = append_file("bin.dat", "tail")
    check("T8 binary append refused", r.get("ok") is False and r.get("error") == "binary file refused", r)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
