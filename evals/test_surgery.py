"""Offline tests for autonomous/surgery.py. stdlib only, uses tmpdir."""

import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent", "src"))

from autonomous.surgery import (  # noqa: E402
    apply_patch,
    insert_after,
    preview_diff,
    backup,
    rollback,
    validate_syntax,
)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS: %s" % name)
    else:
        FAIL += 1
        print("FAIL: %s %s" % (name, detail))


def write(p, content):
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(content)


def read(p):
    with open(p, "r", encoding="utf-8") as fh:
        return fh.read()


PY_SAMPLE = (
    '"""sample module."""\n'
    "\n"
    "\n"
    "def greet(name):\n"
    '    return "hello " + name\n'
    "\n"
    "\n"
    "def farewell(name):\n"
    '    return "bye " + name\n'
)


def test_apply_patch_success(tmp):
    p = os.path.join(tmp, "m1.py")
    write(p, PY_SAMPLE)
    old = '    return "hello " + name\n'
    new = '    return "hi " + name\n'
    r = apply_patch(p, old, new)
    content = read(p)
    check("apply_patch success ok", r.get("ok") is True, repr(r))
    check("apply_patch content changed",
          'return "hi " + name' in content and 'return "hello " + name' not in content,
          content[:200])
    check("apply_patch diff non-empty", bool(r.get("diff")), repr(r.get("diff"))[:100])
    check("apply_patch made backup",
          bool(glob.glob(p + ".boom-bak-*")), "no backup file")
    check("apply_patch patched file still valid python",
          validate_syntax(p, content) == (True, "ok"))


def test_apply_patch_old_not_found(tmp):
    p = os.path.join(tmp, "m2.py")
    write(p, PY_SAMPLE)
    before = read(p)
    r = apply_patch(p, "no such text anywhere", "x")
    check("old-not-found ok False", r.get("ok") is False, repr(r))
    check("old-not-found error text", "not found" in r.get("error", ""), repr(r))
    check("old-not-found file unchanged", read(p) == before)


def test_apply_patch_ambiguous(tmp):
    p = os.path.join(tmp, "m3.py")
    content = 'x = 1\nx = 1\ny = 2\n'
    write(p, content)
    r = apply_patch(p, "x = 1\n", "x = 9\n")
    check("ambiguous ok False", r.get("ok") is False, repr(r))
    check("ambiguous error mentions 2", "2" in r.get("error", "") and "ambiguous" in r.get("error", ""),
          repr(r))
    check("ambiguous file unchanged", read(p) == content)


def test_apply_patch_syntax_rollback(tmp):
    p = os.path.join(tmp, "m4.py")
    write(p, PY_SAMPLE)
    before = read(p)
    # new text drops the closing paren -> invalid python
    old = '    return "hello " + name\n'
    new = '    return "hi " + name\ndef broken(:\n'
    r = apply_patch(p, old, new)
    check("broken-patch ok False", r.get("ok") is False, repr(r))
    check("broken-patch error mentions rollback",
          "rolled back" in r.get("error", ""), repr(r))
    check("broken-patch file restored to original", read(p) == before, read(p)[:200])
    check("broken-patch restored file parses",
          validate_syntax(p, read(p))[0] is True)


def test_insert_after(tmp):
    p = os.path.join(tmp, "m5.py")
    write(p, PY_SAMPLE)
    r = insert_after(p, "def farewell(name):", '    """say goodbye."""')
    check("insert_after ok", r.get("ok") is True, repr(r))
    lines = read(p).splitlines()
    idx = lines.index("def farewell(name):")
    check("insert_after position",
          lines[idx + 1] == '    """say goodbye."""', str(lines[idx:idx + 3]))
    check("insert_after file valid", validate_syntax(p, read(p))[0] is True)


def test_insert_after_anchor_missing(tmp):
    p = os.path.join(tmp, "m6.py")
    write(p, PY_SAMPLE)
    before = read(p)
    r = insert_after(p, "def nonexistent():", "x = 1")
    check("insert anchor-missing ok False", r.get("ok") is False, repr(r))
    check("insert anchor-missing file unchanged", read(p) == before)


