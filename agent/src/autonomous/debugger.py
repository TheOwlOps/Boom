"""Autonomous debugger for Boom: run -> classify -> minimal fix -> rerun.

A mechanical self-debugging loop for Python files. It runs a file, classifies
the failure, and applies only fixes it is *certain* about (mechanical syntax
repairs, verified by recompiling the patched source before touching the
file). Anything needing judgement -- logic bugs, missing modules, assertion
failures, runtime semantics -- is reported honestly with a reason instead of
guessed.

Reused building blocks (imported, never reimplemented):

- ``autonomous.surgery.apply_patch`` -- exact-match patching with backup +
  syntax validation + auto-rollback. Written by a teammate in this same
  package; imported for real at module top.
- ``verify.critic.critique`` -- the adversarial critic from
  ``agent/src/verify/critic.py`` (``critique(content, kind, llm)``). Imported
  opportunistically: used only when the caller supplies an ``llm`` callable
  *and* the loop ends without a fix, so the failure at least gets a hostile
  second pair of eyes. Falls back to ``None`` when the ``verify`` package is
  not importable -- the debugger works fine without it.
- Run convention follows ``agent/src/verify/codecheck.py``: subprocess,
  no ``shell=True``, timeout, truncated error capture.

Related prior art deliberately NOT reused here: ``verify.codecheck.self_debug``
needs an LLM to propose fixes (we do mechanical fixes only, no LLM required);
``tools/code_execution.py`` exec's code in-process (we run files in a separate
interpreter so tracebacks reference real file/line numbers).

Standard library only.
"""

from __future__ import annotations

import re
import subprocess
import sys
from typing import Callable, Dict, List, Optional

# --- real import: surgery.py is written by a teammate in this same package ---
try:
    from .surgery import apply_patch  # package mode: autonomous.debugger
except ImportError:  # pragma: no cover - executed as a top-level script
    from autonomous.surgery import apply_patch  # type: ignore[no-redef]

# --- optional reuse of the adversarial critic (needs an llm callable) ---
try:
    from verify.critic import critique as _critique  # type: ignore[no-redef]
except ImportError:  # pragma: no cover - verify package not on sys.path
    _critique = None

__all__ = [
    "classify_error",
    "suggest_fix",
    "debug_cycle",
    "run_python",
    "critic_available",
]


def critic_available() -> bool:
    """True when ``verify.critic.critique`` could be imported."""
    return _critique is not None


# ---------------------------------------------------------------------------
# error classification
# ---------------------------------------------------------------------------

_SYNTAX_MARKERS = ("SyntaxError", "IndentationError", "TabError")
_IMPORT_MARKERS = ("ModuleNotFoundError", "ImportError")
_TEST_MARKERS = ("AssertionError", "FAILED", "assert")
_RUNTIME_RE = re.compile(r"^([\w.]+)\s*:\s*(.*)$")
_BARE_NAME_RE = re.compile(r"^[\w.]+$")
_KNOWN_RUNTIME_RE = re.compile(
    r"^([\w.]+(Error|Exception|Warning)|KeyboardInterrupt|SystemExit|"
    r"StopIteration|GeneratorExit)\b"
)


def _tail_lines(stderr: str, n: int) -> List[str]:
    lines = [ln for ln in (stderr or "").splitlines() if ln.strip()]
    return lines[-n:]


