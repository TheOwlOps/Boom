"""Skill security-audit: kiem toan bao mat code Python.

Ket hop:
  - `agent/src/security/secretscan.py` cua Boom: quet hardcoded secret
    (load read-only tu repo, khong copy code).
  - `agent/src/security/sanitize.py` cua Boom: phat hien chuoi nghi
    prompt-injection nhung trong source.
  - AST check tu viet: ham nguy hiem (eval/exec/os.system/subprocess
    shell=True/pickle.loads), SQL string concat, thieu validate input.

Output: report [{severity, file, line, issue, fix}].

Chay doc lap:
    python3 audit.py <file.py|thu_muc> [--json] [--markdown]

Bien moi truong:
    BOOM_REPO: duong dan repo Boom (mac dinh: /tmp/boom_git)
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Load secretscan + sanitize tu repo Boom (read-only)
# ---------------------------------------------------------------------------

def _tim_repo() -> Path:
    for ung_vien in (os.environ.get("BOOM_REPO"), "/tmp/boom_git", os.getcwd()):
        if not ung_vien:
            continue
        p = Path(ung_vien) / "agent" / "src" / "security" / "secretscan.py"
        if p.is_file():
            return Path(ung_vien)
    raise FileNotFoundError(
        "khong tim thay agent/src/security/secretscan.py trong repo Boom")


def _load_repo_module(ten_file: str, ten_module: str):
    duong_dan = _tim_repo() / "agent" / "src" / "security" / ten_file
    spec = importlib.util.spec_from_file_location(ten_module, duong_dan)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ---------------------------------------------------------------------------
# Schema output
# ---------------------------------------------------------------------------

class AuditFinding(BaseModel):
    """1 phat hien kiem toan bao mat."""
    severity: Literal["high", "medium", "low"] = Field(description="muc do")
    file: str = Field(description="duong dan file")
    line: int = Field(description="so dong")
    category: str = Field(
        description="nhom: hardcoded_secret|dangerous_function|sql_injection|"
                    "missing_validation|prompt_injection_string")
    issue: str = Field(description="mo ta van de (tieng Viet)")
    fix: str = Field(description="goi y cach sua (tieng Viet)")


# ---------------------------------------------------------------------------
# AST check: ham nguy hiem + SQL concat + thieu validate input
# ---------------------------------------------------------------------------

# attr call nguy hiem: (doi tuong, ten ham) -> (severity, mo ta, cach sua)
_HAM_NGUY_HIEM: Dict[tuple, tuple] = {
    ("os", "system"): (
        "high",
        "os.system() thuc thi shell — nguy co shell injection neu chuoi chua input nguoi dung",
        "Dung subprocess.run([...], shell=False) voi danh sach doi so, "
        "hoac shlex.quote() neu bat buoc dung shell."),
    ("os", "popen"): (
        "high",
        "os.popen() thuc thi shell — nguy co shell injection",
        "Dung subprocess.run([...], capture_output=True, shell=False)."),
    ("subprocess", "call"): (
        "medium",
        "subprocess.call() — kiem tra shell=True",
        "De shell=False (mac dinh) va truyen doi so dang list."),
    ("subprocess", "run"): (
        "medium",
        "subprocess.run() — kiem tra shell=True",
        "De shell=False (mac dinh) va truyen doi so dang list."),
    ("subprocess", "Popen"): (
        "medium",
        "subprocess.Popen() — kiem tra shell=True",
        "De shell=False (mac dinh) va truyen doi so dang list."),
    ("pickle", "loads"): (
        "medium",
        "pickle.loads() voi du lieu khong tin cay -> thuc thi ma tuy y",
        "Dung json hoac format an toan khac; neu bat buoc, chi loads du lieu tu nguon tin cay."),
    ("pickle", "load"): (
        "medium",
        "pickle.load() voi du lieu khong tin cay -> thuc thi ma tuy y",
        "Dung json hoac format an toan khac."),
    ("marshal", "loads"): (
        "medium",
        "marshal.loads() voi du lieu khong tin cay -> nguy hiem",
        "Dung json thay the."),
    ("yaml", "load"): (
        "medium",
        "yaml.load() khong chi dinh Loader -> co the thuc thi ma",
        "Dung yaml.safe_load() thay the."),
}

# ten ham goi tran nguy hiem (khong qua module)
_TEN_TRAN_NGUY_HIEM = {"eval", "exec"}

# tu khoa SQL de nhan dien concat trong query
_TU_KHOA_SQL = ("SELECT", "INSERT", "UPDATE", "DELETE", "WHERE", "FROM")

# ham validate duoc cong nhan (goi truoc khi dua vao sink)
_HAM_VALIDATE = {"isinstance", "match", "search", "fullmatch", "quote",
                 "escape", "sanitize_shell_arg", "validate_path",
                 "check_tool_args", "strip_control_chars"}


def _ten_day_du(func: ast.AST) -> str:
    """'os.system' tu ast.Attribute; 'eval' tu ast.Name; '' neu khac."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        trong = _ten_day_du(func.value)
        return ("%s.%s" % (trong, func.attr)) if trong else func.attr
    return ""


