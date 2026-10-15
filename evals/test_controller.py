"""Test offline cho autonomous controller + guard. Chi stdlib + sqlite3.

Chay:  python3 evals/test_controller.py   (hoac tu thu muc autonomous/)
"""

import os
import sys
import tempfile
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.realpath(os.path.join(_HERE, "..", "agent", "src"))
sys.path.insert(0, _SRC)

from autonomous.controller import run_goal, NeedsHuman  # noqa: E402
from autonomous import guard  # noqa: E402

PASS = []
FAIL = []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print("PASS  %s" % name)
    except Exception:
        FAIL.append(name)
        print("FAIL  %s" % name)
        traceback.print_exc()


# ---------------------------------------------------------------- fake helpers

class FakeStore:
    """Fake toi thieu cho task_store: create/get/save."""

    def __init__(self):
        self.tasks = {}
        self.creates = 0

    def create(self, task):
        self.creates += 1
        tid = "t%d" % len(self.tasks)
        task.id = tid
        self.tasks[tid] = task
        return tid

    def get(self, tid):
        return self.tasks.get(tid)

    def save(self, task):
        self.tasks[task.id] = task


class FakeBrain:
    def __init__(self, steps):
        self._steps = steps
        self.calls = 0

    def plan(self, goal, history):
        self.calls += 1
        return self._steps


def make_write_tool(tmp, calls):
    def write_file(path, content):
        calls.append(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"success": True, "path": path,
                "bytes_written": len(content.encode("utf-8"))}
    return write_file


# ---------------------------------------------------------------- tests

def t_done_basic():
    tmp = tempfile.mkdtemp()
    calls = []
    target = os.path.join(tmp, "hello.txt")
    tools = {"write_file": make_write_tool(tmp, calls),
             "read_file": lambda path: {"content": ""}}
    brain = FakeBrain([{"tool": "write_file",
                        "args": {"path": target, "content": "hello"},
                        "desc": "tao file hello.txt"}])
    store = FakeStore()

    def verify(state):
        ok = os.path.exists(target)
        return {"ok": ok, "detail": "exists" if ok else "missing"}

    out = run_goal("tao file hello.txt", tools, brain=brain,
                   verify_fn=verify, allowed_roots=[tmp], task_store=store)
    assert out["status"] == "done", out
    assert out["iters"] >= 1, out
    assert brain.calls >= 1
    with open(target, encoding="utf-8") as f:
        assert f.read() == "hello"
    # task log: tao 1 task, >=1 step/iter
    assert store.creates == 1, store.creates
    tid = next(iter(store.tasks))
    assert len(store.tasks[tid].steps) >= out["iters"], \
        [s.id for s in store.tasks[tid].steps]


def t_verify_fail_then_debug_fixes():
    tmp = tempfile.mkdtemp()
    calls = []
    target = os.path.join(tmp, "hello.txt")
    tools = {"write_file": make_write_tool(tmp, calls)}
    brain = FakeBrain([{"tool": "write_file",
                        "args": {"path": os.path.join(tmp, "wrong.txt"),
                                 "content": "x"},
                        "desc": "ghi sai file"}])

    def verify(state):
        return {"ok": os.path.exists(target), "detail": "check hello.txt"}

    def debug(state, vres):
        # debug_fn sua bang cach goi tool that
        tools["write_file"](path=target, content="hello fixed")
        return {"fixed": True}

    out = run_goal("tao file hello.txt", tools, brain=brain,
                   verify_fn=verify, debug_fn=debug,
                   allowed_roots=[tmp], task_store=None)
    assert out["status"] == "done", out
    assert out["iters"] == 2, out  # fail lan 1 -> debug -> pass lan 2
    with open(target, encoding="utf-8") as f:
        assert f.read() == "hello fixed"


def t_max_iters():
    tmp = tempfile.mkdtemp()
    tools = {"run_command": lambda command: {"ok": True}}
    brain = FakeBrain([{"tool": "run_command",
                        "args": {"command": "echo hi"}, "desc": "echo"}])

    def verify(state):
        return {"ok": False, "detail": "luon fail"}

    def debug(state, vres):
        return {"tried": True}  # khong fix duoc

    out = run_goal("goal khong bao gio xong", tools, brain=brain,
                   verify_fn=verify, debug_fn=debug, max_iters=3,
                   allowed_roots=[tmp])
    assert out["status"] == "max_iters", out
    assert out["iters"] == 3, out


def t_needs_human_dict():
    tmp = tempfile.mkdtemp()

    def ask_human(question):
        return {"needs_human": True, "reason": "can duyet: %s" % question}

    brain = FakeBrain([{"tool": "ask_human",
                        "args": {"question": "xoa db?"},
                        "desc": "hoi human"}])
    out = run_goal("viec can duyet", {"ask_human": ask_human},
                   brain=brain, allowed_roots=[tmp])
    assert out["status"] == "needs_human", out
    assert "duyet" in out["reason"], out


def t_needs_human_raise():
    tmp = tempfile.mkdtemp()

    def risky():
        raise NeedsHuman("too risky, can human quyet")

    brain = FakeBrain([{"tool": "risky", "args": {}, "desc": "risky"}])
    out = run_goal("viec nguy hiem", {"risky": risky},
                   brain=brain, allowed_roots=[tmp])
    assert out["status"] == "needs_human", out
    assert "human" in out["reason"].lower(), out


