# -*- coding: utf-8 -*-
"""Che secret trong log/audit truoc khi ghi.

Module nay thay the gia tri secret bang placeholder an toan nhung van
giu lai 4 ky tu dau de co the debug (vi du: ghp_****[REDACTED:github]).

Nguyen tac thiet ke:
- stdlib only, khong import cheo tu secretscan (tu dinh nghia lai pattern
  de tranh circular import; giu dong bo bang tay khi them loai moi).
- KHONG BAO GIO raise: bat ky loi nao cung fail-open (tra ve du lieu goc
  + log warning), vi che secret la tang phong thu — khong duoc lam hong
  luong ghi audit.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Tuple

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pattern secret (tu dinh nghia, khong import tu secretscan de tranh circular)
# Moi pattern la tuple (re_compiled, type_label).
# Luu y: thu tu quan trong — pattern cu the hon (co prefix) dat truoc
# pattern chung (password=/api_key=) de khoi bi "an" boi nhau.
# ---------------------------------------------------------------------------
_SECRET_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # GitHub token: ghp_, gho_, ghu_, ghs_, ghr_ + 36 ky tu alnum
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"), "github"),
    # npm token: npm_ + 36 ky tu alnum (classic) hoac uuid-style
    (re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), "npm"),
    # Telegram bot token: <so id>:<35 ky tu base64-ish>
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), "telegram"),
    # AWS Access Key ID: AKIA/ASIA + 16 ky tu viet hoa/so
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), "aws"),
    # Private key PEM block (bao gom ca header/footer)
    (re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ), "privatekey"),
    # password= / passwd= / api_key= / apikey= / secret= (dang key=value)
    # Chi che value, giu lai key de doc log de hieu.
    (re.compile(
        r"(?i)\b(password|passwd|pwd|api[_-]?key|secret|access[_-]?token)"
        r"(\s*[:=]\s*)(['\"]?)([^\s'\";,}]+)\3"
    ), "credential"),
]

# ---------------------------------------------------------------------------
# Ten key nhay cam trong dict: key nao chua mot trong cac token nay
# (khong phan biet hoa thuong) thi value bi che toan bo.
# ---------------------------------------------------------------------------
_SENSITIVE_KEY_PARTS = (
    "password", "passwd", "pwd",
    "token", "secret", "api_key", "apikey",
    "auth", "credential", "private_key", "privatekey",
)

_PREFIX_LEN = 4  # so ky tu dau giu lai khi che


def _mask_value(value: str, type_label: str) -> str:
    """Che value, giu lai _PREFIX_LEN ky tu dau de debug."""
    head = value[:_PREFIX_LEN] if len(value) > _PREFIX_LEN else value[:1]
    return f"{head}****[REDACTED:{type_label}]"


def redact_text(text: Any) -> str:
    """Che secret trong mot chuoi text.

    Thay the gia tri secret bang ``<4 ky tu dau>****[REDACTED:<loai>]``.
    Input khong phai str (None, int, ...) duoc ep thanh str mot cach an toan.
    KHONG BAO GIO raise: loi thi tra ve text goc + log warning.
    """
    try:
        if text is None:
            return ""
        if not isinstance(text, str):
            text = str(text)

        def _replacer(type_label: str):
            def _sub(m: re.Match) -> str:
                # Pattern credential dang key=value: m co 4 group,
                # group(4) la value can che — chi thay value, giu nguyen key.
                # Bo qua value da duoc che truoc do (tranh che chong 2 lop).
                if type_label == "credential":
                    if "[REDACTED:" in m.group(4):
                        return m.group(0)
                    return m.group(1) + m.group(2) + m.group(3) + \
                        _mask_value(m.group(4), type_label)
                return _mask_value(m.group(0), type_label)
            return _sub

        for pattern, type_label in _SECRET_PATTERNS:
            text = pattern.sub(_replacer(type_label), text)
        return text
    except Exception as exc:  # noqa: BLE001 — fail-open la yeu cau thiet ke
        _log.warning("redact_text that bai (fail-open): %r", exc)
        try:
            return str(text)
        except Exception:
            return ""


def _is_sensitive_key(key: Any) -> bool:
    """Key co chua token nhay cam khong (so sanh khong phan biet hoa)."""
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact_dict(d: Any) -> Any:
    """De quy qua dict/list/tuple/set, che value cua key nhay cam
    va che secret trong moi chuoi con.

    - Key chua password/token/secret/api_key/auth/... → value bi che toan bo.
    - Moi chuoi nao (ke ca key, value thuong) deu duoc chay redact_text.
    - KHONG BAO GIO raise: loi thi tra ve input goc.
    """
    try:
        return _redact_any(d)
    except Exception as exc:  # noqa: BLE001 — fail-open la yeu cau thiet ke
        _log.warning("redact_dict that bai (fail-open): %r", exc)
        return d


def _redact_any(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[Any, Any] = {}
        for k, v in value.items():
            new_key = redact_text(k) if isinstance(k, str) else k
            if _is_sensitive_key(k):
                # Che toan bo value; neu la chuoi thi giu 4 ky tu dau.
                out[new_key] = _mask_value(str(v), "key") if isinstance(v, str) \
                    else "[REDACTED:key]"
            else:
                out[new_key] = _redact_any(v)
        return out
    if isinstance(value, (list, tuple)):
        redacted = [_redact_any(item) for item in value]
        return type(value)(redacted)
    if isinstance(value, set):
        return {_redact_any(item) for item in value}
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_args(tool_name: Any, args: Any) -> Dict[str, Any]:
    """Wrapper cho audit: che args cua tool truoc khi ghi log.

    Tra ve dict moi da che (khong sua dict goc). Tool name chi de phuc vu
    log/debug sau nay — hien chua dung de ap dung rule rieng theo tool.
    KHONG BAO GIO raise: loi thi tra ve dict rong an toan.
    """
    try:
        if args is None:
            return {}
        if isinstance(args, dict):
            redacted = redact_dict(args)
            return redacted if isinstance(redacted, dict) else {}
        # Args dang la thi tra ve dang dict boc ngoai de audit de ghi.
        return {"_value": redact_text(args)}
    except Exception as exc:  # noqa: BLE001 — fail-open la yeu cau thiet ke
        _log.warning("redact_args(%r) that bai (fail-open): %r", tool_name, exc)
        return {}