def _chua_tu_khoa_sql(node: ast.AST) -> bool:
    """Kiem tra bieu thuc co chua string voi tu khoa SQL khong."""
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            upper = n.value.upper()
            if any(tk in upper for tk in _TU_KHOA_SQL):
                return True
    return False


def _la_xay_chuoi(node: ast.AST) -> bool:
    """True neu bieu thuc xay chuoi dong: f-string, +, %, .format()."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Mod)):
        return True
    if isinstance(node, ast.Call):
        ten = _ten_day_du(node.func)
        if ten.endswith(".format") or ten == "format":
            return True
    return False


def _ten_trong_bieu_thuc(node: ast.AST) -> Set[str]:
    """Tat ca ten bien (Name) xuat hien trong bieu thuc."""
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


class _KiemTraHam(ast.NodeVisitor):
    """Quet 1 ham: tim sink nguy hiem + kiem tra validate input."""

    def __init__(self, ten_ham: str, tham_so: Set[str]):
        self.ten_ham = ten_ham
        self.tham_so = tham_so
        self.findings: List[dict] = []
        # ten bien da duoc validate trong ham nay
        self.da_validate: Set[str] = set()

    # -- ghi nhan validate --
    def visit_Call(self, node: ast.Call):
        ten = _ten_day_du(node.func)
        ten_ngan = ten.split(".")[-1]
        if ten_ngan in _HAM_VALIDATE:
            for doi_so in node.args:
                self.da_validate.update(_ten_trong_bieu_thuc(doi_so))
        self.generic_visit(node)

    def visit_If(self, node: ast.If):
        # `if isinstance(x, ...)` / `if re.match(..., x)` trong dieu kien
        # cung duoc coi la validate cho nhanh re True
        for n in ast.walk(node.test):
            if isinstance(n, ast.Call):
                ten_ngan = _ten_day_du(n.func).split(".")[-1]
                if ten_ngan in _HAM_VALIDATE:
                    for doi_so in n.args:
                        self.da_validate.update(_ten_trong_bieu_thuc(doi_so))
        self.generic_visit(node)


def _quet_ast(tree: ast.AST, file: str) -> List[AuditFinding]:
    """AST check: ham nguy hiem, SQL concat, thieu validate input."""
    findings: List[AuditFinding] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        ten = _ten_day_du(node.func)
        dong = node.lineno

        # 1. eval / exec tran
        if ten in _TEN_TRAN_NGUY_HIEM:
            findings.append(AuditFinding(
                severity="high", file=file, line=dong,
                category="dangerous_function",
                issue="%s() thuc thi code dong — neu input tu nguoi dung thi "
                      "la RCE" % ten,
                fix="Tranh %s(); neu bat buoc phan tich bieu thuc, dung "
                    "ast.literal_eval() hoac ast.parse() + whitelist node."
                    % ten))
            continue

        # 2. ham nguy hiem qua module (os.system, pickle.loads...)
        for (mod, fn), (sev, mo_ta, cach_sua) in _HAM_NGUY_HIEM.items():
            if ten == "%s.%s" % (mod, fn):
                # subprocess.* chi bao khi shell=True
                if mod == "subprocess":
                    co_shell_true = any(
                        kw.arg == "shell" and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True for kw in node.keywords)
                    if not co_shell_true:
                        break
                findings.append(AuditFinding(
                    severity=sev, file=file, line=dong,
                    category="dangerous_function",
                    issue="%s: %s" % (ten, mo_ta), fix=cach_sua))
                break

        # 3. SQL string concat: execute(f"...{x}...") / execute("..." % x)
        ten_ngan = ten.split(".")[-1]
        if ten_ngan in ("execute", "executemany") and node.args:
            doi_so_dau = node.args[0]
            if _la_xay_chuoi(doi_so_dau) and _chua_tu_khoa_sql(doi_so_dau):
                findings.append(AuditFinding(
                    severity="high", file=file, line=dong,
                    category="sql_injection",
                    issue="Xay query SQL bang f-string/noi chuoi/format — "
                          "nguy co SQL injection",
                    fix="Dung parameterized query: "
                        "cursor.execute('SELECT ... WHERE id = ?', (user_id,)) "
                        "thay vi chen bien vao chuoi SQL."))

    # 4. thieu validate input: tham so di thang vao sink nguy hiem
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tham_so = {a.arg for a in node.args.args}
        kiem = _KiemTraHam(node.name, tham_so)
        kiem.visit(node)
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            ten = _ten_day_du(sub.func)
            ten_ngan = ten.split(".")[-1]
            la_sink = (ten in _TEN_TRAN_NGUY_HIEM
                       or ten in ("os.system", "os.popen")
                       or ten_ngan in ("execute", "executemany")
                       or (ten.startswith("subprocess.")
                           and any(kw.arg == "shell" and isinstance(kw.value, ast.Constant)
                                   and kw.value.value is True
                                   for kw in sub.keywords)))
            if not la_sink or not sub.args:
                continue
            doi_so_dau = sub.args[0]
            if not _la_xay_chuoi(doi_so_dau):
                continue
            bien_dung = _ten_trong_bieu_thuc(doi_so_dau) & tham_so
            chua_validate = {b for b in bien_dung if b not in kiem.da_validate}
            if chua_validate:
                findings.append(AuditFinding(
                    severity="medium", file=file, line=sub.lineno,
                    category="missing_validation",
                    issue="Tham so %s di thang vao %s qua chuoi dong ma "
                          "khong validate truoc" % (
                              ", ".join(sorted(chua_validate)), ten),
                    fix="Validate input truoc: isinstance()/regex/shlex.quote() "
                        "hoac dung sanitize.check_tool_args() cua Boom; "
                        "voi shell thi de shell=False."))

    findings.sort(key=lambda f: (f.line, f.category))
    return findings


def _quet_prompt_injection_string(tree: ast.AST, file: str,
                                  sanitize_mod) -> List[AuditFinding]:
    """Dung sanitize.detect_prompt_injection quet cac string literal."""
    findings: List[AuditFinding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and len(node.value) > 20:
            ket_qua = sanitize_mod.detect_prompt_injection(node.value)
            if ket_qua["is_injection"]:
                findings.append(AuditFinding(
                    severity="low", file=file, line=node.lineno,
                    category="prompt_injection_string",
                    issue="String literal chua pattern prompt injection: %s"
                          % ", ".join(ket_qua["matched"][:3]),
                    fix="Neu la test data thi danh dau ro (vd: bien TEST_); "
                        "neu la input nguoi dung thi chay qua "
                        "sanitize.detect_prompt_injection() luc runtime."))
    return findings


# ---------------------------------------------------------------------------
# API chinh cua skill
# ---------------------------------------------------------------------------

def audit_source(source: str, filename: str = "<string>") -> List[AuditFinding]:
    """Kiem toan 1 doan source code (chuoi)."""
    secretscan = _load_repo_module("secretscan.py", "boom_secretscan")
    sanitize_mod = _load_repo_module("sanitize.py", "boom_sanitize")
    findings: List[AuditFinding] = []

    # 1. hardcoded secret: dung secretscan quet theo dong
    for i, dong in enumerate(source.splitlines(), start=1):
        for f in secretscan._scan_line(dong, i):
            findings.append(AuditFinding(
                severity="high", file=filename, line=f["line"],
                category="hardcoded_secret",
                issue="Phat hien secret dang hardcode (%s, xem truoc: %s)"
                      % (f["type"], f["preview"]),
                fix="Dua secret ra bien moi truong (os.environ.get(...)) hoac "
                    "secret manager; xoa khoi code va xoay (rotate) secret da lo."))

    # 2+3+4. AST checks
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        findings.append(AuditFinding(
            severity="low", file=filename, line=exc.lineno or 1,
            category="dangerous_function",
            issue="Loi cu phap: %s — bo qua AST check" % exc.msg,
            fix="Sua loi cu phap truoc khi audit."))
        return findings

    findings.extend(_quet_ast(tree, filename))
    findings.extend(_quet_prompt_injection_string(tree, filename, sanitize_mod))
    findings.sort(key=lambda f: ({"high": 0, "medium": 1, "low": 2}[f.severity],
                                 f.line))
    return findings


def audit_file(path: str | Path) -> List[AuditFinding]:
    """Kiem toan 1 file."""
    path = Path(path)
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    if "\x00" in source:
        return []  # file nhi phan
    return audit_source(source, filename=str(path))


def audit_dir(root: str | Path) -> List[AuditFinding]:
    """Kiem toan de quy 1 thu muc (bo qua __pycache__, .venv, .git)."""
    bo_qua = {"__pycache__", ".venv", ".git", "node_modules"}
    findings: List[AuditFinding] = []
    for p in sorted(Path(root).rglob("*.py")):
        if any(ph in p.parts for ph in bo_qua):
            continue
        findings.extend(audit_file(p))
    return findings


def to_markdown(findings: List[AuditFinding],
                tieu_de: str = "Bao cao security audit") -> str:
    """Render findings thanh markdown tieng Viet."""
    nhan = {"high": "🔴 Nghiem trong", "medium": "🟡 Trung binh",
            "low": "🔵 Thap"}
    dem = {"high": 0, "medium": 0, "low": 0}
    for f in findings:
        dem[f.severity] += 1
    dong = ["# %s" % tieu_de, "",
            "Tong: %d phat hien (🔴 %d | 🟡 %d | 🔵 %d)"
            % (len(findings), dem["high"], dem["medium"], dem["low"]), ""]
    nhom = None
    for f in sorted(findings,
                    key=lambda x: ({"high": 0, "medium": 1, "low": 2}[x.severity],
                                   x.line)):
        if f.severity != nhom:
            nhom = f.severity
            dong += ["## %s" % nhan[nhom], ""]
        dong.append("- `%s:%d` [`%s`] %s" % (f.file, f.line, f.category, f.issue))
        dong.append("  - Cach sua: %s" % f.fix)
    if not findings:
        dong.append("Khong phat hien van de bao mat nao. 🎉")
    dong.append("")
    return "\n".join(dong)


def findings_to_json(findings: List[AuditFinding]) -> str:
    return json.dumps([f.model_dump() for f in findings],
                      ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Skill security-audit: kiem toan bao mat code Python")
    parser.add_argument("path", help="file .py hoac thu muc can audit")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--fail-on", default=None,
                        choices=["high", "medium", "low"],
                        help="exit 1 neu co finding >= muc nay")
    args = parser.parse_args(argv)

    p = Path(args.path)
    findings = audit_dir(p) if p.is_dir() else audit_file(p)

    if args.json:
        print(findings_to_json(findings))
    else:
        print(to_markdown(findings))

    if args.fail_on:
        muc = {"high": 0, "medium": 1, "low": 2}
        if any(muc[f.severity] <= muc[args.fail_on] for f in findings):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
