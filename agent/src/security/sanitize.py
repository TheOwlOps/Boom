"""Sanitizes tool inputs: anti-injection (shell / prompt / path traversal).

This module complements `autonomous/guard.py` (blocks destructive commands)
and `security/guardrail.py` (blocks dangerous patterns): it sanitizes input
values before a tool executes — quoting shell args, detecting prompt
injection, filtering control chars, capping length, validating paths.

Stdlib only.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Prompt injection patterns (EN + VI), matched case-insensitively.
# ---------------------------------------------------------------------------
# Description: phrases commonly used to trick the LLM into dropping its system prompt.
# NOTE: functional keyword list — kept as-is (evals cover the detection).
INJECTION_PATTERNS: List[str] = [
    # --- English ---
    "ignore previous instructions",
    "disregard previous instructions",
    "ignore all previous instructions",
    "ignore your instructions",
    "forget your instructions",
    "system prompt",
    "reveal your prompt",
    "show your prompt",
    "jailbreak",
    "dan mode",
    "do anything now",
    "you are now",
    "pretend you are",
    "act as if",
    "override your",
    "bypass your",
    # --- Vietnamese (with and without diacritics) ---
    "bỏ qua hướng dẫn",
    "bo qua huong dan",
    "bỏ qua mọi hướng dẫn",
    "quên hướng dẫn",
    "quen huong dan",
    "tiết lộ system prompt",
    "tiet lo system prompt",
    "hiển thị system prompt",
    "hien thi system prompt",
    "vượt rào",
    "vuot rao",
    "chế độ dan",
    "che do dan",
    "giả vờ bạn là",
    "gia vo ban la",
    "hãy giả vờ",
    "hay gia vo",
]

# Precompiled for fast matching, case-insensitive.
_COMPILED_INJECTION: List[tuple] = [
    (p, re.compile(re.escape(p), re.IGNORECASE)) for p in INJECTION_PATTERNS
]

# Dangerous control chars to filter: all C0/C1 except \n (0x0A) and \t (0x09).
# Note: \x0b (VT), \x0c (FF), \x0d (CR) are all dropped — CR prevents
# terminal spoofing, VT/FF are unnecessary controls.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x0d\x0e-\x1f\x7f\x80-\x9f]")

# Tool arg keys that usually hold paths/files (mirrors guard.py's style).
_PATH_KEYS = {
    "path", "file", "file_path", "filepath", "filename", "target", "dest", "dst",
    "src", "source", "dir", "directory", "folder", "root", "cwd", "workdir",
    "working_dir", "output", "out", "log", "logfile",
}


def sanitize_shell_arg(arg: Any) -> str:
    """Quotes 1 argument safely for shell (shlex.quote).

    - Accepts str or str-coercible values (int/float/bool...).
    - Raises ValueError on null bytes (guards C-level / exec bypasses).
    - Raises TypeError on non string-like values (list/dict/None).
    """
    if isinstance(arg, str):
        s = arg
    elif isinstance(arg, (int, float, bool)):
        s = str(arg)
    else:
        raise TypeError(
            "shell arg must be str or a number, got %s" % type(arg).__name__
        )
    if "\x00" in s:
        raise ValueError("shell arg contains a null byte — rejected")
    # Validate first, then quote: shlex.quote never fails,
    # but null bytes were already blocked above.
    return shlex.quote(s)


def detect_prompt_injection(text: Any) -> Dict[str, Any]:
    """Detects prompt injection patterns in text.

    Returns {"is_injection": bool, "matched": [pattern, ...]}.
    - Non-strings (None/numbers/...): returns is_injection=False.
    - Case-insensitive, catches Vietnamese with/without diacritics.
    """
    if not isinstance(text, str) or not text:
        return {"is_injection": False, "matched": []}
    matched: List[str] = []
    for pattern, rx in _COMPILED_INJECTION:
        if rx.search(text):
            matched.append(pattern)
    return {"is_injection": bool(matched), "matched": matched}


def strip_control_chars(text: Any) -> Any:
    """Removes dangerous control chars, KEEPS \\n and \\t.

    - Only touches str; other values pass through unchanged.
    - \r (carriage return) is dropped to prevent terminal spoofing
      (e.g. "OK\rMALICIOUS" renders deceptively).
    """
    if not isinstance(text, str):
        return text
    return _CONTROL_RE.sub("", text)


def truncate(text: Any, max_len: int) -> Any:
    """Cuts text to at most max_len chars, adding the "[cắt bớt]" marker.

    - max_len <= 0: raises ValueError.
    - Non-str or already shorter than max_len: returned unchanged.
    - When max_len is too small (< len(marker)): plain cut, no marker.
    NOTE: the marker is pinned by evals/test_overnight_sanitize.py — keep it.
    """
    marker = "[cắt bớt]"
    if not isinstance(max_len, int) or max_len <= 0:
        raise ValueError("max_len must be a positive integer")
    if not isinstance(text, str) or len(text) <= max_len:
        return text
    if max_len <= len(marker):
        return text[:max_len]
    return text[: max_len - len(marker)] + marker


def validate_path(path: Any, allowed_roots: List[str]) -> str:
    """Anti path traversal: returns the validated absolute path, or raises.

    - Resolves ".." and symlinks with os.path.realpath.
    - The final path must be inside (or equal to) at least 1 allowed_root,
      otherwise raises ValueError.
    - Raises TypeError when path isn't a str; ValueError when empty/null byte.
    """
    if not isinstance(path, str):
        raise TypeError("path must be str, got %s" % type(path).__name__)
    if not path:
        raise ValueError("empty path")
    if "\x00" in path:
        raise ValueError("path contains a null byte — rejected")
    if not allowed_roots:
        raise ValueError("allowed_roots is empty — no root is allowed")

    resolved = os.path.realpath(path)
    for root in allowed_roots:
        if not root or not isinstance(root, str):
            continue
        rroot = os.path.realpath(root)
        if resolved == rroot or resolved.startswith(rroot + os.sep):
            return resolved
    raise ValueError(
        "path %r is outside allowed_roots (after resolve: %r)"
        % (path[:160], resolved[:160])
    )


def check_tool_args(
    tool_name: str,
    args: Dict[str, Any],
    allowed_roots: List[str] | None = None,
    max_str_len: int = 100_000,
) -> Dict[str, Any]:
    """The full pass: sanitizes a tool's args dict.

    For each string value:
      1. strip_control_chars (keeps \\n\\t).
      2. truncate to max_str_len (warns when cut).
      3. detect_prompt_injection (warns on pattern match).
    For keys in _PATH_KEYS: validate_path against allowed_roots
    (skipped when allowed_roots isn't passed).

    Returns {"ok": bool, "sanitized_args": dict, "warnings": [str, ...]}.
    - "ok" is False only on injection or path traversal (severe).
    - truncation only warns, never fails.
    """
    if not isinstance(args, dict):
        return {
            "ok": False,
            "sanitized_args": {},
            "warnings": ["tool %s: args must be a dict" % (tool_name,)],
        }
    roots = allowed_roots or []
    sanitized: Dict[str, Any] = {}
    warnings: List[str] = []
    ok = True

    def _sanitize_value(key: str, value: Any) -> Any:
        nonlocal ok
        if isinstance(value, str):
            v = strip_control_chars(value)
            if len(v) > max_str_len:
                v = truncate(v, max_str_len)
                warnings.append(
                    "tool %s: arg %r truncated for length (%d -> %d)"
                    % (tool_name, key, len(value), max_str_len)
                )
            inj = detect_prompt_injection(v)
            if inj["is_injection"]:
                ok = False
                warnings.append(
                    "tool %s: arg %r suspected prompt injection: %s"
                    % (tool_name, key, ", ".join(inj["matched"]))
                )
            if key.lower() in _PATH_KEYS and roots:
                try:
                    v = validate_path(v, roots)
                except (ValueError, TypeError) as e:
                    ok = False
                    warnings.append(
                        "tool %s: arg %r invalid path: %s"
                        % (tool_name, key, e)
                    )
            return v
        if isinstance(value, list):
            return [_sanitize_value(key, item) for item in value]
        if isinstance(value, tuple):
            return tuple(_sanitize_value(key, item) for item in value)
        if isinstance(value, dict):
            return {k: _sanitize_value(str(k), item) for k, item in value.items()}
        return value

    for k, v in args.items():
        sanitized[k] = _sanitize_value(str(k), v)

    return {"ok": ok, "sanitized_args": sanitized, "warnings": warnings}
