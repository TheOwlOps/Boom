"""Offline evals for autonomous.debugger.

Run:  python3 test_debugger.py        (no pytest, no network, stdlib only)

Uses tempfile dirs for file fixtures. A stub `verify` package is installed
on sys.path BEFORE importing the debugger so the critic-integration path
(import-try/fallback + critique-on-terminal-failure) is exercised without
needing the real Boom repo.
"""

import os
import shutil
import sys
import tempfile

# --- stub `verify` package (critic integration) before importing debugger ---
_STUB_DIR = tempfile.mkdtemp(prefix="boom_verify_stub_")
os.makedirs(os.path.join(_STUB_DIR, "verify"))
with open(os.path.join(_STUB_DIR, "verify", "__init__.py"), "w") as fh:
    fh.write("# test stub of the Boom verify package\n")
with open(os.path.join(_STUB_DIR, "verify", "critic.py"), "w") as fh:
    fh.write(
        "CALLS = []\n"
        "def critique(content, kind, llm):\n"
        "    CALLS.append({'content': content, 'kind': kind})\n"
        "    return [{'severity': 'high', 'issue': 'stub-issue',\n"
        "             'suggestion': 'stub-fix'}]\n"
    )
sys.path.insert(0, _STUB_DIR)
sys.path.insert(0, os.path.expanduser(
    "~/workspace/boom-mods/autonomous/agent/src"))

from autonomous.debugger import (  # noqa: E402
    classify_error,
    suggest_fix,
    debug_cycle,
    run_python,
    critic_available,
)
import autonomous.debugger as dbg  # noqa: E402
import verify.critic as stub_critic  # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name
          + (" -- " + str(extra) if extra and not cond else ""))