def t_blocked_path_tool_not_called():
    tmp = tempfile.mkdtemp()
    calls = []
    tools = {"write_file": make_write_tool(tmp, calls)}
    brain = FakeBrain([{"tool": "write_file",
                        "args": {"path": "/etc/passwd", "content": "pwned"},
                        "desc": "ghi /etc/passwd"}])
    out = run_goal("ghi file he thong", tools, brain=brain,
                   allowed_roots=[tmp])
    assert out["status"] == "blocked", out
    assert calls == [], calls  # tool KHONG duoc goi
    assert "allowed_roots" in out["reason"], out


def t_blocked_destructive_command():
    tmp = tempfile.mkdtemp()
    calls = []

    def run_command(command):
        calls.append(command)
        return {"ok": True}

    brain = FakeBrain([{"tool": "run_command",
                        "args": {"command": "rm -rf /"},
                        "desc": "xoa root"}])
    out = run_goal("pha hoai", {"run_command": run_command},
                   brain=brain, allowed_roots=[tmp])
    assert out["status"] == "blocked", out
    assert calls == [], calls


def t_guard_commands():
    bad = ["rm -rf /", "rm -rf /*", "rm -fr ~", "sudo rm -rf /",
           "dd if=/dev/zero of=/dev/sda", "dd of=/dev/sda",
           ":(){ :|:& };:", "mkfs.ext4 /dev/sda1", "shutdown -h now",
           "reboot", "format C:", "del /f /s /q C:\\Windows",
           "echo hi > /dev/sda", "chmod -R 777 /"]
    for cmd in bad:
        ok, reason = guard.is_command_safe(cmd)
        assert not ok, "dang le phai chan: %r (reason=%r)" % (cmd, reason)
    good = ["ls -la", "python3 test.py", "echo hello world",
            "git status", "pip install requests", "rm -rf ./build",
            "rm temp.txt"]
    for cmd in good:
        ok, reason = guard.is_command_safe(cmd)
        assert ok, "dang le phai cho qua: %r (%s)" % (cmd, reason)


def t_guard_paths():
    tmp = tempfile.mkdtemp()
    inside = os.path.join(tmp, "sub", "a.txt")
    assert guard.is_path_allowed(inside, [tmp])
    assert guard.is_path_allowed(tmp, [tmp])  # bang root
    assert not guard.is_path_allowed("/etc/passwd", [tmp])
    trav = os.path.join(tmp, "..", "..", "etc", "passwd")
    assert not guard.is_path_allowed(trav, [tmp]), trav
    assert not guard.is_path_allowed("", [tmp])
    assert not guard.is_path_allowed(inside, [])


def t_sanitize_tool_call():
    tmp = tempfile.mkdtemp()
    ok, _ = guard.sanitize_tool_call(
        "write_file", {"path": os.path.join(tmp, "a.txt"), "content": "x"},
        [tmp])
    assert ok
    ok, reason = guard.sanitize_tool_call(
        "run_command", {"cmd": "rm -rf /"}, [tmp])
    assert not ok, reason
    ok, reason = guard.sanitize_tool_call(
        "write_file", {"file_path": "/etc/shadow"}, [tmp])
    assert not ok, reason
    ok, _ = guard.sanitize_tool_call(
        "run_command", {"command": "ls -la"}, [tmp])
    assert ok


def t_no_brain_default_plan():
    tmp = tempfile.mkdtemp()
    seen = []

    def run_command(command):
        seen.append(command)
        return {"ok": True}

    out = run_goal("lam gi do", {"run_command": run_command},
                   brain=None,
                   verify_fn=lambda s: {"ok": True, "detail": "ok"},
                   allowed_roots=[tmp], task_store=None)
    assert out["status"] == "done", out
    assert seen == ["lam gi do"], seen


def t_never_raises():
    tmp = tempfile.mkdtemp()

    def boom():
        raise RuntimeError("kaboom")

    brain = FakeBrain([{"tool": "boom", "args": {}, "desc": "boom"}])

    def bad_verify(state):
        raise ValueError("verify hong")

    # khong debug_fn, khong verify ok -> het iters chu khong raise
    out = run_goal("g", {"boom": boom}, brain=brain,
                   verify_fn=bad_verify, max_iters=2, allowed_roots=[tmp])
    assert out["status"] in ("max_iters", "error"), out


def t_unknown_tool_no_crash():
    tmp = tempfile.mkdtemp()
    brain = FakeBrain([{"tool": "khong_ton_tai", "args": {},
                        "desc": "tool la"}])
    out = run_goal("g", {"run_command": lambda command: {"ok": True}},
                   brain=brain, max_iters=2, allowed_roots=[tmp])
    assert out["status"] in ("max_iters", "error"), out


if __name__ == "__main__":
    check("done_basic", t_done_basic)
    check("verify_fail_then_debug_fixes", t_verify_fail_then_debug_fixes)
    check("max_iters", t_max_iters)
    check("needs_human_dict", t_needs_human_dict)
    check("needs_human_raise", t_needs_human_raise)
    check("blocked_path_tool_not_called", t_blocked_path_tool_not_called)
    check("blocked_destructive_command", t_blocked_destructive_command)
    check("guard_commands", t_guard_commands)
    check("guard_paths", t_guard_paths)
    check("sanitize_tool_call", t_sanitize_tool_call)
    check("no_brain_default_plan", t_no_brain_default_plan)
    check("never_raises", t_never_raises)
    check("unknown_tool_no_crash", t_unknown_tool_no_crash)
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)
