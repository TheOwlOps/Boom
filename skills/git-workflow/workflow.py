#!/usr/bin/env python3
"""Skill git-workflow: helper cho quy trình git của agent.

  - smart_commit(): tự sinh commit message (kiểu conventional commits)
    từ diff đang staged (hoặc chưa staged), có dry_run để xem trước
  - branch_status(): trạng thái nhánh (ahead/behind, file dirty/staged...)
  - pr_checklist(): kiểm tra nhanh trước khi tạo PR (cây sạch, không commit
    thẳng lên main, đã push, có diff so với main, không còn conflict marker)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel


class GitError(RuntimeError):
    """Lệnh git thất bại (không phải repo, lệnh sai...)."""


# ---------------------------------------------------------------------------
# Model dữ liệu (pydantic)
# ---------------------------------------------------------------------------

class CommitPlan(BaseModel):
    """Kế hoạch commit do smart_commit() sinh ra."""

    type: str  # feat | fix | chore | docs | test | ...
    scope: str  # component bị ảnh hưởng, vd: "auth"
    message: str  # message hoàn chỉnh, vd: "feat(auth): add login"
    files: list[str]  # các file sẽ được commit
    staged: bool  # True nếu diff lấy từ staged area


class BranchStatus(BaseModel):
    """Trạng thái nhánh hiện tại."""

    branch: str
    upstream: Optional[str] = None
    ahead: int = 0  # số commit local chưa push
    behind: int = 0  # số commit remote chưa pull
    staged: list[str] = []  # file đã add, chờ commit
    modified: list[str] = []  # file đã sửa nhưng chưa add
    untracked: list[str] = []  # file mới chưa track
    clean: bool = True


class CheckItem(BaseModel):
    """Một mục trong pr_checklist()."""

    name: str
    passed: bool
    detail: str = ""
    fix: str = ""  # lệnh gợi ý để sửa


# ---------------------------------------------------------------------------
# Chạy git
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: str | Path = ".") -> str:
    """Chạy git, trả về stdout; ném GitError nếu lệnh lỗi."""
    p = subprocess.run(
        ["git"] + args, cwd=str(cwd),
        capture_output=True, text=True, timeout=30,
    )
    if p.returncode != 0:
        raise GitError(f"git {' '.join(args)} thất bại: {p.stderr.strip()}")
    return p.stdout


def _parse_name_status(out: str) -> list[tuple[str, str]]:
    """Parse output 'git diff --name-status' thành [(status, path)]."""
    changes: list[tuple[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0].strip()
        # Với rename "R100\told\tnew": lấy tên mới (phần cuối).
        path = parts[-1].strip().strip('"')
        if status and path:
            changes.append((status[0], path))  # chỉ lấy ký tự đầu: A/M/D/R/...
    return changes


# ---------------------------------------------------------------------------
# smart_commit()
# ---------------------------------------------------------------------------

def _is_test_file(path: str) -> bool:
    p = path.lower()
    base = p.rsplit("/", 1)[-1]
    return (
        p.startswith("test") or "/test" in p or "/tests/" in p
        or base.startswith("test_") or base.endswith("_test.py")
    )


def _is_doc_file(path: str) -> bool:
    p = path.lower()
    return p.endswith((".md", ".rst", ".txt")) or p.startswith("docs/")


def _guess_type(changes: list[tuple[str, str]]) -> str:
    """Đoán conventional-commit type từ danh sách file thay đổi."""
    paths = [p for _, p in changes]
    if all(_is_test_file(p) for p in paths):
        return "test"
    if all(_is_doc_file(p) for p in paths):
        return "docs"
    statuses = {s for s, _ in changes}
    if statuses == {"D"}:
        return "chore"  # chỉ toàn xóa file
    if any(s == "A" for s, _ in changes):
        return "feat"  # có file mới
    return "chore"


def _guess_scope(changes: list[tuple[str, str]]) -> str:
    """Đoán scope: thư mục chung, hoặc tên file nếu chỉ 1 file."""
    if len(changes) == 1:
        # 1 file: scope = tên file không đuôi, vd: auth.py -> "auth"
        return Path(changes[0][1]).stem
    tops = set()
    for _, p in changes:
        parts = Path(p).parts
        tops.add(parts[0] if len(parts) > 1 else "")
    tops.discard("")
    if len(tops) == 1:
        return tops.pop()
    return "repo"


def smart_commit(repo: str | Path = ".", dry_run: bool = True) -> Optional[CommitPlan]:
    """Tự sinh commit message từ diff.

    Ưu tiên diff đã staged; nếu chưa có gì staged thì dùng diff chưa staged
    (và sẽ git add các file đó khi commit thật). dry_run=True (mặc định)
    chỉ trả về kế hoạch, không commit.
    Trả về None khi không có gì để commit.
    """
    staged = _parse_name_status(_git(["diff", "--cached", "--name-status"], cwd=repo))
    if staged:
        changes, is_staged = staged, True
    else:
        unstaged = _parse_name_status(_git(["diff", "--name-status"], cwd=repo))
        # File mới chưa track (git diff không thấy) cũng tính vào
        untracked = _git(["ls-files", "--others", "--exclude-standard"], cwd=repo).split()
        changes = unstaged + [("A", f) for f in untracked]
        is_staged = False
    if not changes:
        return None

    ctype = _guess_type(changes)
    scope = _guess_scope(changes)
    names = [Path(p).name for _, p in changes[:3]]
    summary = ", ".join(names)
    if len(changes) > 3:
        summary += f" (+{len(changes) - 3} file nữa)"
    message = f"{ctype}({scope}): {summary}"
    plan = CommitPlan(
        type=ctype,
        scope=scope,
        message=message,
        files=[p for _, p in changes],
        staged=is_staged,
    )
    if not dry_run:
        if not is_staged:
            # Chỉ add đúng các file đã liệt kê, không add bừa cả repo
            _git(["add", "--"] + plan.files, cwd=repo)
        _git(["commit", "-m", message], cwd=repo)
    return plan


# ---------------------------------------------------------------------------
# branch_status()
# ---------------------------------------------------------------------------

_BRANCH_RE = re.compile(r"^(?:No commits yet on )?(\S+?)(?:\.\.\.(\S+?))?(?: \[(.*)\])?$")


def branch_status(repo: str | Path = ".") -> BranchStatus:
    """Đọc trạng thái nhánh từ 'git status --porcelain=v1 -b'."""
    out = _git(["status", "--porcelain=v1", "-b"], cwd=repo)
    lines = out.splitlines()
    branch, upstream, ahead, behind = "?", None, 0, 0
    if lines:
        m = _BRANCH_RE.match(lines[0][3:])  # bỏ tiền tố "## "
        if m:
            branch = m.group(1)
            upstream = m.group(2)
            extra = m.group(3) or ""
            ma = re.search(r"ahead (\d+)", extra)
            mb = re.search(r"behind (\d+)", extra)
            ahead = int(ma.group(1)) if ma else 0
            behind = int(mb.group(1)) if mb else 0
    staged: list[str] = []
    modified: list[str] = []
    untracked: list[str] = []
    for line in lines[1:]:
        if len(line) < 4:
            continue
        x, y, path = line[0], line[1], line[3:].strip().strip('"')
        if x == "?" and y == "?":
            untracked.append(path)
        else:
            if x not in (" ", "?"):
                staged.append(path)  # thay đổi đã add
            if y not in (" ", "?"):
                modified.append(path)  # thay đổi chưa add
    return BranchStatus(
        branch=branch,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        staged=staged,
        modified=modified,
        untracked=untracked,
        clean=not (staged or modified or untracked),
    )


# ---------------------------------------------------------------------------
# pr_checklist()
# ---------------------------------------------------------------------------

_MAIN_BRANCHES = ("main", "master")


def _has_conflict_markers(repo: str | Path, files: list[str]) -> list[str]:
    """Tìm file còn sót dấu conflict '<<<<<<<'."""
    bad = []
    for f in files:
        p = Path(repo) / f
        try:
            if p.is_file() and "<<<<<<<" in p.read_text(encoding="utf-8", errors="ignore"):
                bad.append(f)
        except OSError:
            continue  # file đã bị xóa sau khi staged: bỏ qua
    return bad


def pr_checklist(repo: str | Path = ".") -> list[CheckItem]:
    """Kiểm tra nhanh các điều kiện nên đạt trước khi tạo PR."""
    st = branch_status(repo)
    items: list[CheckItem] = []

    # 1. Cây làm việc sạch
    dirty = st.staged + st.modified + st.untracked
    items.append(CheckItem(
        name="Cây làm việc sạch",
        passed=st.clean,
        detail="không còn file chưa commit" if st.clean else f"còn {len(dirty)} file: {', '.join(dirty[:5])}",
        fix="" if st.clean else "git add -A && git commit -m \"...\"",
    ))

    # 2. Không commit trực tiếp lên main/master
    on_main = st.branch in _MAIN_BRANCHES
    items.append(CheckItem(
        name="Không commit trực tiếp lên main/master",
        passed=not on_main,
        detail=f"đang ở nhánh {st.branch!r}",
        fix="" if not on_main else "git checkout -b ten-nhanh-moi",
    ))

    # 3. Nhánh đã push hết lên remote
    pushed = st.upstream is not None and st.ahead == 0
    items.append(CheckItem(
        name="Nhánh đã push hết lên remote",
        passed=pushed,
        detail=(f"ahead={st.ahead}, upstream={st.upstream}"),
        fix="" if pushed else f"git push -u origin {st.branch}",
    ))

    # 4. Có thay đổi so với main (PR trống là PR vô nghĩa)
    try:
        base = next(b for b in _MAIN_BRANCHES
                    if _git(["rev-parse", "--verify", b], cwd=repo).strip())
        stat = _git(["diff", "--stat", f"{base}...HEAD"], cwd=repo)
        has_diff = bool(stat.strip())
        detail = "có thay đổi" if has_diff else "không có gì khác so với " + base
    except (GitError, StopIteration):
        has_diff, detail = True, "không tìm thấy nhánh main/master để so sánh (bỏ qua)"
    items.append(CheckItem(
        name="Có thay đổi so với main",
        passed=has_diff,
        detail=detail,
        fix="" if has_diff else "kiểm tra lại: có thể bạn đang đứng nhầm nhánh",
    ))

    # 5. Không còn dấu conflict
    bad = _has_conflict_markers(repo, st.staged + st.modified)
    items.append(CheckItem(
        name="Không còn dấu conflict",
        passed=not bad,
        detail="sạch" if not bad else f"còn marker trong: {', '.join(bad)}",
        fix="" if not bad else "mở file và giải quyết conflict thủ công, rồi commit lại",
    ))

    return items


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="git-workflow: helper quy trình git")
    ap.add_argument("--repo", default=".", help="đường dẫn repo git")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("smart-commit", help="sinh commit message từ diff")
    c.add_argument("--no-dry-run", action="store_true", help="commit thật (mặc định chỉ xem trước)")

    sub.add_parser("branch-status", help="xem trạng thái nhánh")
    sub.add_parser("pr-checklist", help="kiểm tra trước khi tạo PR")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "smart-commit":
            plan = smart_commit(args.repo, dry_run=not args.no_dry_run)
            if plan is None:
                print("Không có gì để commit (cây làm việc sạch).")
            else:
                print(plan.model_dump_json(indent=2))
        elif args.cmd == "branch-status":
            print(branch_status(args.repo).model_dump_json(indent=2))
        elif args.cmd == "pr-checklist":
            items = pr_checklist(args.repo)
            print(json.dumps([i.model_dump() for i in items], ensure_ascii=False, indent=2))
            failed = [i.name for i in items if not i.passed]
            if failed:
                print(f"\nChưa đạt {len(failed)}/{len(items)} mục: {', '.join(failed)}")
                return 1
    except GitError as exc:
        print(f"Lỗi: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
