"""Offline tests for Brain (ReAct + DAG + MASS, one path).

Fake loop factory, no network, no real LLM. Run: python3 evals/test_brain.py
"""
import asyncio
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # repo root: `agent.src...` imports

from agent.src.orchestration.brain import Brain, BrainResult, BrainStrategy
from agent.src.orchestration.mass import SPECIALISTS, AgentRole

PASS = 0


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        raise AssertionError(f"FAIL {name} {extra}")


FULL_TOOLS = {n: {} for n in [
    "read_file", "list_skills", "view_skill", "session_search", "ast_inspect",
    "write_file", "patch_file", "sandbox_exec", "process_start", "process_poll",
    "process_kill", "memory", "install_skill",
]}


class FakeLoop:
    """Minimal stand-in for ReActLoop: step(prompt) -> dict."""

    def __init__(self, tools=None, system_prompt=None, session_id="s"):
        self.tools = dict(tools) if tools is not None else dict(FULL_TOOLS)
        self.system_prompt = system_prompt
        self.session_id = session_id
        self.prompts = []
        self.behavior = None

    async def step(self, prompt):
        self.prompts.append(prompt)
        if self.behavior is None:
            return {"action": f"ok:{prompt[:30]}", "success": True}
        r = self.behavior(prompt)
        if asyncio.iscoroutine(r):
            r = await r
        return r


def simple_factory(created=None):
    box = created if created is not None else []

    def make_loop(system_prompt=None, tools=None, session_id=None):
        lp = FakeLoop(tools=tools, system_prompt=system_prompt,
                      session_id=session_id or "s")
        box.append(lp)
        return lp

    return make_loop, box


# ------------------------------------------------------------------ router

def test_router():
    b = Brain(make_loop=lambda **kw: FakeLoop())
    s, note = b._choose_strategy("dịch câu này sang tiếng Việt", "auto")
    check("router simple -> react", s == BrainStrategy.REACT and note == "")
    s, _ = b._choose_strategy("làm bước 1 rồi sau đó làm bước 2", "auto")
    check("router dep words -> dag", s == BrainStrategy.DAG)
    s, _ = b._choose_strategy("1. viết báo cáo\n2. vẽ biểu đồ\n3. gửi mail", "auto")
    check("router list -> mass", s == BrainStrategy.MASS)
    s, _ = b._choose_strategy("dịch câu này", "mass")
    check("router explicit respected", s == BrainStrategy.MASS)
    s, note = b._choose_strategy("dịch câu này", "bogus")
    check("router bogus -> react + note",
          s == BrainStrategy.REACT and "bogus" in note)


def test_run_dispatch():
    factory, created = simple_factory()
    b = Brain(make_loop=factory)
    r = asyncio.run(b.run("hello", strategy="react"))
    check("run react strategy+text",
          r.strategy == "react" and r.text.startswith("ok:"))
    check("run react one loop, no role prompt",
          len(created) == 1 and created[0].system_prompt is None)


# ------------------------------------------------------------------ REACT

def test_react():
    def factory(**kw):
        lp = FakeLoop()
        lp.behavior = lambda p: {"action": "hello world", "success": True}
        return lp

    b = Brain(make_loop=factory)
    r = asyncio.run(b.run_react("x"))
    check("react text", r.text == "hello world" and r.strategy == "react")

    def efactory(**kw):
        lp = FakeLoop()
        lp.behavior = lambda p: {"error": "model exploded"}
        return lp

    b2 = Brain(make_loop=efactory)
    r2 = asyncio.run(b2.run_react("x"))
    check("react error recorded", "react" in r2.errors and r2.text == "")


# ------------------------------------------------------------------ DAG

PLAN = "1. Thu thập dữ liệu\n2. Phân tích (phụ thuộc 1)\n3. Viết báo cáo (sau 2)"


def dag_factory(order=None, prompts=None, fail_node=None, plan_text=PLAN):
    order = order if order is not None else []
    prompts = prompts if prompts is not None else {}
    created = []

    def make_loop(system_prompt=None, tools=None, session_id=None):
        lp = FakeLoop(tools=tools, system_prompt=system_prompt,
                      session_id=session_id or "s")
        if system_prompt and "Decompose" in system_prompt:
            lp.behavior = lambda p: {"action": plan_text, "success": True}
        else:
            def beh(p):
                m = re.search(r"Subtask \[(\w+)\]", p)
                nid = m.group(1) if m else "?"
                order.append(nid)
                prompts[nid] = p
                if nid == fail_node:
                    raise RuntimeError("node broke")
                return {"action": f"OUT-{nid}", "success": True}
            lp.behavior = beh
        created.append(lp)
        return lp

    return make_loop, created


def test_dag():
    order, prompts = [], {}
    factory, created = dag_factory(order, prompts)
    b = Brain(make_loop=factory)
    r = asyncio.run(b.run_dag("làm báo cáo", session_id="t1"))
    check("dag strategy", r.strategy == "dag")
    check("dag order respects deps", order == ["1", "2", "3"], f"order={order}")
    check("dag outputs", set(r.node_outputs) == {"1", "2", "3"})
    check("dag dep context passed", "OUT-1" in prompts.get("2", ""))
    check("dag text from terminals", "[3]" in r.text and "OUT-3" in r.text)
    planner = [l for l in created if l.session_id == "t1/planner"]
    check("dag planner got planner prompt",
          planner and "Decompose" in (planner[0].system_prompt or ""))
    check("dag planner tools filtered",
          planner and set(planner[0].tools) <= set(SPECIALISTS[AgentRole.PLANNER].allowed_tools))


