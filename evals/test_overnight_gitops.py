"""Test cho agent/src/autonomous/gitops.py — chạy bằng `python3 test_gitops.py`.

Tạo git repo tạm, thao tác file thật, kiểm tra từng hàm.
Assertion nào fail sẽ raise AssertionError kèm message tiếng Việt.
"""

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_sys_src = os.path.join(_REPO, "agent", "src")
if _sys_src not in sys.path:
    sys.path.insert(0, _sys_src)

from autonomous.gitops import (GitOpsError, branch_guard, create_branch, diff_summary,
                    last_commit_info, safe_commit, status)

PASSED = [0]


def check(cond, label):
    assert cond, "FAIL: %s" % label
    PASSED[0] += 1
    print("  ok: %s" % label)


def git(repo, *args):
    r = subprocess.run(["git"] + list(args), cwd=repo,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, timeout=30)
    assert r.returncode == 0, "git %s lỗi: %s" % (args, r.stderr)
    return r.stdout


def write(repo, name, content):
    with open(os.path.join(repo, name), "w", encoding="utf-8") as fh:
        fh.write(content)


def main():
    tmp = tempfile.mkdtemp(prefix="gitops-test-")
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    git(repo, "init", "-b", "main", "-q")
    git(repo, "config", "user.email", "test@boom.local")
    git(repo, "config", "user.name", "Boom Test")
    print("repo test:", repo)

    print("\n== status/diff_summary ban đầu (repo trống) ==")
    check(status(repo) == [], "repo mới init -> status rỗng")
    ds = diff_summary(repo)
    check(ds["files_changed"] == 0 and ds["added"] == 0
          and ds["deleted"] == 0 and ds["files"] == [],
          "repo mới init -> diff_summary zeros")

    print("\n== tạo file mới chưa add ==")
    write(repo, "app.py", "def foo():\n    return 1\n")
    st = status(repo)
    check(len(st) == 1, "1 file untracked xuất hiện trong status")
    check(st[0]["path"] == "app.py", "path parse đúng")
    check(st[0]["kind"] == "untracked", "kind = untracked")
    check(st[0]["staged"] is False and st[0]["unstaged"] is True,
          "untracked: staged=False, unstaged=True")

    print("\n== safe_commit bị chặn khi chưa có gì staged ==")
    r = safe_commit(repo, "msg")
    check(r["ok"] is False and "staged" in r["error"],
          "không có file staged -> từ chối commit")

    print("\n== add + safe_commit thành công ==")
    git(repo, "add", "app.py")
    st = status(repo)
    check(st[0]["staged"] is True and st[0]["kind"] == "added",
          "sau git add: staged=True, kind=added")
    r = safe_commit(repo, "  ")  # message rỗng
    check(r["ok"] is False and "rỗng" in r["error"],
          "message rỗng -> từ chối commit")
    r = safe_commit(repo, "commit đầu tiên")
    check(r["ok"] is True, "safe_commit thành công")
    check(len(r["commit_hash"]) == 40, "commit_hash dài 40 ký tự hex")
    check(status(repo) == [], "sau commit -> status rỗng")
    ds = diff_summary(repo)
    check(ds["files_changed"] == 0, "sau commit -> diff rỗng")

    print("\n== sửa file: status 2 trạng thái, diff_summary có tên hàm ==")
    write(repo, "app.py", "def foo():\n    return 2\n\n\ndef bar():\n    return 3\n")
    git(repo, "add", "app.py")
    # Sửa tiếp sau khi add -> vừa staged vừa unstaged
    write(repo, "app.py",
          "def foo():\n    return 99\n\n\ndef bar():\n    return 3\n")
    st = status(repo)
    check(len(st) == 1, "1 file modified trong status")
    check(st[0]["staged"] is True and st[0]["unstaged"] is True,
          "vừa staged vừa unstaged")
    check(st[0]["kind"] == "modified", "kind = modified")
    ds = diff_summary(repo)
    check(ds["files_changed"] == 1, "diff_summary: 1 file changed")
    f = ds["files"][0]
    check(f["path"] == "app.py", "diff_summary path đúng")
    check(f["added"] == 5 and f["deleted"] == 1,
          "đếm dòng đúng: +5/-1 (staged+unstaged gộp)")
    check(ds["added"] == 5 and ds["deleted"] == 1,
          "tổng +lines/-lines đúng")
    check(any("foo" in name for name in f["functions"]),
          "hunk header parse ra tên hàm chứa 'foo'")

    print("\n== xóa file: kind deleted, diff đếm dòng xóa ==")
    write(repo, "gone.py", "x = 1\ny = 2\n")
    git(repo, "add", "gone.py")
    git(repo, "commit", "-qm", "them gone.py")
    os.remove(os.path.join(repo, "gone.py"))
    st = status(repo)
    deleted = [e for e in st if e["path"] == "gone.py"]
    check(len(deleted) == 1 and deleted[0]["kind"] == "deleted"
          and deleted[0]["unstaged"] is True,
          "file bị xóa: kind=deleted, unstaged=True")
    ds = diff_summary(repo)
    gp = [x for x in ds["files"] if x["path"] == "gone.py"][0]
    check(gp["deleted"] == 2 and gp["added"] == 0,
          "file bị xóa: -2 dòng, +0 dòng")
    git(repo, "checkout", "--", ".")

    print("\n== safe_commit chặn secret ==")
    write(repo, "config.py",
          'API_KEY = "sk-test-abc123def456"\n'
          'aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCY1234567890"\n')
    git(repo, "add", "config.py")
    r = safe_commit(repo, "them config")
    check(r["ok"] is False and "secret" in r["error"],
          "file chứa secret -> từ chối commit")
    git(repo, "reset", "-q", "HEAD", "config.py")
    os.remove(os.path.join(repo, "config.py"))

    print("\n== branch_guard ==")
    check(branch_guard(repo) is True,
          "đang ở main -> branch_guard True (phải tạo branch mới)")
    r = create_branch(repo, "feature/xyz_123")
    check(r["ok"] is True and r["branch"] == "feature/xyz_123",
          "create_branch hợp lệ thành công")
    check(branch_guard(repo) is False,
          "sang branch feature -> branch_guard False")
    r = create_branch(repo, "Bad Name!")
    check(r["ok"] is False and "không hợp lệ" in r["error"],
          "tên branch có ký tự lạ -> từ chối")
    r = create_branch(repo, "")
    check(r["ok"] is False, "tên branch rỗng -> từ chối")
    r = create_branch(repo, "feature/xyz_123")
    check(r["ok"] is False, "branch đã tồn tại -> git báo lỗi")
    git(repo, "checkout", "-q", "main")
    check(branch_guard(repo) is True, "quay lại main -> guard True lại")

    print("\n== last_commit_info ==")
    info = last_commit_info(repo)
    check(len(info["hash"]) == 40, "hash HEAD dài 40 ký tự")
    check(info["message"] == "them gone.py", "message HEAD đúng")
    check(info["author"] == "Boom Test", "author đúng")
    check(bool(info["date"]), "date không rỗng")

    print("\n== GitOpsError khi repo không hợp lệ ==")
    not_a_repo = os.path.join(tmp, "notrepo")
    os.makedirs(not_a_repo)
    for fn in (status, diff_summary, branch_guard, last_commit_info):
        try:
            fn(not_a_repo)
            raise AssertionError("FAIL: %s không raise trên thư mục "
                                 "không phải git repo" % fn.__name__)
        except GitOpsError:
            PASSED[0] += 1
            print("  ok: %s raise GitOpsError trên thư mục không phải repo"
                  % fn.__name__)
    try:
        last_commit_info(os.path.join(tmp, "empty"))
        raise AssertionError("FAIL: last_commit_info không raise trên "
                             "repo chưa có commit")
    except GitOpsError:
        pass
    empty = os.path.join(tmp, "empty")
    os.makedirs(empty)
    git(empty, "init", "-b", "main", "-q")
    git(empty, "config", "user.email", "t@t")
    git(empty, "config", "user.name", "t")
    try:
        last_commit_info(empty)
        raise AssertionError("FAIL: không raise trên repo chưa có commit")
    except GitOpsError as exc:
        check("chưa có commit" in str(exc),
              "repo chưa có commit -> GitOpsError tiếng Việt")

    print("\nTỔNG: %d assertions PASS" % PASSED[0])


if __name__ == "__main__":
    main()
