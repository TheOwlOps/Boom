"""Git workflow an toàn cho autonomous coding của Boom.

Module này cho phép agent kiểm tra trạng thái repo, xem tóm tắt diff,
commit an toàn (chặn secret + chặn commit rỗng), và tránh commit thẳng
lên nhánh protected (main/master) — trước khi thực hiện bất kỳ thao tác
git nào gây thay đổi lịch sử.

Mọi lệnh git đều chạy qua subprocess (không dùng lib ngoài).
Mọi hàm nhận `repo` là đường dẫn thư mục chứa repo.
Khi git lỗi, raise GitOpsError với message tiếng Việt.

Chỉ dùng stdlib: re, os, subprocess.
"""

import os
import re
import subprocess

# ---------------------------------------------------------------------------
# exception
# ---------------------------------------------------------------------------


class GitOpsError(Exception):
    """Lỗi thao tác git (repo không hợp lệ, lệnh git thất bại, input sai)."""
    pass


# ---------------------------------------------------------------------------
# helper nội bộ
# ---------------------------------------------------------------------------

_TIMEOUT = 30  # giây, tránh git treo khi agent chạy nền

# Tên nhánh hợp lệ: chỉ chữ thường, số, dấu gạch ngang, gạch dưới, gạch chéo.
_BRANCH_RE = re.compile(r"^[a-z0-9][a-z0-9\-_/]*$")

# Hunk header của unified diff: @@ -a,b +c,d @@ <context hàm>
_HUNK_RE = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@\s*(.*)$")

# Pattern quét secret fallback (dùng khi không import được secretscan).
_SECRET_PATTERNS = [
    ("aws_secret_key", re.compile(
        r"aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{20,}", re.I)),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("gitlab_token", re.compile(r"glpat-[A-Za-z0-9_\-]{20,}")),
    ("generic_api_key", re.compile(
        r"(?i)(api[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}['\"]?")),
    ("password_assign", re.compile(
        r"(?i)(password|passwd|pwd)\s*[:=]\s*['\"][^'\"]{4,}['\"]")),
    ("client_secret", re.compile(
        r"(?i)(client_secret|secret[_-]?key)\s*[:=]\s*['\"][^'\"]{6,}['\"]")),
    ("private_key", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("bearer_token", re.compile(
        r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}")),
]


def _git(repo: str, *args: str) -> str:
    """Chạy `git <args>` trong repo, trả về stdout.

    Raise GitOpsError (tiếng Việt) khi: repo không phải thư mục,
    không phải git repo, hoặc lệnh git trả về exit code != 0.
    """
    if not os.path.isdir(repo):
        raise GitOpsError("thư mục không tồn tại: %s" % repo)
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
        raise GitOpsError("không tìm thấy lệnh git trên máy")
    except subprocess.TimeoutExpired:
        raise GitOpsError("lệnh git bị timeout: git %s" % " ".join(args))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "lỗi không rõ").strip()
        raise GitOpsError("git %s thất bại: %s" % (" ".join(args), err))
    return proc.stdout


def _staged_paths(repo: str):
    """Danh sách file đang staged (sắp được commit)."""
    out = _git(repo, "diff", "--cached", "--name-only", "-z")
    return [p for p in out.split("\x00") if p]


def _read_staged_content(repo: str, path: str) -> str:
    """Đọc nội dung staged của file.

    Ưu tiên đọc từ worktree (nhanh, đúng khi staged == worktree);
    nếu file đã bị xóa khỏi worktree thì đọc blob staged qua `git show`.
    """
    disk_path = os.path.join(repo, path)
    if os.path.isfile(disk_path):
        try:
            with open(disk_path, "r", encoding="utf-8", errors="ignore") as fh:
                return fh.read()
        except OSError:
            pass
    # File bị xóa/đổi tên khỏi worktree: đọc trực tiếp blob staged.
    blob = _git(repo, "show", ":" + path)
    return blob


def _scan_secrets(repo: str, paths) -> list:
    """Quét secret trong các file staged.

    Thử dùng module secretscan nếu import được (ưu tiên tool có sẵn),
    ngược lại dùng pattern regex đơn giản nội bộ.
    Trả về list các phát hiện: [{path, pattern, line}].
    """
    findings = []

    # 1) Ưu tiên secretscan ngoài nếu có.
    secretscan = None
    for modname in ("secretscan", "autonomous.secretscan"):
        try:
            __import__(modname)
            import sys
            secretscan = sys.modules[modname]
            break
        except ImportError:
            continue
    if secretscan is not None:
        scan_fn = getattr(secretscan, "scan_text", None) \
            or getattr(secretscan, "scan", None)
        if callable(scan_fn):
            for path in paths:
                content = _read_staged_content(repo, path)
                for finding in scan_fn(content) or []:
                    if isinstance(finding, dict):
                        finding = dict(finding)
                        finding.setdefault("path", path)
                    findings.append(finding)
            return findings
        # secretscan tồn tại nhưng không có hàm quen thuộc -> fallback

    # 2) Fallback: pattern regex đơn giản nội bộ.
    for path in paths:
        content = _read_staged_content(repo, path)
        for lineno, line in enumerate(content.splitlines(), 1):
            for name, pattern in _SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        {"path": path, "pattern": name, "line": lineno})
                    break  # 1 dòng chỉ báo 1 lần, tránh spam
    return findings


