"""Guard rails cho autonomous controller: chan lenh pha hoai + sandbox path.

Chi stdlib. Khong bao gio raise vi input xau — tra ve (False, ly_do).
"""

import os
import re
from typing import Dict, List, Tuple

# (regex, ly_do) — kiem tra bang re.search, IGNORECASE khi hop ly.
DESTRUCTIVE_PATTERNS: List[Tuple[str, str]] = [
    # rm de quy vao /, /*, ~, *, $HOME (moi thu tu flag: -rf, -fr, --recursive --force, ...)
    (r"\brm\s+(?:-[a-zA-Z]*r[a-zA-Z]*|--recursive\b)[^\n;|&]*?"
     r"(?<![\w/])(?:/(?:\*|[\s;|&]|$)|\*(?:[\s;|&]|$)|~(?:[\s;|&]|$)|\$HOME\b)",
     "recursive rm tren /, ~, * hoac $HOME"),
    # rm -rf / duoi dang don gian nhat (phong regex tren miss do word boundary)
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+[\"']?/(?:[\"'\s;|&]|$)",
     "rm -rf /"),
    # fork bomb
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;?\s*:",
     "fork bomb"),
    # dd ghi ra device
    (r"\bdd\b[^\n;|&]*\b(if|of)\s*=\s*/dev/",
     "dd doc/ghi truc tiep device"),
    # mkfs
    (r"\bmkfs(\.\w+)?\b",
     "mkfs (format filesystem)"),
    # shutdown / reboot / poweroff / halt / init 0|6
    (r"\b(shutdown|reboot|poweroff|halt)\b",
     "shutdown/reboot/poweroff/halt"),
    (r"\binit\s+[06]\b",
     "init 0/6 (shutdown/reboot)"),
    # format o dia (windows `format C:` / unix `format /dev/...`)
    (r"\bformat\b\s*(?:[a-zA-Z]:|/dev/)",
     "format o dia"),
    # windows mass delete: del /f /s /q C:\...
    (r"\bdel\b(?:\s+/[a-z]+)+\s+[a-zA-Z]:\\",
     "del /f /s /q tren o dia Windows"),
    (r"\brmdir\b\s+/s\b.*[a-zA-Z]:\\",
     "rmdir /s tren o dia Windows"),
    # shred / wipe device
    (r"\b(shred|wipefs|blockdev)\b[^\n;|&]*/dev/",
     "shred/wipefs tren device"),
    # redirect ghi de device: > /dev/sda, >> /dev/null thi ok nhung /dev/sd* thi chan
    (r">{1,2}\s*/dev/(sd[a-z]+|hd[a-z]+|nvme\d+n\d+|vd[a-z]+|mmcblk\d+)",
     "redirect ghi de len o dia"),
    # chmod/chown de quy tren /
    (r"\b(chmod|chown)\b[^\n;|&]*-R[^\n;|&]*\s+[\"']?/(?:[\"'\s;|&]|$)",
     "chmod/chown -R tren /"),
    # mv /* vao /dev/null (xoa toan bo fs)
    (r"\bmv\b[^\n;|&]*/\*?[^\n;|&]*\s+/dev/null",
     "mv toan bo filesystem vao /dev/null"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), reason) for p, reason in DESTRUCTIVE_PATTERNS]


def is_command_safe(cmd: str) -> Tuple[bool, str]:
    """(True, "") neu lenh an toan; (False, ly_do) neu khop pattern pha hoai."""
    if not isinstance(cmd, str):
        return False, "command khong phai string"
    for rx, reason in _COMPILED:
        if rx.search(cmd):
            return False, reason
    return True, ""


def is_path_allowed(path: str, allowed_roots: List[str]) -> bool:
    """True neu realpath(path) nam trong (hoac bang) it nhat 1 allowed_root.

    Chong ../ traversal nho os.path.realpath. Path khong ton tai van check
    duoc (realpath khong doi hoi file ton tai).
    """
    if not path or not isinstance(path, str) or not allowed_roots:
        return False
    try:
        rp = os.path.realpath(path)
    except Exception:
        return False
    for root in allowed_roots:
        if not root or not isinstance(root, str):
            continue
        try:
            rr = os.path.realpath(root)
        except Exception:
            continue
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


# Arg key chua command shell (can kiem tra destructive)
_CMD_KEYS = {"cmd", "command", "shell", "script", "code", "bash", "sh"}
# Arg key chua path/filesystem (can kiem tra sandbox)
_PATH_KEYS = {
    "path", "file", "file_path", "filepath", "filename", "target", "dest", "dst",
    "src", "source", "dir", "directory", "folder", "root", "cwd", "workdir",
    "working_dir", "output", "out", "log", "logfile",
}


def _check_value(tool_name: str, key: str, value: object,
                 allowed_roots: List[str]) -> Tuple[bool, str]:
    if isinstance(value, str):
        if key in _CMD_KEYS:
            ok, reason = is_command_safe(value)
            if not ok:
                return False, "tool %s: lenh khong an toan (%s): %r" % (tool_name, reason, value[:120])
        if key in _PATH_KEYS:
            if not is_path_allowed(value, allowed_roots):
                return False, "tool %s: path %r nam ngoai allowed_roots" % (tool_name, value[:160])
    elif isinstance(value, (list, tuple)):
        for v in value:
            ok, reason = _check_value(tool_name, key, v, allowed_roots)
            if not ok:
                return False, reason
    elif isinstance(value, dict):
        for k, v in value.items():
            kk = str(k).lower()
            if kk in _CMD_KEYS or kk in _PATH_KEYS:
                ok, reason = _check_value(tool_name, kk, v, allowed_roots)
                if not ok:
                    return False, reason
    return True, ""


def sanitize_tool_call(tool_name: str, args: Dict[str, object],
                       allowed_roots: List[str]) -> Tuple[bool, str]:
    """Kiem tra 1 tool call truoc khi thuc thi.

    - arg key thuoc _CMD_KEYS (cmd/command/...): quet DESTRUCTIVE_PATTERNS.
    - arg key thuoc _PATH_KEYS (path/file/...): phai nam trong allowed_roots.
    Tra (True, "") neu pass; (False, ly_do) neu vi pham.
    """
    if not isinstance(args, dict):
        return False, "tool %s: args phai la dict" % (tool_name,)
    for key, value in args.items():
        k = str(key).lower()
        if k in _CMD_KEYS or k in _PATH_KEYS:
            ok, reason = _check_value(tool_name, k, value, allowed_roots or [])
            if not ok:
                return False, reason
    return True, ""