def classify_error(stderr: str, returncode: int) -> dict:
    """Classify a process failure from its stderr text.

    Returns ``{"kind": ..., "detail": ..., "returncode": ...}`` where kind is
    one of ``syntax`` | ``import`` | ``test_fail`` | ``runtime`` | ``unknown``.
    Pure function; never raises on arbitrary input.
    """
    s = stderr or ""
    base = {"returncode": returncode}

    if any(m in s for m in _SYNTAX_MARKERS):
        tail = _tail_lines(s, 6)
        exc_line = tail[-1] if tail else ""
        m = re.search(r"line (\d+)", s)
        detail = exc_line
        if m:
            detail = "%s (line %s)" % (exc_line, m.group(1))
        return {"kind": "syntax", "detail": detail.strip(), **base}

    if any(m in s for m in _IMPORT_MARKERS):
        tail = _tail_lines(s, 4)
        return {"kind": "import",
                "detail": tail[-1] if tail else "import error", **base}

    if any(m in s for m in _TEST_MARKERS):
        tail = _tail_lines(s, 3)
        return {"kind": "test_fail",
                "detail": "\n".join(tail) if tail else "test failure",
                **base}

    tail = _tail_lines(s, 1)
    last = tail[-1] if tail else ""
    m = _RUNTIME_RE.match(last.strip())
    if m and _KNOWN_RUNTIME_RE.match(m.group(1)):
        name, msg = m.group(1), m.group(2).strip()
        return {"kind": "runtime",
                "detail": ("%s: %s" % (name, msg)).strip(),
                "exception": name, **base}
    if _BARE_NAME_RE.match(last.strip()) and _KNOWN_RUNTIME_RE.match(
            last.strip()):
        return {"kind": "runtime", "detail": last.strip(),
                "exception": last.strip(), **base}

    detail = last if last else "no stderr captured"
    return {"kind": "unknown", "detail": detail, **base}


# ---------------------------------------------------------------------------
# minimal fix suggestion (mechanical only -- never guesses logic)
# ---------------------------------------------------------------------------

_COMPOUND_RE = re.compile(
    r"^(async\s+)?(def|if|elif|else|for|while|with|try|except|finally|"
    r"class|match|case)\b"
)
_NO_MODULE_RE = re.compile(r"No module named ['\"]([^'\"]+)['\"]")
_NAME_ERROR_RE = re.compile(r"NameError:\s*name '([^']+)' is not defined")
_LINE_NO_RE = re.compile(r"line (\d+)")

_BRACKET_CLOSER = {"(": ")", "[": "]", "{": "}"}
_BRACKET_PAIR = {")": "(", "]": "[", "}": "{"}


def _no_fix(reason: str) -> dict:
    return {"old": None, "new": None, "reason": reason}


def _missing_closers(line: str) -> str:
    """Return the closing brackets a line is missing, or "".

    String-aware (skips quoted text and # comments). Returns "" when the
    brackets are balanced *or* mismatched -- only the clean "unclosed"
    case is reported, because only that has a certain mechanical fix.
    """
    stack: List[str] = []
    in_str: Optional[str] = None
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch == "#":
            break
        if ch in ("'", '"'):
            in_str = ch
            i += 1
            continue
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if stack and stack[-1] == _BRACKET_PAIR[ch]:
                stack.pop()
            else:
                return ""  # mismatched closer: not a simple unclosed case
        i += 1
    return "".join(_BRACKET_CLOSER[c] for c in reversed(stack))


def _syntax_lineno(detail: str, code: str) -> Optional[int]:
    m = _LINE_NO_RE.search(detail or "")
    if m:
        return int(m.group(1))
    # Fallback: compile the code ourselves -- SyntaxError carries .lineno.
    try:
        compile(code, "<debugger>", "exec")
    except SyntaxError as exc:
        return exc.lineno
    except (ValueError, TypeError):
        return None
    return None


def _candidate_fix(code: str, lines: List[str], lineno: int,
                   old_line: str, new_line: str, what: str) -> dict:
    """Accept a syntax fix only if the patched source compiles cleanly.

    This is the certainty gate: a mechanical repair that does not actually
    resolve the SyntaxError is refused with a reason instead of applied.
    """
    patched = "\n".join(lines[:lineno - 1] + [new_line] + lines[lineno:])
    try:
        compile(patched, "<debugger>", "exec")
    except SyntaxError as exc:
        return _no_fix(
            "candidate %s fix still leaves a syntax error: %s (line %s); "
            "refusing to apply" % (what, exc.msg, exc.lineno))
    except (ValueError, TypeError) as exc:
        return _no_fix("candidate %s fix not verifiable: %s" % (what, exc))
    return {"old": old_line, "new": new_line, "reason": None, "fix": what}


