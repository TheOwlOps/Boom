"""Sanitize input cho tools: chong injection (shell / prompt / path traversal).

Module nay bo xung cho `autonomous/guard.py` (chan lenh pha hoai) va
`security/guardrail.py` (chan pattern nguy hiem): no sanitize gia tri input
truoc khi tool thuc thi — quote shell arg, phat hien prompt injection,
loc ky tu dieu khien, cat do dai, validate path.

Chi stdlib. Comment tieng Viet.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Pattern prompt injection (EN + VI), match case-insensitive.
# ---------------------------------------------------------------------------
# Mo ta: cac cum tu thuong duoc dung de danh lua LLM bo qua system prompt.
INJECTION_PATTERNS: List[str] = [
    # --- tieng Anh ---
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
    # --- tieng Viet ---
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

# Bien dich truoc de match nhanh, case-insensitive.
_COMPILED_INJECTION: List[tuple] = [
    (p, re.compile(re.escape(p), re.IGNORECASE)) for p in INJECTION_PATTERNS
]

# Ky tu dieu khien nguy hiem can loc: tat ca C0/C1 tru \n (0x0A) va \t (0x09).
# Luu y: \x0b (VT), \x0c (FF), \x0d (CR) deu bi loai — CR de chong
# terminal spoofing, VT/FF la control khong can thiet.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x0d\x0e-\x1f\x7f\x80-\x9f]")

# Key cua tool args thuong chua path/filesystem (giong style guard.py).
_PATH_KEYS = {
    "path", "file", "file_path", "filepath", "filename", "target", "dest", "dst",
    "src", "source", "dir", "directory", "folder", "root", "cwd", "workdir",
    "working_dir", "output", "out", "log", "logfile",
}


def sanitize_shell_arg(arg: Any) -> str:
    """Quote 1 argument an toan cho shell (shlex.quote).

    - Nhan str hoac gia tri chuyen duoc sang str (int/float/bool...).
    - Raise ValueError neu chua null byte (de phong bypass C-level / exec).
    - Raise TypeError neu khong phai string-like (list/dict/None).
    """
    if isinstance(arg, str):
        s = arg
    elif isinstance(arg, (int, float, bool)):
        s = str(arg)
    else:
        raise TypeError(
            "shell arg phai la str hoac so, nhan duoc %s" % type(arg).__name__
        )
    if "\x00" in s:
        raise ValueError("shell arg chua null byte — tu choi")
    # Validate truoc roi moi quote: shlex.quote khong bao gio loi,
    # nhung null byte da bi chan o tren.
    return shlex.quote(s)


def detect_prompt_injection(text: Any) -> Dict[str, Any]:
    """Phat hien pattern prompt injection trong text.

    Tra ve {"is_injection": bool, "matched": [pattern, ...]}.
    - Khong phai string (None/so/...): tra is_injection=False.
    - Case-insensitive, bat ca tieng Viet co dau / khong dau.
    """
    if not isinstance(text, str) or not text:
        return {"is_injection": False, "matched": []}
    matched: List[str] = []
    for pattern, rx in _COMPILED_INJECTION:
        if rx.search(text):
            matched.append(pattern)
    return {"is_injection": bool(matched), "matched": matched}


def strip_control_chars(text: Any) -> Any:
    """Loai bo ky tu dieu khien nguy hiem, GIU lai \\n va \\t.

    - Chi tac dong len str; gia tri khac tra ve nguyen ven.
    - \r (carriage return) bi loai de chong terminal spoofing
      (VD: "OK\rMALICIOUS" hien thi de lenh nhau).
    """
    if not isinstance(text, str):
        return text
    return _CONTROL_RE.sub("", text)


def truncate(text: Any, max_len: int) -> Any:
    """Cat text ve toi da max_len ky tu, them marker "[cắt bớt]".

    - max_len <= 0: raise ValueError.
    - Khong phai str hoac da ngan hon max_len: tra ve nguyen ven.
    - Neu max_len qua nho (< len(marker)): cat thuan, khong them marker.
    """
    marker = "[cắt bớt]"
    if not isinstance(max_len, int) or max_len <= 0:
        raise ValueError("max_len phai la so nguyen duong")
    if not isinstance(text, str) or len(text) <= max_len:
        return text
    if max_len <= len(marker):
        return text[:max_len]
    return text[: max_len - len(marker)] + marker


def validate_path(path: Any, allowed_roots: List[str]) -> str:
    """Chong path traversal: tra ve path tuyet doi da validate, hoac raise.

    - Resolve ".." va symlink bang os.path.realpath.
    - Path cuoi cung phai nam trong (hoac bang) it nhat 1 allowed_root,
      neu khong raise ValueError.
    - Raise TypeError neu path khong phai str; ValueError neu rong/null byte.
    """
    if not isinstance(path, str):
        raise TypeError("path phai la str, nhan duoc %s" % type(path).__name__)
    if not path:
        raise ValueError("path rong")
    if "\x00" in path:
        raise ValueError("path chua null byte — tu choi")
    if not allowed_roots:
        raise ValueError("allowed_roots rong — khong co root nao duoc phep")

    resolved = os.path.realpath(path)
    for root in allowed_roots:
        if not root or not isinstance(root, str):
            continue
        rroot = os.path.realpath(root)
        if resolved == rroot or resolved.startswith(rroot + os.sep):
            return resolved
    raise ValueError(
        "path %r nam ngoai allowed_roots (sau khi resolve: %r)"
        % (path[:160], resolved[:160])
    )


def check_tool_args(
    tool_name: str,
    args: Dict[str, Any],
    allowed_roots: List[str] | None = None,
    max_str_len: int = 100_000,
) -> Dict[str, Any]:
    """Tong hop: sanitize dict args cua 1 tool.

    Voi moi gia tri string:
      1. strip_control_chars (giu \\n\\t).
      2. truncate ve max_str_len (ghi warning neu bi cat).
      3. detect_prompt_injection (ghi warning neu khop pattern).
    Voi key nam trong _PATH_KEYS: validate_path bang allowed_roots
    (neu khong truyen allowed_roots thi bo qua buoc nay).

    Tra ve {"ok": bool, "sanitized_args": dict, "warnings": [str, ...]}.
    - "ok" False chi khi co injection hoac path traversal (loi nghiem trong).
    - truncate chi tao warning, khong fail.
    """
    if not isinstance(args, dict):
        return {
            "ok": False,
            "sanitized_args": {},
            "warnings": ["tool %s: args phai la dict" % (tool_name,)],
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
                    "tool %s: arg %r bi cat do dai (%d -> %d)"
                    % (tool_name, key, len(value), max_str_len)
                )
            inj = detect_prompt_injection(v)
            if inj["is_injection"]:
                ok = False
                warnings.append(
                    "tool %s: arg %r nghi prompt injection: %s"
                    % (tool_name, key, ", ".join(inj["matched"]))
                )
            if key.lower() in _PATH_KEYS and roots:
                try:
                    v = validate_path(v, roots)
                except (ValueError, TypeError) as e:
                    ok = False
                    warnings.append(
                        "tool %s: arg %r path khong hop le: %s"
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
