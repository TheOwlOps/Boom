"""Integration test: controller + surgery + debugger working together.

Simulates an autonomous agent fixing a broken Python file:
goal -> controller -> tool writes buggy file -> verify fails ->
debugger fixes via surgery -> verify passes -> done.
"""
import os
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent", "src")
sys.path.insert(0, ROOT)

from autonomous.controller import run_goal
from autonomous.debugger import debug_cycle, run_python
from autonomous import guard

PASS = 0
FAIL = 0

def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {extra}")

tmp = tempfile.mkdtemp()
target = os.path.join(tmp, "app.py")

# --- tools the fake agent can use ---
calls = {"writes": 0}

def tool_write_file(**kwargs):
    calls["writes"] += 1
    # agent writes a buggy file (missing colon) on first attempt
    buggy = 'def greet()\n    print("hello")\n\ngreet()\n'
    with open(kwargs["path"], "w") as f:
        f.write(buggy)
    return {"ok": True, "path": kwargs["path"]}

def verify_fn(state):
    r = run_python(target)
    return {"ok": r["ok"], "detail": r["stderr"][:200] if not r["ok"] else "runs clean"}

def debug_fn(state, verify_result):
    res = debug_cycle(target, run_python, max_rounds=3)
    return {"fixed": res["status"] == "fixed", "detail": str(res)}

tools = {"write_file": tool_write_file}

class FakeBrain:
    def plan(self, goal, history):
        # only write once; after that, let verify/debug do their job
        if any(h.get("tool") == "write_file" for h in history):
            return []
        return [{"tool": "write_file", "args": {"path": target}, "desc": "write app"}]

result = run_goal(
    goal="create working app.py",
    tools=tools,
    brain=FakeBrain(),
    verify_fn=verify_fn,
    debug_fn=debug_fn,
    max_iters=5,
    allowed_roots=[tmp],
)

check("e2e goal done", result["status"] == "done", str(result))
check("e2e file runs clean", run_python(target)["ok"])
check("e2e file content fixed", open(target).read().startswith("def greet():"))

# --- guard still enforced through the full loop ---
def evil_tool(**kwargs):
    return {"ok": True}

r2 = run_goal(
    goal="evil",
    tools={"write_file": evil_tool},
    verify_fn=lambda s: {"ok": True, "detail": ""},
    allowed_roots=[tmp],
    max_iters=2,
)
# controller with no brain uses default plan; ensure guard blocks evil paths
ok, reason = guard.is_command_safe("rm -rf /")
check("guard blocks rm -rf /", not ok)
check("guard path traversal blocked", not guard.is_path_allowed("/etc/passwd", [tmp]))

# --- debugger + surgery do not touch file when unfixable ---
bad = os.path.join(tmp, "bad.py")
with open(bad, "w") as f:
    f.write('print(undefined_var)\n')
before = open(bad).read()
res3 = debug_cycle(bad, run_python, max_rounds=2)
after = open(bad).read()
check("unfixable status", res3["status"] == "unfixable", str(res3["status"]))
check("unfixable file untouched", before == after)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
