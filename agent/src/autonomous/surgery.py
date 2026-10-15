"""Code surgery for Boom: exact-match patching with backup + validation + rollback.

This module deliberately complements agent/src/tools/file_ops.py rather than
duplicating it:

- file_ops.patch_file uses FUZZY matching (whitespace/unicode/approximate
  fallback strategies) and writes the file with no safety net.
- surgery here uses EXACT string matching only: 0 hits -> error,
  >1 hits -> error (never guesses), exactly 1 hit -> replace.

Every mutating operation follows the safe flow:
    backup -> modify -> validate syntax -> on failure auto-rollback.

Only stdlib is used: ast, difflib, json, shutil, time, os.
"""

import ast
import difflib
import json
import os
import shutil
import time

# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

_PAIRS = {")": "(", "]": "[", "}": "{"}


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _count_exact(content: str, needle: str) -> int:
    """Exact substring count. No regex, no normalization, no fuzzy logic."""
    return content.count(needle)


def _unified_diff(path: str, before: str, after: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile="a/" + path,
        tofile="b/" + path,
    )
    return "".join(lines)


def _detect_newline(content: str) -> str:
    if "\r\n" in content:
        return "\r\n"
    return "\n"


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def validate_syntax(path: str, content: str):
    """Validate source syntax based on file extension.

    Returns (ok: bool, message: str).
    .py  -> ast.parse
    .json -> json.loads
    .js/.ts/.jsx/.tsx -> crude bracket-balance check with a stack,
                         skipping strings and comments (catches gross errors)
    anything else -> (True, "skip")
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".py":
        try:
            ast.parse(content, filename=path)
        except SyntaxError as exc:
            return False, "SyntaxError: %s (line %s)" % (exc.msg, exc.lineno)
        return True, "ok"
    if ext == ".json":
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            return False, "JSONDecodeError: %s (line %s col %s)" % (
                exc.msg, exc.lineno, exc.colno)
        return True, "ok"
    if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        ok, msg = _check_brackets_js(content)
        return ok, msg
    return True, "skip"


def _check_brackets_js(content: str):
    """Crude JS/TS bracket balance check.

    Walks the source once, skipping '...' / "..." / `...` strings (with
    backslash escapes) and //... plus /*...*/ comments, then pushes/pops
    (){}[] on a stack. Reports the first mismatch or leftover opener.
    Good enough to catch gross damage (e.g. a deleted closing paren).
    """
    stack = []
    i = 0
    n = len(content)
    line = 1
    while i < n:
        ch = content[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        # line comment
        if ch == "/" and i + 1 < n and content[i + 1] == "/":
            j = content.find("\n", i + 2)
            i = n if j == -1 else j
            continue
        # block comment
        if ch == "/" and i + 1 < n and content[i + 1] == "*":
            j = content.find("*/", i + 2)
            if j == -1:
                return False, "unterminated block comment"
            line += content.count("\n", i, j)
            i = j + 2
            continue
        # strings
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            while i < n:
                c = content[i]
                if c == "\\":
                    i += 2
                    continue
                if c == "\n" and quote != "`":
                    return False, "unterminated string (line %d)" % line
                if c == "\n":
                    line += 1
                if c == quote:
                    i += 1
                    break
                i += 1
            else:
                return False, "unterminated string (line %d)" % line
            continue
        if ch in "([{":
            stack.append((ch, line))
            i += 1
            continue
        if ch in ")]}":
            if not stack:
                return False, "unmatched closing '%s' (line %d)" % (ch, line)
            opener, oline = stack.pop()
            if opener != _PAIRS[ch]:
                return False, (
                    "mismatched '%s' (line %d) closed by '%s' (line %d)"
                    % (opener, oline, ch, line)
                )
            i += 1
            continue
        i += 1
    if stack:
        opener, oline = stack[-1]
        return False, "unclosed '%s' opened at line %d" % (opener, oline)
    return True, "ok"


def backup(path: str) -> str:
    """Copy file to path + '.boom-bak-<timestamp>'. Returns backup path."""
    if not os.path.isfile(path):
        raise FileNotFoundError("file not found: %s" % path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = "%s.boom-bak-%s" % (path, stamp)
    # avoid clobbering if two backups happen within the same second
    counter = 0
    while os.path.exists(backup_path):
        counter += 1
        backup_path = "%s.boom-bak-%s-%d" % (path, stamp, counter)
    shutil.copy2(path, backup_path)
    return backup_path


def rollback(path: str, backup_path: str) -> dict:
    """Restore file content from a backup created by backup()."""
    if not os.path.isfile(backup_path):
        return {"ok": False, "error": "backup not found: %s" % backup_path}
    try:
        shutil.copy2(backup_path, path)
    except OSError as exc:
        return {"ok": False, "error": "rollback failed: %s" % exc}
    return {"ok": True, "path": path, "backup": backup_path}


def preview_diff(path: str, old: str, new: str) -> str:
    """Return unified diff of applying old->new, WITHOUT writing the file."""
    try:
        content = _read(path)
    except (OSError, FileNotFoundError):
        return ""
    if not old or _count_exact(content, old) != 1:
        return ""
    patched = content.replace(old, new, 1)
    return _unified_diff(path, content, patched)


def apply_patch(path: str, old: str, new: str, dry_run: bool = False) -> dict:
    """Replace the single exact occurrence of old with new.

    Safe flow: backup -> replace -> validate -> auto-rollback on invalid.
    dry_run: compute diff, write nothing, create no backup.
    """
    if not os.path.isfile(path):
        return {"ok": False, "error": "file not found: %s" % path}
    if not old:
        return {"ok": False, "error": "old text must not be empty"}

    content = _read(path)
    hits = _count_exact(content, old)
    if hits == 0:
        return {"ok": False, "error": "old text not found"}
    if hits > 1:
        return {"ok": False,
                "error": "old text matches %d locations, ambiguous" % hits}

    patched = content.replace(old, new, 1)
    diff = _unified_diff(path, content, patched)

    if dry_run:
        valid, msg = validate_syntax(path, patched)
        return {"ok": True, "diff": diff, "dry_run": True,
                "syntax_ok": valid, "syntax_msg": msg}

    backup_path = backup(path)
    try:
        _write(path, patched)
    except OSError as exc:
        return {"ok": False, "error": "write failed: %s" % exc,
                "backup": backup_path}

    valid, msg = validate_syntax(path, patched)
    if not valid:
        rollback(path, backup_path)
        return {"ok": False,
                "error": "syntax invalid after patch, rolled back: %s" % msg,
                "backup": backup_path}

    return {"ok": True, "diff": diff, "backup": backup_path}


def insert_after(path: str, anchor: str, text: str, dry_run: bool = False) -> dict:
    """Insert text on new line(s) right after the line containing anchor.

    anchor must match exactly once (0 or >1 -> error, never guesses).
    Safe flow: backup -> insert -> validate -> auto-rollback on invalid.
    """
    if not os.path.isfile(path):
        return {"ok": False, "error": "file not found: %s" % path}
    if not anchor:
        return {"ok": False, "error": "anchor must not be empty"}

    content = _read(path)
    hits = _count_exact(content, anchor)
    if hits == 0:
        return {"ok": False, "error": "anchor not found"}
    if hits > 1:
        return {"ok": False,
                "error": "anchor matches %d locations, ambiguous" % hits}

    nl = _detect_newline(content)
    idx = content.index(anchor)
    eol = content.find("\n", idx)
    if eol == -1:
        # anchor on last line without trailing newline
        insert_at = len(content)
        patched = content + nl + text
    else:
        insert_at = eol + 1
        insertion = text if text.endswith(nl) else text + nl
        patched = content[:insert_at] + insertion + content[insert_at:]

    diff = _unified_diff(path, content, patched)

    if dry_run:
        valid, msg = validate_syntax(path, patched)
        return {"ok": True, "diff": diff, "dry_run": True,
                "syntax_ok": valid, "syntax_msg": msg}

    backup_path = backup(path)
    try:
        _write(path, patched)
    except OSError as exc:
        return {"ok": False, "error": "write failed: %s" % exc,
                "backup": backup_path}

    valid, msg = validate_syntax(path, patched)
    if not valid:
        rollback(path, backup_path)
        return {"ok": False,
                "error": "syntax invalid after insert, rolled back: %s" % msg,
                "backup": backup_path}

    return {"ok": True, "diff": diff, "backup": backup_path}