def test_insert_after_anchor_ambiguous(tmp):
    p = os.path.join(tmp, "m7.py")
    content = "key = 1\nkey = 1\n"
    write(p, content)
    r = insert_after(p, "key = 1", "key = 2")
    check("insert ambiguous ok False", r.get("ok") is False, repr(r))
    check("insert ambiguous error", "ambiguous" in r.get("error", ""), repr(r))
    check("insert ambiguous file unchanged", read(p) == content)


def test_preview_diff_no_write(tmp):
    p = os.path.join(tmp, "m8.py")
    write(p, PY_SAMPLE)
    before = read(p)
    mtime_before = os.path.getmtime(p)
    time.sleep(0.02)
    d = preview_diff(p, 'return "hello " + name', 'return "hi " + name')
    check("preview_diff returns diff", bool(d) and "@@" in d, d[:100])
    check("preview_diff content unchanged", read(p) == before)
    check("preview_diff mtime unchanged", os.path.getmtime(p) == mtime_before)


def test_dry_run_no_write(tmp):
    p = os.path.join(tmp, "m9.py")
    write(p, PY_SAMPLE)
    before = read(p)
    r = apply_patch(p, 'return "hello " + name', 'return "hi " + name', dry_run=True)
    check("dry_run ok True", r.get("ok") is True, repr(r))
    check("dry_run diff present", bool(r.get("diff")), repr(r))
    check("dry_run file unchanged", read(p) == before)
    check("dry_run no backup created", not glob.glob(p + ".boom-bak-*"))


def test_backup_and_manual_rollback(tmp):
    p = os.path.join(tmp, "m10.py")
    write(p, PY_SAMPLE)
    before = read(p)
    b = backup(p)
    check("backup returns path", isinstance(b, str) and os.path.isfile(b), repr(b))
    check("backup name pattern", ".boom-bak-" in b, b)
    check("backup content matches", read(b) == before)
    # manual edit
    write(p, PY_SAMPLE + "\n# extra line\n")
    check("file changed after manual edit", read(p) != before)
    r = rollback(p, b)
    check("rollback ok", r.get("ok") is True, repr(r))
    check("rollback restores content", read(p) == before)
    # rollback with missing backup
    r2 = rollback(p, p + ".does-not-exist")
    check("rollback missing backup fails", r2.get("ok") is False, repr(r2))


def test_validate_syntax(tmp):
    ok, msg = validate_syntax("a.py", "def f():\n    return 1\n")
    check("validate valid python", ok is True, msg)
    ok, msg = validate_syntax("a.py", "def f(:\n    return 1\n")
    check("validate invalid python", ok is False, msg)
    ok, msg = validate_syntax("a.json", '{"a": 1}')
    check("validate valid json", ok is True, msg)
    ok, msg = validate_syntax("a.json", '{"a": 1,}')
    check("validate invalid json", ok is False, msg)
    ok, msg = validate_syntax("a.txt", "anything goes {{{")
    check("validate unknown ext skipped", (ok, msg) == (True, "skip"), repr((ok, msg)))
    ok, msg = validate_syntax("a.js", "function f() { return [1, 2]; }")
    check("validate valid js", ok is True, msg)
    ok, msg = validate_syntax("a.js", "function f( { return 1; }")
    check("validate invalid js", ok is False, msg)


def main():
    import tempfile
    tmp = tempfile.mkdtemp(prefix="surgery-test-")
    test_apply_patch_success(tmp)
    test_apply_patch_old_not_found(tmp)
    test_apply_patch_ambiguous(tmp)
    test_apply_patch_syntax_rollback(tmp)
    test_insert_after(tmp)
    test_insert_after_anchor_missing(tmp)
    test_insert_after_anchor_ambiguous(tmp)
    test_preview_diff_no_write(tmp)
    test_dry_run_no_write(tmp)
    test_backup_and_manual_rollback(tmp)
    test_validate_syntax(tmp)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