def _fix_syntax(detail: str, code: str) -> dict:
    lineno = _syntax_lineno(detail, code)
    if lineno is None:
        return _no_fix("could not locate the error line in the traceback")
    lines = code.splitlines()
    if not 1 <= lineno <= len(lines):
        return _no_fix("error line %d out of range (%d lines)"
                       % (lineno, len(lines)))
    line = lines[lineno - 1]
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return _no_fix("error line is empty or a comment; no mechanical fix")

    # Case 1: unclosed bracket on the error line -> append the closers.
    missing = _missing_closers(line)
    if missing:
        return _candidate_fix(code, lines, lineno, line, line + missing,
                              "unclosed bracket")

    # Case 2: compound-statement header missing its trailing colon.
    if (_COMPOUND_RE.match(stripped) and not stripped.endswith(":")
            and not stripped.endswith("\\")):
        return _candidate_fix(code, lines, lineno, line, line + ":",
                              "missing colon")

    return _no_fix("no mechanical syntax fix applies to line %d: %r "
                   "(not an unclosed bracket, not a missing colon)"
                   % (lineno, stripped[:60]))


def _defines_name(code: str, name: str) -> bool:
    """Heuristic: is `name` defined anywhere in the source?"""
    n = re.escape(name)
    patterns = [
        r"^\s*%s\s*=(?!=)" % n,          # assignment (not ==)
        r"^\s*def\s+%s\b" % n,
        r"^\s*class\s+%s\b" % n,
        r"^\s*import\s+.*\b%s\b" % n,
        r"^\s*from\s+\S+\s+import\s+.*\b%s\b" % n,
        r"\bfor\s+%s\s+in\b" % n,
        r"\bas\s+%s\b" % n,             # with/except ... as name
        r"^\s*global\s+.*\b%s\b" % n,
        r"^\s*nonlocal\s+.*\b%s\b" % n,
    ]
    return any(re.search(p, code, re.M) for p in patterns)


def _fix_runtime(detail: str, code: str) -> dict:
    detail = detail or ""
    m = _NAME_ERROR_RE.search(detail)
    if m:
        name = m.group(1)
        if _defines_name(code, name):
            reason = ("name '%s' is defined somewhere in the file "
                      "(ordering/scope problem); refusing to guess a fix"
                      % name)
        else:
            reason = ("name '%s' is not defined anywhere in the file; "
                      "cannot invent a definition" % name)
        return _no_fix(reason)
    last = detail.strip().splitlines()
    exc_name = "runtime error"
    if last:
        mm = _KNOWN_RUNTIME_RE.match(last[-1])
        if mm:
            exc_name = mm.group(1)
    return _no_fix("%s needs semantic understanding of the code; "
                   "refusing to guess a fix" % exc_name)


def suggest_fix(kind: str, detail: str, code: str) -> dict:
    """Suggest a minimal ``{"old", "new"}`` patch, or refuse honestly.

    Only mechanical syntax repairs are ever suggested (and only after the
    patched source is proven to compile). Every other failure kind returns
    ``{"old": None, "new": None, "reason": ...}``. Pure function.
    """
    if kind == "syntax":
        return _fix_syntax(detail or "", code or "")
    if kind == "import":
        m = _NO_MODULE_RE.search(detail or "")
        mod = m.group(1) if m else "unknown"
        return _no_fix("missing module %s, need install "
                       "(not auto-installing packages)" % mod)
    if kind == "test_fail":
        return _no_fix("test/assertion failure needs logic understanding; "
                       "refusing to guess a fix")
    if kind == "runtime":
        return _fix_runtime(detail or "", code or "")
    return _no_fix("unrecognized error kind %r; no safe fix available" % kind)


# ---------------------------------------------------------------------------
# debug loop
# ---------------------------------------------------------------------------