def _head_exists(repo: str) -> bool:
    """True nếu repo đã có ít nhất 1 commit (HEAD hợp lệ)."""
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", "HEAD")
        return True
    except GitOpsError:
        return False
def _kind_from_xy(x: str, y: str) -> str:
    """Phân loại file từ 2 ký tự XY của porcelain v1."""
    if x == "?" and y == "?":
        return "untracked"
    codes = x + y
    if "U" in codes:
        return "unmerged"
    if "R" in codes:
        return "renamed"
    if "C" in codes:
        return "copied"
    if "A" in codes:
        return "added"
    if "D" in codes:
        return "deleted"
    if "M" in codes or "T" in codes:
        return "modified"
    return "unknown"


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def status(repo: str) -> list:
    """Trả về trạng thái worktree dạng list dict.

    Mỗi dict: {path, staged, unstaged, kind}.
    - staged: True nếu có thay đổi đã `git add` (cột X của porcelain)
    - unstaged: True nếu có thay đổi chưa add (cột Y), hoặc file untracked
    - kind: added | modified | deleted | renamed | copied | untracked
             | unmerged | unknown
    """
    out = _git(repo, "status", "--porcelain=v1", "-z")
    entries = []
    for token in out.split("\x00"):
        if not token or len(token) < 4:
            continue
        xy = token[:2]
        path = token[3:]
        x, y = xy[0], xy[1]
        if x == "?" and y == "?":
            staged, unstaged = False, True
        else:
            staged = x != " "
            unstaged = y != " "
        # Dòng rename có dạng "old -> new": lấy tên mới.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        entries.append({
            "path": path,
            "staged": staged,
            "unstaged": unstaged,
            "kind": _kind_from_xy(x, y),
        })
    return entries


def diff_summary(repo: str) -> dict:
    """Tóm tắt diff so với HEAD (gồm cả staged lẫn unstaged).

    Trả về:
    {
      "files_changed": int,
      "added": int,        # tổng số dòng thêm
      "deleted": int,       # tổng số dòng xóa
      "files": [
        {"path": str, "added": int, "deleted": int,
         "functions": [str]},  # hàm bị sửa, parse từ hunk header
      ],
    }
    Repo chưa có commit nào: so worktree với index (git diff thường)
    thay vì so với HEAD.
    """
    # Số dòng thêm/xóa từng file.
    # Repo chưa có commit: HEAD không tồn tại -> dùng `git diff` thường
    # (so worktree với index) thay vì `git diff HEAD`.
    diff_args = ["diff", "HEAD"] if _head_exists(repo) else ["diff"]
    numstat = _git(repo, *(diff_args + ["--numstat", "-z"]))
    counts = {}
    order = []
    for token in numstat.split("\x00"):
        if not token:
            continue
        parts = token.split("\t")
        if len(parts) != 3:
            continue
        added_s, deleted_s, path = parts
        if " -> " in path:  # rename: lấy tên mới
            path = path.split(" -> ", 1)[1].strip("{}")
        try:
            added = int(added_s) if added_s != "-" else 0
            deleted = int(deleted_s) if deleted_s != "-" else 0
        except ValueError:
            continue
        counts[path] = {"added": added, "deleted": deleted}
        if path not in order:
            order.append(path)

    # Tên hàm bị sửa: parse context sau hunk header @@ ... @@ <func>.
    funcs = {p: [] for p in order}
    current = None
    for line in _git(repo, *(diff_args + ["-U0"])).splitlines():
        if line.startswith("diff --git "):
            m = re.search(r"b/(.+)$", line)
            current = m.group(1) if m else None
            continue
        if current is None:
            continue
        m = _HUNK_RE.match(line)
        if m:
            ctx = m.group(1).strip()
            if ctx and current in funcs and ctx not in funcs[current]:
                funcs[current].append(ctx)

    files = []
    total_added = total_deleted = 0
    for path in order:
        c = counts[path]
        total_added += c["added"]
        total_deleted += c["deleted"]
        files.append({
            "path": path,
            "added": c["added"],
            "deleted": c["deleted"],
            "functions": funcs.get(path, []),
        })
    return {
        "files_changed": len(files),
        "added": total_added,
        "deleted": total_deleted,
        "files": files,
    }


