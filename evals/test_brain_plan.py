"""Tests for Brain.plan() — LLM planning with heuristic fallback.

Staging layout mirrors the repo (agent/src/orchestration/brain.py); the
heavy deps (react/dag/mass) are stubbed because plan() never touches them.
"""
import asyncio
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ---- stub heavy deps before importing brain -------------------------------
def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod

_stub("agent.src.loop.react", ReActLoop=object)
_stub("agent.src.orchestration.dag", DAGScheduler=object, TaskNode=object)


class _FakeSwarm:
    def resolve(self, r):
        return None


_stub(
    "agent.src.orchestration.mass",
    SPECIALISTS={},
    AgentRole=object,
    SpecialistAgent=object,
    MASSSwarm=_FakeSwarm,
)

from agent.src.orchestration.brain import Brain  # noqa: E402


# ---- fakes -----------------------------------------------------------------
class FakeLoop:
    """Mimics ReActLoop: async step(), carries .tools, records prompts."""

    def __init__(self, reply_text="", reply_exc=None, tools=None):
        self._reply_text = reply_text
        self._reply_exc = reply_exc
        self.tools = dict(tools or {})
        self.prompts = []
        self.system_prompts = []

    async def step(self, prompt):
        self.prompts.append(prompt)
        if self._reply_exc is not None:
            raise self._reply_exc
        return {"action": self._reply_text}


def make_factory(loop):
    def factory(system_prompt=None, tools=None, session_id=None):
        loop.system_prompts.append(system_prompt)
        if tools is not None:
            loop.tools = dict(tools)
        return loop

    return factory


TOOLS = ["read_file", "write_file", "run_command", "pytest", "grep"]

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS: %s" % name)
    else:
        FAIL += 1
        print("FAIL: %s %s" % (name, detail))


def shape_ok(steps, allowed):
    if not isinstance(steps, list) or not steps:
        return False
    for s in steps:
        if not isinstance(s, dict):
            return False
        if set(s.keys()) != {"tool", "args", "desc"}:
            return False
        if s["tool"] not in allowed:
            return False
        if not isinstance(s["args"], dict):
            return False
    return True


# 1. LLM returns valid JSON -> parsed steps, correct shape -------------------
loop = FakeLoop(reply_text=json.dumps([
    {"tool": "read_file", "args": {"path": "a.py"}, "desc": "read a"},
    {"tool": "pytest", "args": {}, "desc": "run tests"},
]))
b = Brain(make_factory(loop))
steps = b.plan("fix bug in a.py", [], TOOLS)
check("llm-json parsed", shape_ok(steps, TOOLS), steps)
check("llm-json content",
      steps[0]["tool"] == "read_file"
      and steps[0]["args"] == {"path": "a.py"},
      steps)

# 2. history is passed into the LLM prompt -----------------------------------
loop2 = FakeLoop(reply_text="[]")
b2 = Brain(make_factory(loop2))
b2.plan("do stuff", [{"iter": 1, "tool": "read_file", "ok": True}], TOOLS)
prompt_text = loop2.prompts[0] if loop2.prompts else ""
check("history in prompt",
      "read_file" in prompt_text and "HISTORY" in prompt_text,
      prompt_text[:120])

# 3. LLM returns markdown-fenced JSON -> still parsed -------------------------
loop3 = FakeLoop(reply_text=(
    "```json\n"
    '[{"tool": "grep", "args": {"pattern": "login"}, "desc": "find"}]\n'
    "```"
))
b3 = Brain(make_factory(loop3))
steps3 = b3.plan("search login", [], TOOLS)
check("fenced json parsed",
      shape_ok(steps3, TOOLS) and steps3[0]["tool"] == "grep", steps3)