def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _terminal(status: str, path: str, rounds: int, err: dict,
              reason: str, code: str,
              llm: Optional[Callable[[str], str]]) -> dict:
    """Build a terminal result; optionally attach an adversarial critique."""
    out = {
        "status": status,
        "path": path,
        "rounds": rounds,
        "kind": err.get("kind"),
        "reason": reason,
        "last_error": {
            "kind": err.get("kind"),
            "detail": err.get("detail"),
            "returncode": err.get("returncode"),
        },
    }
    if llm is not None and _critique is not None:
        try:
            content = "%s\n\n=== ERROR ===\n%s" % (code, err.get("detail") or "")
            out["critic_issues"] = _critique(content, "code", llm)
        except Exception as exc:  # critic must never break the loop
            out["critic_error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


def debug_cycle(path: str, run_fn: Callable[[str], dict],
                max_rounds: int = 3,
                llm: Optional[Callable[[str], str]] = None) -> dict:
    """Run -> classify -> minimal fix -> rerun, up to ``max_rounds``.

    ``run_fn(path)`` -> ``{"ok": bool, "stderr": str, "returncode": int}`` is
    injected so the loop is testable with fakes; pass ``run_python`` for real
    runs. ``llm`` is optional: when given (and the ``verify`` critic is
    importable), a terminal failure gets an adversarial critique attached.

    Never raises: every failure mode becomes a result dict.
    """
    try:
        return _debug_cycle(path, run_fn, max_rounds, llm)
    except Exception as exc:  # absolute last resort -- never raise
        return {"status": "unfixable", "path": path, "rounds": 0,
                "kind": "internal",
                "reason": "debugger internal error: %s: %s"
                          % (type(exc).__name__, exc)}


def _debug_cycle(path: str, run_fn: Callable[[str], dict], max_rounds: int,
                 llm: Optional[Callable[[str], str]]) -> dict:
    try:
        code = _read_text(path)
    except OSError as exc:
        return {"status": "unfixable", "path": path, "rounds": 0,
                "kind": "io",
                "reason": "cannot read %s: %s" % (path, exc)}

    last_err = {"kind": "unknown", "detail": "", "returncode": None}
    rounds = 0
    for n in range(1, max(0, max_rounds) + 1):
        rounds = n
        try:
            res = run_fn(path) or {}
        except Exception as exc:
            return {"status": "unfixable", "path": path, "rounds": n,
                    "kind": "runner",
                    "reason": "run_fn raised %s: %s"
                             % (type(exc).__name__, exc)}
        if res.get("ok"):
            return {"status": "fixed", "path": path, "rounds": n}

        err = classify_error(res.get("stderr") or "",
                             res.get("returncode", 1))
        last_err = err
        fix = suggest_fix(err.get("kind", "unknown"),
                          err.get("detail") or "", code)
        old, new = fix.get("old"), fix.get("new")
        if not old or new is None:
            return _terminal("unfixable", path, rounds, err,
                             fix.get("reason") or "no safe fix available",
                             code, llm)
        try:
            pres = apply_patch(path, old, new)
        except Exception as exc:
            return _terminal("unfixable", path, rounds, err,
                             "apply_patch raised %s: %s"
                             % (type(exc).__name__, exc), code, llm)
        if not pres.get("ok"):
            return _terminal("unfixable", path, rounds, err,
                             "patch refused: %s" % pres.get("error"),
                             code, llm)
        try:
            code = _read_text(path)
        except OSError as exc:
            return _terminal("unfixable", path, rounds, err,
                             "cannot re-read file after patch: %s" % exc,
                             code, llm)

    return _terminal("max_rounds", path, rounds, last_err,
                     "still failing after %d round(s)" % rounds, code, llm)


# ---------------------------------------------------------------------------
# real runner: execute a Python file in a subprocess
# ---------------------------------------------------------------------------

def run_python(path: str, timeout: int = 30) -> dict:
    """Run ``path`` with the current interpreter, capturing output.

    Returns ``{"ok", "stdout", "stderr", "returncode"}``. Timeout and OS
    errors become ``ok=False`` results, never exceptions.
    """
    try:
        proc = subprocess.run(
            [sys.executable, path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "",
                "stderr": "timeout after %ss: %s" % (timeout, path),
                "returncode": -1}
    except OSError as exc:
        return {"ok": False, "stdout": "",
                "stderr": "cannot execute %s: %s" % (path, exc),
                "returncode": -1}
    return {"ok": proc.returncode == 0,
            "stdout": proc.stdout or "",
            "stderr": (proc.stderr or "").strip(),
            "returncode": proc.returncode}
