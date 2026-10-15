"""test_tools_core.py — test cho tools-core (filesystem/terminal/git).

Chạy: python3 test_tools_core.py
Mỗi tool có ít nhất 3 assertions: happy path, invalid input, error case.
Thoát code 0 khi tất cả PASS, 1 khi có FAIL.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

# --- sys.path: tools-core mới + repo gốc (cho autonomous/gitops, security/secretscan)
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(1, "/tmp/boom_git")

from agent.src.tools import (  # noqa: E402  (sau khi chỉnh sys.path)
    append,
    bg,
    delete,
    edit,
    forget,
    git_branch_guard,
    git_diff,
    git_log,
    git_safe_commit,
    git_status,
    info,
    kill,
    kill_process,
    list_processes,
    poll_process,
    read,
    run,
    start_process,
    status,
    write,
)
from agent.src.tools import filesystem as fs_pkg  # noqa: E402
from agent.src.tools import terminal as term_pkg  # noqa: E402
from agent.src.tools.terminal import process as proc_mod  # noqa: E402

# alias "list" theo spec
fs_list = getattr(fs_pkg, "list")

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond):
    """1 assertion: đếm PASS/FAIL, không dừng ngay để chạy hết suite."""
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL: {name}")


class Cwd:
    """Context manager đổi cwd tạm thời (để test path traversal)."""

    def __init__(self, path):
        self.path = path
        self.old = None

    def __enter__(self):
        self.old = os.getcwd()
        os.chdir(self.path)
        return self

    def __exit__(self, *a):
        os.chdir(self.old)


def sh(*args, cwd=None):
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True)


# ===========================================================================
print("== filesystem ==")
with tempfile.TemporaryDirectory() as tmp, Cwd(tmp):
    # --- write ---
    r = write("a.txt", "hello world")
    check("write happy: ok", r.get("ok") is True)
    check("write happy: bytes_written", r.get("bytes_written") == 11)
    check("write happy: nội dung đúng", open("a.txt").read() == "hello world")
    r = write("../evil.txt", "x")
    check("write invalid: path traversal bị chặn", r.get("ok") is False and "traversal" in r.get("error", ""))
    r = write("", "x")
    check("write invalid: path rỗng", r.get("ok") is False)

    # --- read ---
    r = read("a.txt")
    check("read happy: ok", r.get("ok") is True)
    check("read happy: có số dòng", "1|hello world" in r.get("content", ""))
    check("read happy: total_lines", r.get("total_lines") == 1)
    r = read("khong-co.txt")
    check("read error: file không tồn tại", r.get("ok") is False)
    r = read("a.txt", offset=0)
    check("read invalid: offset=0", r.get("ok") is False)

    # --- edit (strict, 1 match + backup) ---
    r = edit("a.txt", "world", "there")
    check("edit happy: ok", r.get("ok") is True)
    check("edit happy: nội dung đã đổi", open("a.txt").read() == "hello there")
    check("edit happy: có backup", os.path.exists("a.txt.bak"))
    write("multi.txt", "foo bar foo")
    r = edit("multi.txt", "foo", "baz")
    check("edit error: ambiguous 2 matches", r.get("ok") is False and "ambiguous" in r.get("error", ""))
    r = edit("multi.txt", "zzz-khong-co", "q")
    check("edit error: not found", r.get("ok") is False)
    r = edit("../evil.txt", "a", "b")
    check("edit invalid: traversal", r.get("ok") is False)

    # --- append ---
    r = append("b.txt", "line1")
    check("append happy: tạo mới", r.get("ok") is True and open("b.txt").read() == "line1")
    r = append("b.txt", "line2")
    check("append happy: tự xuống dòng", open("b.txt").read() == "line1\nline2")
    check("append happy: bytes_added", r.get("bytes_added") == 6)
    r = append("../evil.txt", "x")
    check("append invalid: traversal", r.get("ok") is False)

    # --- delete ---
    write("del.txt", "x")
    r = delete("del.txt")
    check("delete happy: ok", r.get("ok") is True and not os.path.exists("del.txt"))
    r = delete("khong-co.txt")
    check("delete error: không tồn tại", r.get("ok") is False)
    os.makedirs("emptydir", exist_ok=True)
    r = delete("emptydir")
    check("delete happy: thư mục rỗng", r.get("ok") is True)
    os.makedirs("fulldir", exist_ok=True)
    write("fulldir/f.txt", "x")
    r = delete("fulldir")
    check("delete error: thư mục không rỗng", r.get("ok") is False)
    r = delete(".")
    check("delete invalid: từ chối xóa cwd", r.get("ok") is False)

    # --- list_dir / list ---
    r = fs_pkg.list_dir(".")
    check("list_dir happy: ok", r.get("ok") is True)
    names = [e["name"] for e in r.get("entries", [])]
    check("list_dir happy: thấy a.txt", "a.txt" in names)
    r = fs_pkg.list_dir("khong-co")
    check("list_dir error: không tồn tại", r.get("ok") is False)
    r = fs_list(".")
    check("list alias: hoạt động", r.get("ok") is True)

    # --- info ---
    r = info("a.txt")
    check("info happy: exists", r.get("ok") is True and r.get("exists") is True)
    check("info happy: size/lines", r.get("size") == 11 and r.get("lines") == 1)
    check("info happy: có mtime", isinstance(r.get("mtime"), str))
    r = info("khong-co.txt")
    check("info: không tồn tại -> exists False", r.get("ok") is True and r.get("exists") is False)
    r = info("../evil.txt")
    check("info invalid: traversal", r.get("ok") is False)

# ===========================================================================
print("== terminal.shell ==")
# --- run ---
r = run("echo hello")
check("run happy: ok", r.get("ok") is True)
check("run happy: stdout", r.get("stdout", "").strip() == "hello")
check("run happy: returncode 0", r.get("returncode") == 0)
r = run(["echo", "argv-list"])
check("run happy: argv list", r.get("stdout", "").strip() == "argv-list")

r = run("rm -rf /")
check("run blocked: rm -rf /", r.get("ok") is False and "blocked" in r.get("stderr", ""))
r = run(":(){:|:&};:")
check("run blocked: fork bomb", r.get("ok") is False and "blocked" in r.get("stderr", ""))
r = run("mkfs.ext4 /dev/sda1")
check("run blocked: mkfs", r.get("ok") is False and "blocked" in r.get("stderr", ""))

r = run("")
check("run invalid: lệnh rỗng", r.get("ok") is False)
r = run("echo hi", timeout=0)
check("run invalid: timeout=0", r.get("ok") is False)
r = run("boom_cmd_khong_ton_tai_xyz")
check("run error: command not found", r.get("ok") is False)

r = run(["sleep", "3"], timeout=1)
check("run timeout: timed_out=True", r.get("timed_out") is True)
check("run timeout: ok=False", r.get("ok") is False)

with tempfile.TemporaryDirectory() as tmp:
    r = run("pwd", cwd="/", allowed_root=tmp)
    check("run invalid: cwd ngoài allowed_root", r.get("ok") is False)

# --- bg / status / kill / forget ---
r = bg(["sleep", "30"])
check("bg happy: ok", r.get("ok") is True)
jid = r.get("job_id")
check("bg happy: có job_id", isinstance(jid, str) and jid.startswith("job_"))
r = status(jid)
check("status happy: running", r.get("ok") is True and r.get("running") is True)
r = kill(jid)
check("kill happy: ok", r.get("ok") is True)
time.sleep(0.3)
r = status(jid)
check("status sau kill: không running", r.get("running") is False)
r = forget(jid)
check("forget happy: ok", r.get("ok") is True)
r = status("job_khong_ton_tai")
check("status error: not found", r.get("ok") is False)
r = kill("job_khong_ton_tai")
check("kill error: not found", r.get("ok") is False)
r = bg("rm -rf /")
check("bg blocked: lệnh nguy hiểm", r.get("ok") is False)

# ===========================================================================
print("== terminal.process ==")
with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "processes.json")
    proc_mod.configure(ledger_file=ledger)
    r = start_process("s1", ["sleep", "30"])
    check("start_process happy: running", r.get("status") == "running")
    check("start_process happy: có pid", isinstance(r.get("pid"), int))
    r = start_process("s2", [])
    check("start_process invalid: cmd rỗng", "error" in r)
    r = list_processes()
    check("list_processes happy: ok", r.get("ok") is True)
    ids = [p["session_id"] for p in r.get("processes", [])]
    check("list_processes happy: thấy s1", "s1" in ids)
    r = poll_process("s1")
    check("poll_process happy: running", r.get("status") == "running")
    r = poll_process("khong-co")
    check("poll_process: not_found", r.get("status") == "not_found")
    r = kill_process("s1")
    check("kill_process happy: killed", r.get("status") == "killed")
    r = kill_process("khong-co")
    check("kill_process: not_found", r.get("status") == "not_found")
    r = kill_process("")
    check("kill_process invalid: session rỗng", r.get("status") == "error")

# ===========================================================================
print("== git ==")
if shutil.which("git") is None:
    print("  SKIP: không có git trên máy")
else:
    with tempfile.TemporaryDirectory() as tmp:
        sh("git", "init", "-q", cwd=tmp)
        sh("git", "branch", "-M", "main", cwd=tmp)
        sh("git", "config", "user.email", "test@boom.local", cwd=tmp)
        sh("git", "config", "user.name", "Boom Test", cwd=tmp)
        open(os.path.join(tmp, "hello.txt"), "w").write("hello\n")
        sh("git", "add", ".", cwd=tmp)
        sh("git", "commit", "-qm", "init", cwd=tmp)

        # --- git_status ---
        open(os.path.join(tmp, "hello.txt"), "w").write("hello changed\n")
        r = git_status(tmp)
        check("git_status happy: ok", r.get("ok") is True)
        kinds = {f["path"]: f for f in r.get("files", [])}
        check("git_status happy: thấy file unstaged", kinds.get("hello.txt", {}).get("unstaged") is True)
        r = git_status("/duong/dan/khong/ton/tai")
        check("git_status invalid: repo không tồn tại", r.get("ok") is False)

        # --- git_diff ---
        r = git_diff(tmp)
        check("git_diff happy: ok", r.get("ok") is True)
        check("git_diff happy: files_changed>=1", r.get("summary", {}).get("files_changed", 0) >= 1)
        r = git_diff(tmp, stat_only=False)
        check("git_diff happy: có text", isinstance(r.get("text"), str) and "hello changed" in r["text"])
        r = git_diff("/duong/dan/khong/ton/tai")
        check("git_diff invalid: repo sai", r.get("ok") is False)

        # --- git_log ---
        r = git_log(tmp)
        check("git_log happy: ok", r.get("ok") is True)
        check("git_log happy: >=1 commit", len(r.get("commits", [])) >= 1)
        check("git_log happy: đủ field", all(k in r["commits"][0] for k in ("hash", "author", "date", "message")))
        r = git_log(tmp, limit=0)
        check("git_log invalid: limit=0", r.get("ok") is False)
        r = git_log("/duong/dan/khong/ton/tai")
        check("git_log invalid: repo sai", r.get("ok") is False)

        # --- git_safe_commit ---
        r = git_safe_commit(tmp, "   ")
        check("safe_commit invalid: message rỗng", r.get("ok") is False)
        sh("git", "add", ".", cwd=tmp)
        sh("git", "commit", "-qm", "change hello", cwd=tmp)
        r = git_safe_commit(tmp, "commit khi không có gì staged")
        check("safe_commit error: không có gì staged", r.get("ok") is False)

        # secret trong staged -> bị chặn bởi secretscan
        with open(os.path.join(tmp, "leak.txt"), "w") as fh:
            fh.write('api_key = "sk-1234567890abcdef"\n')
        sh("git", "add", "leak.txt", cwd=tmp)
        r = git_safe_commit(tmp, "commit có secret")
        check("safe_commit: chặn secret", r.get("ok") is False and "secret" in r.get("error", ""))
        sh("git", "restore", "--staged", "leak.txt", cwd=tmp)
        os.remove(os.path.join(tmp, "leak.txt"))

        # happy path: file sạch
        open(os.path.join(tmp, "clean.txt"), "w").write("sạch\n")
        sh("git", "add", "clean.txt", cwd=tmp)
        r = git_safe_commit(tmp, "commit sạch")
        check("safe_commit happy: ok", r.get("ok") is True)
        check("safe_commit happy: hash 40 ký tự", len(r.get("commit_hash", "")) == 40)

        # --- git_branch_guard ---
        r = git_branch_guard(tmp)
        check("branch_guard happy: ok", r.get("ok") is True)
        check("branch_guard: main là protected", r.get("protected") is True and r.get("branch") == "main")
        check("branch_guard: có warning", isinstance(r.get("warning"), str))
        sh("git", "checkout", "-qb", "feature/test-x", cwd=tmp)
        r = git_branch_guard(tmp)
        check("branch_guard: feature/test-x không protected", r.get("protected") is False)
        r = git_branch_guard("/duong/dan/khong/ton/tai")
        check("branch_guard invalid: repo sai", r.get("ok") is False)

# --- re-export backward-compat ---
check("tools/__init__: có read", hasattr(fs_pkg, "read"))
check("tools/__init__: có run", hasattr(term_pkg, "run"))
check("tools/__init__: __all__ không rỗng", len(fs_pkg.__all__ + term_pkg.__all__) > 0)

print(f"\n===== KẾT QUẢ: {PASS} PASS, {FAIL} FAIL =====")
if FAILURES:
    print("Các case FAIL:")
    for f in FAILURES:
        print(f"  - {f}")
sys.exit(1 if FAIL else 0)
