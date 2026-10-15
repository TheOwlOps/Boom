# -*- coding: utf-8 -*-
"""Env Protector — loc bien moi truong truoc khi truyen cho subprocess.

Muc dich: dam bao khong bao gio lot API key / secret / token / password
vao tien trinh con (log, crash dump, /proc/<pid>/environ deu doc duoc env).

Thiet ke:
- stdlib only, khong import cheo module khac.
- KHONG BAO GIO raise: loi → tra ve dict rong / gia tri da che.
- Ham chinh:
    - is_sensitive_key(name) -> bool
    - get_safe_env() -> dict
    - redact_value(value) -> str
    - safe_subprocess_env() -> dict
"""

from __future__ import annotations

import os
import re
from typing import Dict

# ---------------------------------------------------------------------------
# Pattern ten bien nhay cam (case-insensitive).
# Bat cac dang: API_KEY, SECRET, TOKEN, PASSWORD, PRIVATE_KEY va bien the
# pho bien (PASSWD, APIKEY, CREDENTIAL, ...).
# ---------------------------------------------------------------------------
_SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|secret|token|passwd|password|pwd|private[_-]?key|credential|auth[_-]?key)",
    re.IGNORECASE,
)

# So ky tu giu lai o hai dau khi redact
_KEEP = 4


def is_sensitive_key(name: str) -> bool:
    """Kiem tra ten bien moi truong co nhay cam khong (case-insensitive).

    Args:
        name: ten bien, vi du "OPENAI_API_KEY".

    Returns:
        True neu ten chua API_KEY / SECRET / TOKEN / PASSWORD /
        PRIVATE_KEY (hoac bien the).
    """
    if not name:
        return False
    return bool(_SENSITIVE_KEY_RE.search(str(name)))


def redact_value(value: str) -> str:
    """Che gia tri: giu 4 ky tu dau + 4 ky tu cuoi, giua thay bang '*'.

    Vi du: "sk-1234567890abcdef" -> "sk-1************cdef"

    Args:
        value: gia tri can che.

    Returns:
        Chuoi da che. Gia tri ngan (<= 8 ky tu) bi che toan bo.
    """
    if value is None:
        return ""
    text = str(value)
    if len(text) <= _KEEP * 2:
        # Qua ngan — che het, khong de lo thong tin do dai that
        return "*" * len(text) if text else ""
    middle_len = len(text) - _KEEP * 2
    return f"{text[:_KEEP]}{'*' * middle_len}{text[-_KEEP:]}"


def get_safe_env() -> Dict[str, str]:
    """Tra ve ban sao env da loc — LOAI BO moi bien nhay cam.

    Returns:
        dict env sach (ban copy — sua khong anh huong os.environ).
        Khong bao gio raise: loi → tra ve dict rong.
    """
    try:
        return {
            key: val
            for key, val in os.environ.items()
            if not is_sensitive_key(key)
        }
    except Exception:
        return {}


def safe_subprocess_env() -> Dict[str, str]:
    """Env sach de truyen truc tiep cho subprocess (env=...).

    Bao dam: khong chua API_KEY / SECRET / TOKEN / PASSWORD / PRIVATE_KEY.
    Bien thuong (PATH, HOME, LANG, ...) duoc giu nguyen.

    Returns:
        dict env an toan cho subprocess.Popen / subprocess.run.
    """
    return get_safe_env()