# 4. LLM returns unknown tools -> dropped -> heuristic fallback ---------------
loop4 = FakeLoop(reply_text=json.dumps([
    {"tool": "teleport", "args": {}, "desc": "nope"},
]))
b4 = Brain(make_factory(loop4))
steps4 = b4.plan("chạy pytest", [], TOOLS)
check("unknown tool filtered, heuristic fallback",
      shape_ok(steps4, TOOLS) and steps4[0]["tool"] == "run_command"
      and steps4[0]["args"]["command"] == "pytest", steps4)

# 5. LLM garbage -> heuristic fallback ----------------------------------------
loop5 = FakeLoop(reply_text="xin chào, tôi không biết JSON")
b5 = Brain(make_factory(loop5))
steps5 = b5.plan("chạy python3 main.py", [], TOOLS)
check("garbage llm -> heuristic run",
      shape_ok(steps5, TOOLS) and steps5[0]["tool"] == "run_command"
      and steps5[0]["args"]["command"] == "python3 main.py",
      steps5)

# 6. LLM raises -> heuristic fallback ------------------------------------------
loop6 = FakeLoop(reply_exc=RuntimeError("boom"))
b6 = Brain(make_factory(loop6))
steps6 = b6.plan("sửa lỗi ở app.py", [], TOOLS)
check("llm raises -> heuristic fix",
      shape_ok(steps6, TOOLS)
      and steps6[0]["tool"] == "read_file"
      and steps6[0]["args"] == {"path": "app.py"},
      steps6)

# 7. make_loop raises immediately -> heuristic fallback, never raises ----------
def bad_factory(**kw):
    raise ConnectionError("nope")

b7 = Brain(bad_factory)
try:
    steps7 = b7.plan("chạy pytest", [], TOOLS)
    check("factory raises -> heuristic, no raise",
          shape_ok(steps7, TOOLS) and steps7[0]["tool"] == "run_command"
          and steps7[0]["args"]["command"] == "pytest", steps7)
except Exception as e:
    check("factory raises -> heuristic, no raise", False, repr(e))

# 8. heuristic intents ----------------------------------------------------------
INTENT_CASES = [
    ("chạy pytest cho module x", "pytest"),
    ("sửa lỗi đăng nhập ở auth.py", "read_file"),   # fix -> read file first
    ("chạy python3 main.py", "run_command"),
    ("đọc file config.yaml", "read_file"),
    ("tìm hàm login", "grep"),
    ("sửa file README.md", "read_file"),            # edit -> read first
    ("làm gì đó chung chung", "run_command"),       # generic -> run goal
]
for goal, want_tool in INTENT_CASES:
    b8 = Brain(bad_factory)  # force heuristic
    st = b8.plan(goal, [], TOOLS)
    check("heuristic %r -> %s" % (goal, want_tool),
          shape_ok(st, TOOLS) and st[0]["tool"] == want_tool, st)

# 9. fix intent extracts the right file -----------------------------------------
b9 = Brain(bad_factory)
st9 = b9.plan("sửa lỗi ở agent/src/auth.py", [], TOOLS)
check("fix extracts file path",
      st9 and st9[0]["args"].get("path") == "agent/src/auth.py", st9)

# 10. read intent with file ------------------------------------------------------
b10 = Brain(bad_factory)
st10 = b10.plan("đọc file notes.txt", [], TOOLS)
check("read intent",
      shape_ok(st10, TOOLS) and st10[0]["args"] == {"path": "notes.txt"},
      st10)

# 11. run intent strips the verb --------------------------------------------------
b11 = Brain(bad_factory)
st11 = b11.plan("chạy python3 -m pytest -x", [], TOOLS)
check("run strips verb",
      shape_ok(st11, TOOLS)
      and st11[0]["args"]["command"] == "python3 -m pytest -x",
      st11)

# 12. empty goal -> [] (controller default applies) ---------------------------------
b12 = Brain(bad_factory)
check("empty goal -> []", b12.plan("", [], TOOLS) == [])
check("blank goal -> []", b12.plan("   ", [], TOOLS) == [])

