# -*- coding: utf-8 -*-
"""Command Validator — kiem tra lenh shell truoc khi chay.

Module nay quyet dinh mot lenh shell co duoc phep chay hay khong,
dua tren denylist + kiem tra command chaining + kiem tra redirect ghi.

Thiet ke:
- stdlib + pydantic, khong import cheo module khac.
- KHONG BAO GIO raise: lenh la → allowed=False (fail-closed),
  vi day la tang phong thu — sai sot phai chan, khong duoc cho chay.
- Ham chinh: validate_command(cmd) -> CommandValidation
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Ket qua validation — Pydantic model
# ---------------------------------------------------------------------------
class CommandValidation(BaseModel):
    """Ket qua kiem tra mot lenh shell."""

    allowed: bool = Field(description="True neu lenh duoc phep chay")
    reason: str = Field(description="Ly do chan / cho phep (tieng Viet)")
    sanitized: str = Field(description="Lenh da lam sach (strip, gom whitespace)")


# ---------------------------------------------------------------------------
# Denylist: (regex, ly do). Regex duoc compile voi re.IGNORECASE.
# ---------------------------------------------------------------------------
# Tu dong them re.IGNORECASE vao moi pattern: dung ham helper _rx
# de tranh viet lap lai flag o tung dong (de doc, khoi quen).
def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


_DENYLIST: List[Tuple[re.Pattern, str]] = [
    # rm -rf /  hoac  rm -rf ~  (ke ca dang viet tat: rm -rf /*, rm -rf ~/ )
    (_rx(r"\brm\b[^\n;|&]*-[a-z]*r[a-z]*f\b[^\n;|&]*(?:^|[\s'\"`])(?:/(?:[\s'\"`]|$)|~)"), "rm -rf vao thu muc goc hoac home"),
    (_rx(r"\brm\s+(?:-[a-z]*rf[a-z]*|--recursive\s+--force)\s+(?:/|~)(?:\s|;|&&|\|\||\||$|['\"])"), "rm -rf vao thu muc goc hoac home"),
    # mkfs.* — format o dia
    (_rx(r"\bmkfs\b"), "mkfs: format o dia"),
    # dd of=/dev/... — ghi truc tiep vao thiet bi
    (_rx(r"\bdd\b[^\n;|&]*\bof\s*=\s*/dev/"), "dd ghi truc tiep vao thiet bi /dev/"),
    # fork bomb kinh dien va bien the: :(){:|:&};:
    (_rx(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:.*&?\s*\}\s*;?\s*:"), "fork bomb"),
    (_rx(r"\bfork\s*bomb\b"), "fork bomb"),
    # tat / khoi dong lai may
    (_rx(r"\bshutdown\b"), "shutdown: tat may"),
    (_rx(r"\breboot\b"), "reboot: khoi dong lai may"),
    (_rx(r"\bhalt\b"), "halt: dung may"),
    (_rx(r"\bpoweroff\b"), "poweroff: tat may"),
    (_rx(r"\binit\s+0\b"), "init 0: tat may"),
    (_rx(r"\binit\s+6\b"), "init 6: khoi dong lai may"),
    # tai script tu mang roi chay truc tiep: curl ... | sh, wget ... | sh
    (_rx(r"\bcurl\b[^|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b"), "curl | sh: chay script tu mang"),
    (_rx(r"\bwget\b[^|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b"), "wget | sh: chay script tu mang"),
    (_rx(r"\bcurl\b[^\n;|&]*\|\s*(?:sudo\s+)?python"), "curl | python: chay code tu mang"),
    (_rx(r"\bwget\b[^\n;|&]*\|\s*(?:sudo\s+)?python"), "wget | python: chay code tu mang"),
    # ghi truc tiep vao thiet bi
    (_rx(r">\s*/dev/[a-z]"), "ghi truc tiep vao thiet bi /dev/"),
]

# Dau tach lenh trong shell: ; && || | (pipe) — dung de tach thanh tung lenh con
_CHAIN_SPLIT = re.compile(r"(?:&&|\|\||;;|[;|])")

# Redirect ghi: bat cac dang  > file, >> file, 2> file, &> file ...
_REDIRECT = re.compile(
    r"(?:^|\s)(?:\d*>>?|&>)\s*"          # dau redirect
    r"(?:\"([^\"]*)\"|'([^']*)'|([^\s;|&]+))"  # target: "..." hoac '...' hoac token
)


# ---------------------------------------------------------------------------
# Ham phu tro
# ---------------------------------------------------------------------------
def _sanitize(cmd: str) -> str:
    """Lam sach lenh: strip + gom whitespace thua."""
    try:
        # shlex.split roi join lai → chuan hoa whitespace, giu nguyen quoting can thiet
        parts = shlex.split(cmd)
        return " ".join(parts)
    except ValueError:
        # quoting loi → fallback: strip + gom whitespace don gian
        return re.sub(r"\s+", " ", cmd).strip()


def _check_denylist(cmd: str) -> Optional[str]:
    """Tra ve ly do neu lenh khop denylist, None neu khong khop."""
    for pattern, reason in _DENYLIST:
        if pattern.search(cmd):
            return reason
    return None


def _split_segments(cmd: str) -> List[str]:
    """Tach lenh thanh cac lenh con theo dau ; && || |."""
    segments = [seg.strip() for seg in _CHAIN_SPLIT.split(cmd)]
    return [seg for seg in segments if seg]


def _redirect_targets(cmd: str) -> List[str]:
    """Trich tat ca target cua redirect ghi trong lenh."""
    targets: List[str] = []
    for m in _REDIRECT.finditer(cmd):
        target = m.group(1) or m.group(2) or m.group(3) or ""
        targets.append(target.strip())
    return targets


def _is_within_dir(target: str, workdir: Path) -> bool:
    """Kiem tra target co nam trong workdir khong (resolve ca symlink)."""
    # Bo qua fd-duplication dang 2>&1 (target bat dau bang &)
    if target.startswith("&"):
        return True
    try:
        resolved = (workdir / target).resolve()
        workdir_resolved = workdir.resolve()
        return resolved == workdir_resolved or workdir_resolved in resolved.parents
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# API chinh
# ---------------------------------------------------------------------------
def validate_command(cmd: str, workdir: Optional[str] = None) -> CommandValidation:
    """Kiem tra lenh shell co duoc phep chay khong.

    Args:
        cmd: lenh shell can kiem tra.
        workdir: thu muc lam viec — redirect ghi chi duoc phep trong nay.
            Mac dinh la thu muc hien tai cua process.

    Returns:
        CommandValidation(allowed, reason, sanitized). Khong bao gio raise.
    """
    base = Path(workdir) if workdir else Path(os.getcwd())

    if not cmd or not cmd.strip():
        return CommandValidation(
            allowed=False,
            reason="Lenh rong — khong co gi de chay",
            sanitized="",
        )

    sanitized = _sanitize(cmd)

    # 1) Denylist tren TOAN BO lenh (bat ca khi lenh xau nam trong chuoi chaining)
    reason = _check_denylist(cmd)
    if reason:
        return CommandValidation(
            allowed=False,
            reason=f"Bi chan boi denylist: {reason}",
            sanitized=sanitized,
        )

    # 2) Command chaining: tach thanh tung lenh con, moi lenh con phai safe
    segments = _split_segments(cmd)
    for seg in segments:
        reason = _check_denylist(seg)
        if reason:
            return CommandValidation(
                allowed=False,
                reason=f"Bi chan boi denylist trong lenh con '{seg[:60]}': {reason}",
                sanitized=sanitized,
            )

    # 3) Redirect ghi: chi cho phep ghi trong workdir
    for target in _redirect_targets(cmd):
        if not target:
            return CommandValidation(
                allowed=False,
                reason="Redirect ghi thieu ten file",
                sanitized=sanitized,
            )
        if not _is_within_dir(target, base):
            return CommandValidation(
                allowed=False,
                reason=f"Redirect ghi ra ngoai working dir: '{target}'",
                sanitized=sanitized,
            )

    return CommandValidation(
        allowed=True,
        reason="Lenh hop le — khong khop denylist, redirect nam trong working dir",
        sanitized=sanitized,
    )