def test_dag_fail():
    order, prompts = [], {}
    factory, _ = dag_factory(order, prompts, fail_node="2")
    b = Brain(make_loop=factory)
    r = asyncio.run(b.run_dag("x", session_id="t2"))
    check("dag node error recorded", "2" in r.errors)
    check("dag dependent blocked",
          any(k.startswith("blocked:3") for k in r.errors), f"errors={r.errors}")
    check("dag node1 output kept", r.node_outputs.get("1") == "OUT-1")


def test_dag_fallback():
    factory, _ = dag_factory(plan_text="tôi không liệt kê gì đâu")
    b = Brain(make_loop=factory)
    r = asyncio.run(b.run_dag("x"))
    check("dag fallback strategy", r.strategy == "react")
    check("dag fallback noted", "fallback" in r.errors, f"errors={r.errors}")
    check("dag fallback has text", r.text != "")


def test_parse():
    b = Brain(make_loop=lambda **kw: FakeLoop())
    nodes = b._parse_plan("1. Thu thập\n2. Phân tích (phụ thuộc 1)\n3. Báo cáo (sau 2)")
    check("parse ids", [n.id for n in nodes] == ["1", "2", "3"])
    check("parse deps", [n.dependencies for n in nodes] == [[], ["1"], ["2"]])
    check("parse name stripped", nodes[1].name == "Phân tích", f"got={nodes[1].name!r}")
    for bad, label in [("no list here", "empty"), ("1. A (after 9)", "bad dep"),
                       ("1. A\n1. B", "dup id")]:
        try:
            b._parse_plan(bad)
            check(f"parse {label} raises", False)
        except ValueError:
            check(f"parse {label} raises", True)


# ------------------------------------------------------------------ MASS

def test_mass():
    state = {"active": 0, "max": 0}
    created = []

    def factory(system_prompt=None, tools=None, session_id=None):
        lp = FakeLoop(tools=tools, system_prompt=system_prompt,
                      session_id=session_id or "s")

        async def beh(p):
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            await asyncio.sleep(0.02)
            state["active"] -= 1
            return {"action": f"ans:{(session_id or '').split('/')[-1]}", "success": True}

        lp.behavior = beh
        created.append(lp)
        return lp

    b = Brain(make_loop=factory)
    roles = [AgentRole.PLANNER, AgentRole.CODER, AgentRole.REVIEWER]
    r = asyncio.run(b.run_mass("review this", session_id="m1", roles=roles))
    check("mass strategy", r.strategy == "mass")
    check("mass ran parallel", state["max"] >= 2, f"max={state['max']}")
    check("mass outputs per role", set(r.agent_outputs) == {"planner", "coder", "reviewer"})
    check("mass text labelled", "[coder]" in r.text and "[planner]" in r.text)
    ok = True
    for lp in created:
        if not lp.session_id.startswith("m1/mass/"):
            continue  # the _all_tools() probe loop, not a specialist
        role = lp.session_id.split("/")[-1]
        if not set(lp.tools) <= set(SPECIALISTS[AgentRole(role)].allowed_tools):
            ok = False
    check("mass tools filtered per role", ok)
    # role prompt carries system instruction + contract
    coder = [l for l in created if l.session_id.endswith("/coder")][0]
    check("mass role prompt has contract",
          "Write and fix code" in (coder.system_prompt or "") and "Contract" in (coder.system_prompt or ""))


def test_mass_fail():
    def factory(system_prompt=None, tools=None, session_id=None):
        lp = FakeLoop(tools=tools, system_prompt=system_prompt,
                      session_id=session_id or "s")
        role = (session_id or "").split("/")[-1]

        async def beh(p):
            if role == "coder":
                raise RuntimeError("coder down")
            return {"action": f"ans:{role}", "success": True}

        lp.behavior = beh
        return lp

    b = Brain(make_loop=factory)
    r = asyncio.run(b.run_mass("x", session_id="m2",
                               roles=[AgentRole.PLANNER, AgentRole.CODER, AgentRole.REVIEWER]))
    check("mass role error recorded", "coder" in r.errors)
    check("mass others still done",
          r.agent_outputs.get("planner", {}).get("text") == "ans:planner"
          and r.agent_outputs.get("reviewer", {}).get("text") == "ans:reviewer")
    check("mass failed role labelled",
          "[coder]" not in r.text and "[planner]" in r.text)

    r2 = asyncio.run(b.run_mass("x", roles=["nope"]))
    check("mass unknown role", "role:nope" in r2.errors and r2.text == "")


# ------------------------------------------------------------------ safety

def test_no_raise():
    def bad_factory(**kw):
        raise RuntimeError("factory dead")

    b = Brain(make_loop=bad_factory)
    r = asyncio.run(b.run("hello", strategy="react"))
    check("run never raises", isinstance(r, BrainResult) and "react" in r.errors)
    r2 = asyncio.run(b.run("hello", strategy="mass"))
    check("mass never raises", isinstance(r2, BrainResult) and bool(r2.errors))


if __name__ == "__main__":
    test_router()
    test_run_dispatch()
    test_react()
    test_dag()
    test_dag_fail()
    test_dag_fallback()
    test_parse()
    test_mass()
    test_mass_fail()
    test_no_raise()
    print(f"ALL BRAIN TESTS PASSED ({PASS} checks)")
