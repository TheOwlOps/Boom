"""tools.git.gitops — git tools an toàn cho Boom.

Wrap agent/src/autonomous/gitops.py thành 5 tool functions:
git_status / git_diff / git_log / git_safe_commit / git_branch_guard.

- Mọi lỗi (GitOpsError, git exit != 0, repo không hợp lệ) được bắt và trả về
  dict {"ok": False, "error": ...} thay vì raise.
- git_safe_commit quét secret bằng agent/src/security/secretscan.py
  (scan_diff trên staged diff) TRƯỚC khi commit — phát hiện secret thì từ chối.

Chỉ dùng stdlib + pydantic.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError

_TIMEOUT = 30  # giây, tránh git treo khi agent chạy nền


# ---------------------------------------------------------------------------
# nạp module gốc (chạy được cả khi repo root lẫn agent/src nằm trên sys.path)
# ---------------------------------------------------------------------------

def _import(name_primary: str, name_fallback: str, label: str):
    try:
        module = __import__(name_primary, fromlist=["*"])
        return module
    except ImportError:
        pass
    try:
        return __import__(name_fallback, fromlist=["*"])
    except ImportError as exc:
        raise ImportError(
            f"không nạp được module {label}: thử '{name_primary}' và "
            f"'{name_fallback}' đều thất bại ({exc})")


def _gitops():
    return _import("agent.src.autonomous.gitops", "autonomous.gitops", "gitops")


def _secretscan():
    return _import("agent.src.security.secretscan", "security.secretscan",
                   "secretscan")


class LogOptions(BaseModel):
    """Tham số validate cho git_log()."""

    limit: int = Field(default=10, ge=1, le=100,
                       description="số commit tối đa, 1..100")


# ---------------------------------------------------------------------------
# helper nội bộ
# ---------------------------------------------------------------------------

def _check_repo(repo: str) -> Optional[str]:
    """Validate repo: phải là chuỗi non-empty trỏ tới thư mục tồn tại.

    Trả về None nếu hợp lệ, ngược lại chuỗi lỗi.
    """
    if not isinstance(repo, str) or not repo.strip():
        return "repo phải là chuỗi đường dẫn non-empty"
    if not os.path.isdir(repo):
        return f"repo không phải thư mục tồn tại: {repo}"
    return None


def _git(repo: str, *args: str) -> Tuple[bool, str]:
    """Chạy `git <args>` trong repo. Trả về (ok, stdout_hoặc_lỗi).

    Không raise — lỗi được gói trong tuple.
    """
    try:
        proc = subprocess.run(
            ["git"] + list(args),
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_TIMEOUT,
        )
    except FileNotFoundError:
        return False, "không tìm thấy lệnh git trên máy"
    except subprocess.TimeoutExpired:
        return False, f"lệnh git bị timeout: git {' '.join(args)}"
    except OSError as exc:
        return False, str(exc)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "lỗi không rõ").strip()
        return False, f"git {' '.join(args)} thất bại: {err}"
    return True, proc.stdout


def _scan_staged_secrets(repo: str) -> List[Dict[str, Any]]:
    """Quét secret trên staged diff bằng secretscan.scan_diff.

    Trả về list findings (rỗng = sạch). Mỗi finding có {path, line, type, preview}.
    """
    secretscan = _secretscan()
    ok, diff_text = _git(repo, "diff", "--cached", "-U0")
    if not ok:
        return []  # không đọc được diff -> để gitops xử lý ở bước sau
    try:
        findings = secretscan.scan_diff(diff_text) or []
    except Exception:
        return []
    # Gắn path từng file vào finding để báo lỗi rõ ràng.
    ok, names = _git(repo, "diff", "--cached", "--name-only", "-z")
    paths = [p for p in names.split("\x00") if p] if ok else []
    for f in findings:
        if isinstance(f, dict):
            f.setdefault("path", paths[0] if len(paths) == 1 else "?")
    return findings


# ---------------------------------------------------------------------------
# 5 tool functions
# ---------------------------------------------------------------------------

def git_status(repo: str = ".") -> Dict[str, Any]:
    """Xem trạng thái worktree (file nào staged/unstaged, loại thay đổi).

    Mô tả:
        Tương đương `git status --porcelain`, trả về dạng structured.
    Args:
        repo: đường dẫn thư mục chứa repo git.
    Returns:
        {"ok": True, "files": [{"path","staged","unstaged","kind"}], "counts": {...}}
        hoặc {"ok": False, "error": str}.
    Raises:
        Không raise — mọi lỗi trả về dict.
    """
    err = _check_repo(repo)
    if err:
        return {"ok": False, "error": err}
    gitops = _gitops()
    try:
        files = gitops.status(repo)
    except gitops.GitOpsError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # phòng thủ: không để exception lạ lọt ra
        return {"ok": False, "error": f"lỗi không ngờ: {exc}"}
    counts: Dict[str, int] = {}
    for f in files:
        counts[f.get("kind", "unknown")] = counts.get(f.get("kind", "unknown"), 0) + 1
    return {"ok": True, "files": files, "counts": counts}


def git_diff(repo: str = ".", staged: bool = False,
             stat_only: bool = True, max_chars: int = 20000) -> Dict[str, Any]:
    """Xem diff: tóm tắt số liệu + (tùy chọn) nội dung diff thô.

    Mô tả:
        summary: số file đổi, tổng dòng thêm/xóa, hàm bị sửa (parse từ hunk header).
        text: nội dung unified diff (cắt ở max_chars), None khi stat_only=True.
    Args:
        repo: đường dẫn repo.
        staged: True để chỉ xem phần đã `git add` (--cached).
        stat_only: True thì chỉ trả summary, không trả text.
        max_chars: giới hạn độ dài text (1..200000).
    Returns:
        {"ok": True, "summary": {...}, "text": str|None}
        hoặc {"ok": False, "error": str}.
    Raises:
        Không raise — mọi lỗi trả về dict.
    """
    err = _check_repo(repo)
    if err:
        return {"ok": False, "error": err}
    if not isinstance(max_chars, int) or not 1 <= max_chars <= 200000:
        return {"ok": False, "error": "max_chars phải trong khoảng 1..200000"}
    gitops = _gitops()
    try:
        summary = gitops.diff_summary(repo)
    except gitops.GitOpsError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"lỗi không ngờ: {exc}"}
    text = None
    if not stat_only:
        args = ["diff", "--cached"] if staged else ["diff", "HEAD"]
        ok, out = _git(repo, *args)
        if not ok:
            return {"ok": False, "error": out}
        text = out[:max_chars]
        if len(out) > max_chars:
            text += f"\n...[truncated at {max_chars} chars]..."
    return {"ok": True, "summary": summary, "text": text}


def git_log(repo: str = ".", limit: int = 10) -> Dict[str, Any]:
    """Xem lịch sử commit gần nhất.

    Mô tả:
        Trả về *limit* commit mới nhất: hash, author, date, message.
    Args:
        repo: đường dẫn repo.
        limit: số commit (1..100).
    Returns:
        {"ok": True, "commits": [{"hash","author","date","message"}]}
        hoặc {"ok": False, "error": str}.
    Raises:
        Không raise — mọi lỗi trả về dict.
    """
    err = _check_repo(repo)
    if err:
        return {"ok": False, "error": err}
    try:
        opts = LogOptions(limit=limit)
    except ValidationError as exc:
        return {"ok": False, "error": f"limit không hợp lệ: {exc.errors()[0]['msg']}"}
    ok, out = _git(repo, "log", f"-n{opts.limit}",
                   "--format=%H%x1f%an%x1f%ad%x1f%s", "--date=iso")
    if not ok:
        return {"ok": False, "error": out}
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        commits.append({"hash": parts[0], "author": parts[1],
                        "date": parts[2], "message": parts[3]})
    return {"ok": True, "commits": commits}


def git_safe_commit(repo: str, message: str) -> Dict[str, Any]:
    """Commit an toàn: chặn message rỗng, chặn commit rỗng, chặn secret.

    Mô tả:
        1. message rỗng -> từ chối.
        2. Quét secret trên staged diff bằng secretscan.scan_diff;
           phát hiện secret -> từ chối (kèm vị trí).
        3. Gọi gitops.safe_commit (kiểm tra lại: có file staged không,
           quét secret fallback) rồi commit.
    Args:
        repo: đường dẫn repo.
        message: commit message.
    Returns:
        {"ok": True, "commit_hash": str}
        hoặc {"ok": False, "error": str} (tiếng Việt).
    Raises:
        Không raise — mọi lỗi trả về dict.
    """
    err = _check_repo(repo)
    if err:
        return {"ok": False, "error": err}
    if not isinstance(message, str) or not message.strip():
        return {"ok": False, "error": "commit message rỗng, từ chối commit"}
    findings = _scan_staged_secrets(repo)
    if findings:
        first = findings[0] if isinstance(findings[0], dict) else {}
        detail = "%s (loại %s, dòng %s)" % (
            first.get("path", "?"), first.get("type", "?"), first.get("line", "?"))
        return {"ok": False,
                "error": "phát hiện secret trong file staged: %s "
                         "(+%d phát hiện khác), từ chối commit"
                         % (detail, len(findings) - 1)}
    gitops = _gitops()
    try:
        result = gitops.safe_commit(repo, message)
    except gitops.GitOpsError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"lỗi không ngờ: {exc}"}
    # gitops.safe_commit đã trả dict {"ok": ...} — chuyển tiếp nguyên vẹn.
    if isinstance(result, dict):
        return result
    return {"ok": False, "error": f"kết quả lạ từ gitops: {result!r}"}


def git_branch_guard(repo: str = ".",
                     protected: Tuple[str, ...] = ("main", "master")) -> Dict[str, Any]:
    """Kiểm tra nhánh hiện tại có phải nhánh protected không.

    Mô tả:
        Agent KHÔNG được commit thẳng lên nhánh protected (main/master) —
        phải tạo branch mới. Tool này cho agent biết mình đang ở đâu.
    Args:
        repo: đường dẫn repo.
        protected: tuple tên nhánh được bảo vệ.
    Returns:
        {"ok": True, "branch": str, "protected": bool, "warning": str|None}
        hoặc {"ok": False, "error": str}.
    Raises:
        Không raise — mọi lỗi trả về dict.
    """
    err = _check_repo(repo)
    if err:
        return {"ok": False, "error": err}
    if not isinstance(protected, (tuple, list)) or not protected:
        return {"ok": False, "error": "protected phải là tuple/list non-empty"}
    gitops = _gitops()
    try:
        on_protected = gitops.branch_guard(repo, tuple(protected))
    except gitops.GitOpsError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"lỗi không ngờ: {exc}"}
    ok, branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    branch_name = branch.strip() if ok else "?"
    warning = None
    if on_protected:
        warning = (f"đang ở nhánh protected '{branch_name}': "
                   "không commit thẳng, hãy tạo branch mới")
    return {"ok": True, "branch": branch_name, "protected": on_protected,
            "warning": warning}


__all__ = ["git_status", "git_diff", "git_log", "git_safe_commit",
           "git_branch_guard"]
