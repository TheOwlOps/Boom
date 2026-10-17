"""Skill code-review: review tinh code Python, wrap module codereview cua Boom.

Dung module `agent/src/autonomous/codereview.py` tu repo Boom (load read-only,
khong copy code), bo sung truong `file` va `suggestion` cho moi issue.

Chay doc lap:
    python3 review.py <file.py|thu_muc> [--json] [--fail-on error]
    python3 review.py --diff-file thay_doi.diff [--json]

Bien moi truong:
    BOOM_REPO: duong dan repo Boom (mac dinh: /tmp/boom_git)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Load module codereview tu repo Boom (read-only)
# ---------------------------------------------------------------------------

def _tim_repo() -> Path:
    """Tim repo Boom theo thu tu: env BOOM_REPO -> /tmp/boom_git -> cwd."""
    for ung_vien in (os.environ.get("BOOM_REPO"), "/tmp/boom_git", os.getcwd()):
        if not ung_vien:
            continue
        p = Path(ung_vien) / "agent" / "src" / "autonomous" / "codereview.py"
        if p.is_file():
            return Path(ung_vien)
    raise FileNotFoundError(
        "khong tim thay agent/src/autonomous/codereview.py trong repo Boom "
        "(thu BOOM_REPO, /tmp/boom_git, cwd)")


def _load_codereview():
    """Load module codereview.py cua repo bang importlib (khong can sys.path)."""
    repo = _tim_repo()
    duong_dan = repo / "agent" / "src" / "autonomous" / "codereview.py"
    spec = importlib.util.spec_from_file_location("boom_codereview", duong_dan)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ---------------------------------------------------------------------------
# Schema output (pydantic)
# ---------------------------------------------------------------------------

class ReviewIssue(BaseModel):
    """1 issue review: file, line, severity, message, suggestion."""
    file: str = Field(description="duong dan file duoc review")
    line: int = Field(description="so dong xay ra issue")
    severity: Literal["error", "warning", "info"] = Field(
        description="muc do: error/warning/info")
    rule: str = Field(description="ten rule vi pham, vd: bare_except")
    message: str = Field(description="mo ta van de (tieng Viet)")
    suggestion: str = Field(description="goi y cach sua (tieng Viet)")


# ---------------------------------------------------------------------------
# Goi y sua theo tung rule
# ---------------------------------------------------------------------------

_GOI_Y: Dict[str, str] = {
    "complexity": (
        "Tach ham thanh cac ham nho, moi ham xu ly mot nhanh logic; "
        "voi chuoi if/elif dai, can nhac dung dict dispatch."),
    "long_function": (
        "Tach ham thanh nhieu ham nho, moi ham duoi 50 dong va chi lam "
        "mot viec duy nhat."),
    "long_file": (
        "Tach file thanh nhieu module nho theo trach nhiem (vd: tach lop, "
        "tach helper ra file rieng)."),
    "dead_code": (
        "Xoa bien khong dung, hoac su dung no neu logic dang thieu."),
    "bare_except": (
        "Bat exception cu the, vd: except (ValueError, TypeError) as e: "
        "thay vi except: tran."),
    "todo": (
        "Tao ticket/task theo doi va xu ly TODO nay, tranh de no ky thuat."),
    "print_stmt": (
        "Thay print() bang logging, vd: logging.info(...) de log quan ly "
        "duoc khi chay that."),
    "syntax": (
        "Sua loi cu phap o dong nay truoc khi review tiep."),
}


def _doi_sang_issue(issue_tho: Dict[str, Any], file: str) -> ReviewIssue:
    """Chuyen issue tho tu CodeReviewer sang schema chuan cua skill."""
    rule = issue_tho.get("rule", "unknown")
    return ReviewIssue(
        file=file,
        line=int(issue_tho.get("line", 1)),
        severity=issue_tho.get("severity", "info"),
        rule=rule,
        message=str(issue_tho.get("message", "")),
        suggestion=_GOI_Y.get(rule, "Xem lai doan code nay va sua theo chuan."),
    )


# ---------------------------------------------------------------------------
# API chinh cua skill
# ---------------------------------------------------------------------------

def review_source(source: str, filename: str = "<string>") -> List[ReviewIssue]:
    """Review 1 doan source code (chuoi), tra ve list ReviewIssue."""
    mod = _load_codereview()
    reviewer = mod.CodeReviewer()
    # CodeReviewer.review_file nhan path -> ghi tam ra file .py
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(source)
        tam = fh.name
    try:
        issues_tho = reviewer.review_file(tam)
    finally:
        os.unlink(tam)
    return [_doi_sang_issue(i, filename) for i in issues_tho]


def review_file(path: str | Path) -> List[ReviewIssue]:
    """Review 1 file Python, tra ve list ReviewIssue."""
    mod = _load_codereview()
    reviewer = mod.CodeReviewer()
    issues_tho = reviewer.review_file(str(path))
    return [_doi_sang_issue(i, str(path)) for i in issues_tho]


def review_files(paths: List[str | Path]) -> List[ReviewIssue]:
    """Review nhieu file, gom tat ca issues lai (sap xep theo file, dong)."""
    ket_qua: List[ReviewIssue] = []
    for p in paths:
        if str(p).endswith(".py"):
            ket_qua.extend(review_file(p))
    ket_qua.sort(key=lambda i: (i.file, i.line, i.rule))
    return ket_qua


def review_dir(root: str | Path,
               pattern: str = "*.py") -> List[ReviewIssue]:
    """Review de quy 1 thu muc (bo qua __pycache__, .venv, .git)."""
    bo_qua = {"__pycache__", ".venv", ".git", "node_modules"}
    files = [p for p in Path(root).rglob(pattern)
             if not any(ph in p.parts for ph in bo_qua)]
    return review_files([str(p) for p in sorted(files)])


def review_diff(diff_text: str) -> List[ReviewIssue]:
    """Review git diff: chi check cac dong them moi."""
    mod = _load_codereview()
    reviewer = mod.CodeReviewer()
    issues_tho = reviewer.review_diff(diff_text)
    # review_diff khong tra file rieng; trich tu message "[diff <file>]"
    ket_qua = []
    for i in issues_tho:
        msg = str(i.get("message", ""))
        file = "<diff>"
        if msg.startswith("[diff "):
            file = msg.split("]", 1)[0][6:]
            msg = msg.split("]", 1)[1].strip()
            i = dict(i, message=msg)
        ket_qua.append(_doi_sang_issue(i, file))
    return ket_qua


def issues_to_json(issues: List[ReviewIssue]) -> str:
    """Serialize issues sang JSON (tieng Viet khong escape)."""
    return json.dumps([i.model_dump() for i in issues],
                      ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Skill code-review: review tinh code Python (wrap codereview.py cua Boom)")
    parser.add_argument("paths", nargs="*", help="file .py hoac thu muc can review")
    parser.add_argument("--diff-file", default=None,
                        help="review git diff tu file thay vi review file")
    parser.add_argument("--json", action="store_true", help="in JSON thay vi markdown")
    parser.add_argument("--fail-on", default=None, choices=["error", "warning"],
                        help="exit code 1 neu co issue >= muc nay")
    args = parser.parse_args(argv)

    if args.diff_file:
        diff_text = Path(args.diff_file).read_text(encoding="utf-8")
        issues = review_diff(diff_text)
    elif args.paths:
        targets: List[str] = []
        for p in args.paths:
            pp = Path(p)
            if pp.is_dir():
                targets.extend(str(f) for f in sorted(pp.rglob("*.py")))
            else:
                targets.append(str(pp))
        issues = review_files(targets)
    else:
        parser.print_help()
        return 2

    if args.json:
        print(issues_to_json(issues))
    else:
        mod = _load_codereview()
        tho = [{"line": i.line, "severity": i.severity, "rule": i.rule,
                "message": "[%s] %s | Goi y: %s" % (i.file, i.message, i.suggestion)}
               for i in issues]
        print(mod.CodeReviewer().to_markdown(tho))

    if args.fail_on:
        muc = {"error": 0, "warning": 1}
        if any(muc.get(i.severity, 9) <= muc[args.fail_on] for i in issues):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