# 13. no tools -> [] -----------------------------------------------------------------
b13 = Brain(bad_factory)
check("no tools -> []", b13.plan("do x", [], []) == [])

# 14. controller calling convention: plan(goal, history) positional -----------------
loop14 = FakeLoop(reply_text=json.dumps([
    {"tool": "run_command", "args": {"command": "ls"}, "desc": "list"},
]), tools={"run_command": object(), "read_file": object()})
b14 = Brain(make_factory(loop14))
st14 = b14.plan("list files", [{"iter": 1, "tool": "x", "ok": True}])
check("positional (goal, history) works",
      shape_ok(st14, TOOLS), st14)

# 15. available_tools=None probes via make_loop --------------------------------------
loop15 = FakeLoop(reply_text=json.dumps([
    {"tool": "run_command", "args": {"command": "ls"}, "desc": "list"},
]), tools={"run_command": object(), "read_file": object()})
b15 = Brain(make_factory(loop15))
st15 = b15.plan("list files")
check("available_tools=None probes factory",
      shape_ok(st15, ["run_command", "read_file"]), st15)

# 16. plan inside a running event loop -> heuristic (no deadlock) ---------------------
async def _in_loop():
    loop16 = FakeLoop(reply_text=json.dumps([
        {"tool": "pytest", "args": {}, "desc": "t"},
    ]))
    b16 = Brain(make_factory(loop16))
    return b16.plan("chạy pytest", [], TOOLS)

st16 = asyncio.run(_in_loop())
check("running loop -> heuristic, no deadlock",
      shape_ok(st16, TOOLS) and st16[0]["tool"] == "run_command"
      and st16[0]["args"]["command"] == "pytest", st16)

# 17. LLM returns {"steps": [...]} wrapper -> parsed ----------------------------------
loop17 = FakeLoop(reply_text=json.dumps({
    "steps": [{"tool": "read_file", "args": {"path": "b.py"}, "desc": "r"}],
}))
b17 = Brain(make_factory(loop17))
st17 = b17.plan("read b", [], TOOLS)
check("steps-wrapper parsed",
      shape_ok(st17, TOOLS) and st17[0]["args"] == {"path": "b.py"}, st17)

# 18. heuristic never emits unknown tools ----------------------------------------------
b18 = Brain(bad_factory)
for goal in ["chạy test", "sửa lỗi x.py", "đọc a.py", "tìm y", "zzz"]:
    st = b18.plan(goal, [], ["weird_tool"])
    check("heuristic respects tool list %r" % goal,
          st == [] or all(s["tool"] == "weird_tool" for s in st), st)

# 18b. explicit test command vs Vietnamese prose -------------------------------
b18b = Brain(bad_factory)
st_explicit = b18b.plan("chạy pytest -x", [], TOOLS)
check("explicit 'pytest -x' runs verbatim",
      shape_ok(st_explicit, TOOLS)
      and st_explicit[0]["tool"] == "run_command"
      and st_explicit[0]["args"]["command"] == "pytest -x",
      st_explicit)
st_prose = b18b.plan("chạy pytest cho module x", [], TOOLS)
check("prose 'pytest cho module x' uses pytest tool",
      shape_ok(st_prose, TOOLS) and st_prose[0]["tool"] == "pytest",
      st_prose)
st_path = b18b.plan("chạy pytest tests/a.py", [], TOOLS)
check("explicit 'pytest tests/a.py' runs verbatim",
      shape_ok(st_path, TOOLS)
      and st_path[0]["args"]["command"] == "pytest tests/a.py",
      st_path)

# 19. plan never raises on hostile input -----------------------------------------------
b19 = Brain(bad_factory)
for weird in [None, 123, {"x": 1}, ["a"]]:
    try:
        r = b19.plan(weird, None, TOOLS)
        ok19 = isinstance(r, list)
    except Exception:
        ok19 = False
    check("never raises on %r" % (weird,), ok19)

print("\n==== PASS: %d  FAIL: %d ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