def write_tmp(tmpdir, name, content):
    p = os.path.join(tmpdir, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(content)
    return p


# ---------------------------------------------------------------- classify
def test_classify():
    s_syntax = ('Traceback (most recent call last):\n'
                '  File "/tmp/x.py", line 1, in <module>\n'
                '    def f()\n'
                '           ^\n'
                "SyntaxError: expected ':'\n")
    r = classify_error(s_syntax, 1)
    check("classify SyntaxError -> syntax", r["kind"] == "syntax", r)

    s_import = ('Traceback (most recent call last):\n'
                '  File "/tmp/x.py", line 1, in <module>\n'
                '    import requests\n'
                "ModuleNotFoundError: No module named 'requests'\n")
    r = classify_error(s_import, 1)
    check("classify ModuleNotFoundError -> import",
          r["kind"] == "import" and "requests" in r["detail"], r)

    s_test = ("FAILED test_x.py::test_y - AssertionError: assert 1 == 2\n"
              "E       assert 1 == 2\n")
    r = classify_error(s_test, 1)
    check("classify AssertionError/FAILED -> test_fail",
          r["kind"] == "test_fail", r)

    s_name = ('Traceback (most recent call last):\n'
              '  File "/tmp/x.py", line 1, in <module>\n'
              '    print(zzz)\n'
              "NameError: name 'zzz' is not defined\n")
    r = classify_error(s_name, 1)
    check("classify NameError -> runtime",
          r["kind"] == "runtime" and "NameError" in r["detail"], r)

    r = classify_error("something completely unexpected blah blah", 3)
    check("classify garbage -> unknown",
          r["kind"] == "unknown" and r["returncode"] == 3, r)

    r = classify_error("", 1)
    check("classify empty stderr -> unknown", r["kind"] == "unknown", r)


# -------------------------------------------------------------- suggest_fix
def test_suggest_fix():
    code = 'def f()\n    return 1\n'
    f = suggest_fix("syntax", "SyntaxError: expected ':' (line 1)", code)
    check("suggest missing colon -> old/new",
          f["old"] == "def f()" and f["new"] == "def f():", f)

    code2 = 'print("hello"\n'
    f = suggest_fix("syntax", "SyntaxError: '(' was never closed (line 1)",
                    code2)
    check("suggest unclosed bracket -> old/new",
          f["old"] == 'print("hello"' and f["new"] == 'print("hello")', f)

    f = suggest_fix("import",
                    "ModuleNotFoundError: No module named 'requests'", "")
    check("suggest import -> old None + reason names module",
          f["old"] is None and f["new"] is None
          and "requests" in (f["reason"] or ""), f)

    f = suggest_fix("runtime", "NameError: name 'zzz' is not defined",
                    "print(zzz)\n")
    check("suggest NameError undefined -> reason, no patch",
          f["old"] is None and "zzz" in (f["reason"] or ""), f)

    f = suggest_fix("runtime", "NameError: name 'v' is not defined",
                    "print(v)\nv = 1\n")
    check("suggest NameError defined-later -> reason, no patch",
          f["old"] is None and f["reason"], f)

    f = suggest_fix("test_fail", "AssertionError: assert 1 == 2", "x = 1\n")
    check("suggest test_fail -> reason, no patch",
          f["old"] is None and f["reason"], f)

    f = suggest_fix("runtime", "TypeError: unsupported operand", "x = 1\n")
    check("suggest TypeError -> reason, no patch",
          f["old"] is None and f["reason"], f)


# -------------------------------------------------------------- debug_cycle
def test_debug_cycle_fixes(tmpdir):
    p = write_tmp(tmpdir, "broken.py",
                  'def greet()\n    return "hello"\n\nprint(greet())\n')
    res = debug_cycle(p, run_python, max_rounds=3)
    check("debug_cycle fixes missing colon",
          res["status"] == "fixed" and res["rounds"] <= 2, res)
    again = run_python(p)
    check("patched file runs clean",
          again["ok"] is True and again["stdout"].strip() == "hello", again)


def test_debug_cycle_unfixable(tmpdir):
    src = "print(zzz)\n"
    p = write_tmp(tmpdir, "nameerr.py", src)
    before = open(p, "rb").read()
    res = debug_cycle(p, run_python, max_rounds=3)
    after = open(p, "rb").read()
    check("debug_cycle NameError -> unfixable",
          res["status"] == "unfixable" and res["kind"] == "runtime"
          and res["reason"], res)
    check("unfixable file NOT modified", before == after)
    check("no critic without llm", "critic_issues" not in res, res)


def test_debug_cycle_max_rounds(tmpdir):
    p = write_tmp(tmpdir, "stubborn.py", "def f()\n    pass\n")

    def always_syntax_err(path):
        return {"ok": False,
                "stderr": ('  File "x.py", line 1\n    def f()\n'
                           "SyntaxError: expected ':'"),
                "returncode": 1}

    real = dbg.apply_patch
    # patch "succeeds" but the file never actually heals -> loop must stop
    dbg.apply_patch = lambda path, old, new, dry_run=False: {
        "ok": True, "diff": "", "backup": None}
    try:
        res = debug_cycle(p, always_syntax_err, max_rounds=3)
    finally:
        dbg.apply_patch = real
    check("debug_cycle persistent failure -> max_rounds",
          res["status"] == "max_rounds" and res["rounds"] == 3
          and res["last_error"]["kind"] == "syntax", res)


def test_debug_cycle_critic_wiring(tmpdir):
    stub_critic.CALLS.clear()
    p = write_tmp(tmpdir, "cerr.py", "print(zzz)\n")
    res = debug_cycle(p, run_python, max_rounds=2,
                      llm=lambda prompt: '{"issues": []}')
    check("critic attached on terminal failure with llm",
          res["status"] == "unfixable"
          and isinstance(res.get("critic_issues"), list)
          and len(res["critic_issues"]) == 1, res)
    check("critic called with kind='code'",
          stub_critic.CALLS and stub_critic.CALLS[0]["kind"] == "code"
          and "NameError" in stub_critic.CALLS[0]["content"],
          stub_critic.CALLS)


# -------------------------------------------------------------- run_python
def test_run_python(tmpdir):
    p = write_tmp(tmpdir, "hi.py", 'print("hi")\n')
    r = run_python(p)
    check("run_python ok file", r["ok"] is True
          and r["stdout"].strip() == "hi" and r["returncode"] == 0, r)

    p = write_tmp(tmpdir, "boom.py", 'raise ValueError("bad")\n')
    r = run_python(p)
    check("run_python raising file",
          r["ok"] is False and "ValueError" in r["stderr"]
          and r["returncode"] != 0, r)

    p = write_tmp(tmpdir, "slow.py", "import time\ntime.sleep(30)\n")
    r = run_python(p, timeout=2)
    check("run_python timeout",
          r["ok"] is False and "timeout" in r["stderr"], r)


def main():
    print("critic_available():", critic_available())
    check("critic import fallback works (stub found)",
          critic_available() is True)
    test_classify()
    test_suggest_fix()
    tmpdir = tempfile.mkdtemp(prefix="boom_dbg_eval_")
    try:
        test_debug_cycle_fixes(tmpdir)
        test_debug_cycle_unfixable(tmpdir)
        test_debug_cycle_max_rounds(tmpdir)
        test_debug_cycle_critic_wiring(tmpdir)
        test_run_python(tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        shutil.rmtree(_STUB_DIR, ignore_errors=True)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("FAILED:", failed)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