def safe_commit(repo: str, message: str) -> dict:
    """Commit an toàn: chỉ commit khi đủ điều kiện.

    Điều kiện:
    1. message không rỗng (sau khi strip)
    2. có ít nhất 1 file đã staged
    3. không phát hiện secret trong nội dung staged
       (dùng secretscan nếu import được, ngược lại pattern đơn giản)

    Trả về {"ok": True, "commit_hash": str} hoặc
            {"ok": False, "error": str} (tiếng Việt, KHÔNG raise).
    """
    if not message or not message.strip():
        return {"ok": False, "error": "commit message rỗng, từ chối commit"}

    staged = _staged_paths(repo)
    if not staged:
        return {"ok": False,
                "error": "không có file nào staged, từ chối commit"}

    findings = _scan_secrets(repo, staged)
    if findings:
        first = findings[0]
        detail = "%s (pattern %s, dòng %s)" % (
            first.get("path", "?"), first.get("pattern", "?"),
            first.get("line", "?"))
        return {"ok": False,
                "error": "phát hiện secret trong file staged: %s "
                         "(+%d phát hiện khác), từ chối commit"
                         % (detail, len(findings) - 1)}

    try:
        _git(repo, "commit", "-m", message.strip())
    except GitOpsError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        commit_hash = _git(repo, "rev-parse", "HEAD").strip()
    except GitOpsError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "commit_hash": commit_hash}


def branch_guard(repo: str, protected=("main", "master")) -> bool:
    """True nếu đang ở nhánh protected -> agent PHẢI tạo branch mới,
    không được commit thẳng.

    Raise GitOpsError khi không đọc được nhánh hiện tại.
    """
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return branch in protected


def create_branch(repo: str, name: str) -> dict:
    """Tạo và checkout sang branch mới.

    Validate tên: chỉ cho [a-z0-9-_/], phải bắt đầu bằng chữ/số.
    Trả về {"ok": True, "branch": name} hoặc
            {"ok": False, "error": str} (tiếng Việt).
    """
    if not name or not _BRANCH_RE.match(name):
        return {"ok": False,
                "error": "tên branch không hợp lệ: %r "
                         "(chỉ cho phép a-z 0-9 - _ /)" % (name,)}
    if name.endswith("/") or ".." in name:
        return {"ok": False,
                "error": "tên branch không hợp lệ: %r" % (name,)}
    try:
        _git(repo, "checkout", "-b", name)
    except GitOpsError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "branch": name}


def last_commit_info(repo: str) -> dict:
    """Thông tin commit HEAD: {hash, message, author, date}.

    Raise GitOpsError khi repo chưa có commit nào.
    """
    try:
        out = _git(repo, "log", "-1",
                   "--format=%H%n%s%n%an%n%ad", "--date=iso")
    except GitOpsError:
        raise GitOpsError("repo chưa có commit nào")
    lines = out.splitlines()
    if not lines or not lines[0].strip():
        raise GitOpsError("repo chưa có commit nào")
    return {
        "hash": lines[0].strip(),
        "message": lines[1].strip() if len(lines) > 1 else "",
        "author": lines[2].strip() if len(lines) > 2 else "",
        "date": lines[3].strip() if len(lines) > 3 else "",
    }
