#!/usr/bin/env python3
"""End-to-end OFFLINE integration tests: Brain verify contract + tasks/ checkpoint
contract + environment §5e wire resilience + regression vs evals/test_brain.py.

Run:  python3 evals/test_brain_tasks_verify.py        (from integration/)
   or python3 ~/workspace/boom-mods/tasks-verify/integration/evals/test_brain_tasks_verify.py

100% OFFLINE: real sqlite3, real tempfile dir, real asyncio. No network, no real
LLM. Fakes are plain objects/functions, clearly labelled.

WHAT IS REAL vs TEST-DOUBLE (honest map):
  Part A — Brain verify contract.
      Uses the REAL ../brain.py iff it exists AND imports AND passes a contract
      probe (verify=False -> obj with .text/.strategy; verify=True -> dict with
      "result"+"verification"). Otherwise uses BrainShim, a minimal
      TEST-DOUBLE that mirrors the real module's documented contract
      (async run, router rules + regexes, _extract_text, lazy
      import_module("verify.verifier"), mod.verify(text, kind="text",
      llm=..., search_fn=...) with any-error -> {"status": "skipped"}).
      NOTE (2026-10-15): the real brain.py needs pydantic + the agent.src tree,
      neither available offline here -> the probe falls back to the shim.
      The selection is printed at startup.
  Part B — task checkpoint/resume contract.
      TaskStore + TaskRunner are TEST-DOUBLES, bannered
      "REFERENCE IMPLEMENTATION OF THE CONTRACT". The real tasks/ module (other
      worker) must satisfy the same observable contract; these tests pin the
      contract, not the real module.
  Part C — env wire resilience.
      Replicates the EXACT try/except loop from environment.py section 5e
      (copied logic, real environment module NOT imported). One check greps
      the real file to confirm the pattern fingerprint is present.
  Part D — regression vs evals/test_brain.py on main (293 lines, 40 checks).
      Re-asserts the behaviours that are meaningful against shim+fake
      (router rules, react path, never-raises, bogus-strategy note).
      DAG execution / MASS parallelism / _parse_plan need the full
      brain.py + dag/mass modules and are NOT re-runnable against the minimal
      shim: they are listed as INFO with the pointer to the real test file,
      never faked as PASS.

Every check prints [PASS]/[FAIL]. The final line
"ALL INTEGRATION TESTS PASSED (N checks)" prints only when every check passed;
the process exits non-zero on any failure.
"""

import asyncio
import importlib
import importlib.util
import inspect
import os
import re
import sqlite3
import sys
import tempfile
import types
from enum import Enum
from pathlib import Path

HERE = Path(__file__).resolve().parent
INTEGRATION_DIR = HERE.parent  # ../brain.py, ../environment.py live here

# ---------------------------------------------------------------- harness

CHECKS = 0
FAILURES = []


def check(name, cond, detail=""):
    """Record one check; print PASS/FAIL."""
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name}" + (f" -- {detail}" if detail else ""))
        FAILURES.append(name)


def info(msg):
    print(f"[INFO] {msg}")


# ------------------------------------------------- fake verifier injection

def _make_fake_verifier(impl=None):
    """Build fake `verify` package + `verify.verifier` module.

    Default impl mirrors the task spec: verify(result, kind, llm, search_fn)
    -> {"checks": [{"claim": "x", "verdict": "supported"}]}.
    """
    def verify(result, kind="text", llm=None, search_fn=None):
        if impl is not None:
            return impl(result, kind, llm, search_fn)
        return {"checks": [{"claim": "x", "verdict": "supported"}],
                "kind": kind, "had_llm": llm is not None,
                "had_search": search_fn is not None}

    pkg = types.ModuleType("verify")
    pkg.__path__ = []  # mark as package so `import verify.verifier` works
    mod = types.ModuleType("verify.verifier")
    mod.verify = verify
    pkg.verifier = mod
    return pkg, mod


def _inject_fake_verifier(impl=None):
    pkg, mod = _make_fake_verifier(impl)
    sys.modules["verify"] = pkg
    sys.modules["verify.verifier"] = mod
    return mod.verify


def _remove_fake_verifier():
    sys.modules.pop("verify.verifier", None)
    sys.modules.pop("verify", None)


# ------------------------------------------------------------- fake loop

class FakeLoop:
    """Fake loop: step() -> {"action": "done"} (per task spec)."""

    def __init__(self, **kw):
        self.kw = kw
        self.prompts = []

    def step(self, prompt):
        self.prompts.append(prompt)
        return {"action": "done"}


def make_fake_factory(box):
    def make_loop(**kw):
        lp = FakeLoop(**kw)
        box.append(lp)
        return lp

    return make_loop


# ------------------------------------------------- minimal Brain (shim)

_DEP_WORDS = re.compile(
    r"\b(rồi|sau đó|trước tiên|bước\s*\d+|step\s*\d+|\bthen\b|after that)\b",
    re.IGNORECASE,
)
_LIST_ITEM = re.compile(r"^\s*(?:\d+[\.\)\]]|[-*•])\s+\S", re.MULTILINE)


class BrainStrategyShim(str, Enum):
    REACT = "react"
    DAG = "dag"
    MASS = "mass"


class BrainResultShim:
    """Stdlib stand-in for the pydantic BrainResult (test-double)."""

    def __init__(self, strategy="", text="", node_outputs=None,
                 agent_outputs=None, errors=None):
        self.strategy = strategy
        self.text = text
        self.node_outputs = node_outputs or {}
        self.agent_outputs = agent_outputs or {}
        self.errors = errors or {}

    def __repr__(self):
        return (f"BrainResultShim(strategy={self.strategy!r}, "
                f"text={self.text!r}, errors={self.errors!r})")


class BrainShim:
    """TEST-DOUBLE: minimal Brain implementing the run(..., verify=...) contract
    of integration/brain.py (async run, router rules, _extract_text, lazy
    verify.verifier import, skipped-on-any-error)."""

    def __init__(self, make_loop, llm=None):
        self.make_loop = make_loop
        self.llm = llm

    # -- router: same rules + regexes as the real module ------------------
    def _choose_strategy(self, prompt, strategy):
        s = (strategy or "auto").strip().lower()
        if s != "auto":
            try:
                return BrainStrategyShim(s), ""
            except ValueError:
                return (BrainStrategyShim.REACT,
                        f"unknown strategy '{strategy}'; using 'react'")
        text = prompt or ""
        if _DEP_WORDS.search(text):
            return BrainStrategyShim.DAG, ""
        if len(_LIST_ITEM.findall(text)) >= 2:
            return BrainStrategyShim.MASS, ""
        return BrainStrategyShim.REACT, ""

    # -- run: never raises; verify contract --------------------------------
    async def run(self, prompt, strategy="auto", session_id=None, roles=None,
                  verify=False, **kw):
        chosen, note = self._choose_strategy(prompt, strategy)
        errors = {}
        if note:
            errors["strategy"] = note
        try:
            if chosen is BrainStrategyShim.DAG:
                raise RuntimeError("shim has no DAG engine (full brain.py only)")
            elif chosen is BrainStrategyShim.MASS:
                raise RuntimeError("shim has no MASS engine (full brain.py only)")
            else:
                result = await self.run_react(prompt, session_id=session_id)
        except Exception as e:  # last-resort net, like the real module
            result = BrainResultShim(
                strategy=chosen.value, text="",
                errors={**errors, "brain": f"{type(e).__name__}: {e}"})
        else:
            result.errors = {**errors, **result.errors}
        if not verify:
            return result
        return {"result": result, "verification": self._verify_result(result)}

    def _verify_result(self, result):
        """Lazy import of verify.verifier; any error -> skipped (never raise)."""
        try:
            from importlib import import_module
            mod = import_module("verify.verifier")
            return mod.verify(result.text, kind="text", llm=self.llm,
                              search_fn=None)
        except Exception as e:
            return {"status": "skipped",
                    "reason": f"{type(e).__name__}: {e}"}

    @staticmethod
    def _extract_text(res):
        if isinstance(res, dict):
            return str(res.get("action") or res.get("observation") or "")
        return "" if res is None else str(res)

    async def run_react(self, prompt, session_id=None):
        try:
            loop = self.make_loop(session_id=session_id)
            res = loop.step(prompt)
            if inspect.isawaitable(res):
                res = await res
        except Exception as e:
            return BrainResultShim(
                strategy=BrainStrategyShim.REACT.value, text="",
                errors={"react": f"{type(e).__name__}: {e}"})
        errors = {}
        if isinstance(res, dict) and res.get("error"):
            errors["react"] = str(res["error"])
        return BrainResultShim(
            strategy=BrainStrategyShim.REACT.value,
            text=self._extract_text(res), errors=errors)


def _probe_real_brain():
    """Try to use the REAL ../brain.py. Returns (Brain, BrainResult, label)
    or (None, None, reason). Never raises."""
    p = INTEGRATION_DIR / "brain.py"
    if not p.exists():
        return None, None, "not present"
    # Let `agent...` imports resolve the way the real module expects, so the
    # failure reason (if any) is the true blocker, not a sys.path artefact.
    parent = str(INTEGRATION_DIR.parent)
    sys.path.insert(0, parent)
    try:
        spec = importlib.util.spec_from_file_location(
            "boom_integration_brain_real", str(p))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return None, None, f"import failed ({type(e).__name__}: {e})"
    finally:
        if parent in sys.path:
            sys.path.remove(parent)
    Brain, BrainResult = mod.Brain, getattr(mod, "BrainResult", None)
    try:
        box = []
        b = Brain(make_loop=make_fake_factory(box), llm=None)
        r = asyncio.run(b.run("probe", strategy="react", session_id="probe",
                             verify=False))
        if not (hasattr(r, "text") and hasattr(r, "strategy")):
            return None, None, "verify=False lacks .text/.strategy"
        _inject_fake_verifier()
        try:
            d = asyncio.run(b.run("probe", strategy="react", verify=True))
        finally:
            _remove_fake_verifier()
        if not (isinstance(d, dict) and "result" in d and "verification" in d):
            return None, None, "verify=True lacks result/verification dict"
        return Brain, BrainResult, "ok"
    except Exception as e:
        _remove_fake_verifier()
        return None, None, f"contract probe failed ({type(e).__name__}: {e})"


Brain, BrainResult, _probe_note = _probe_real_brain()
if Brain is None:
    info(f"Part A implementation: SHIM (test-double) -- real ../brain.py "
         f"unusable: {_probe_note}")
    Brain, BrainResult = BrainShim, BrainResultShim
    BRAIN_UNDER_TEST = "SHIM (test-double)"
else:
    info("Part A implementation: REAL ../brain.py (passed contract probe)")
    BRAIN_UNDER_TEST = "REAL ../brain.py"


# ======================================================================
# Part A -- Brain verify contract
# ======================================================================

def part_a():
    print("\n=== Part A: Brain verify contract "
          f"(under test: {BRAIN_UNDER_TEST}) ===")

    # A1/A2: fake make_loop -> fake loop with step() -> {"action": "done"};
    # run(verify=False) returns object with .text/.strategy (regression pass).
    box = []
    b = Brain(make_loop=make_fake_factory(box), llm=None)
    r = asyncio.run(b.run("hello brain", strategy="react", session_id="s1",
                          verify=False))
    check("A: run(verify=False) returns .text/.strategy object (no raise)",
          hasattr(r, "text") and hasattr(r, "strategy"),
          f"got {type(r).__name__}")
    check("A: explicit react -> strategy 'react', text 'done'",
          getattr(r, "strategy", None) == "react" and r.text == "done",
          f"strategy={getattr(r, 'strategy', None)!r} text={getattr(r, 'text', None)!r}")
    check("A: fake make_loop was used exactly once, prompt reached step()",
          len(box) == 1 and box[0].prompts == ["hello brain"],
          f"loops={len(box)}")

    # A3: inject fake verify.verifier -> run(verify=True) returns dict with
    # ["result"] and ["verification"]["checks"].
    _inject_fake_verifier()
    try:
        d = asyncio.run(b.run("verify me", strategy="react", verify=True))
    finally:
        pass  # keep injected for the next checks; removed later
    check("A: run(verify=True) returns dict", isinstance(d, dict),
          f"got {type(d).__name__}")
    check("A: dict has 'result' and 'verification' keys",
          isinstance(d, dict) and "result" in d and "verification" in d)
    res, ver = d["result"], d["verification"]
    check("A: result is BrainResult-like (.text/.strategy)",
          hasattr(res, "text") and hasattr(res, "strategy"))
    check("A: verification has checks with verdict 'supported'",
          isinstance(ver, dict) and ver.get("checks")
          and ver["checks"][0].get("verdict") == "supported",
          f"verification={ver!r}")
    check("A: verifier called with kind='text' (real call convention)",
          isinstance(ver, dict) and ver.get("kind") == "text",
          f"verification={ver!r}")
    check("A: verify=True result text matches verify=False execution",
          res.text == "done", f"text={res.text!r}")

    # Contract stability: verify=False still returns the plain result object
    # even while the verifier module is importable.
    r2 = asyncio.run(b.run("plain", strategy="react", verify=False))
    check("A: verify=False still returns non-dict while verifier present",
          not isinstance(r2, dict) and hasattr(r2, "text"))

    # A4a: verifier that RAISES -> skipped, never raises.
    _inject_fake_verifier(impl=lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("verifier exploded")))
    try:
        d_raise = asyncio.run(b.run("boom", strategy="react", verify=True))
        raised = False
    except Exception:
        raised, d_raise = True, None
    check("A: raising verifier -> no raise, verification skipped",
          not raised and isinstance(d_raise, dict)
          and d_raise["verification"].get("status") == "skipped")

    # A4b: fake module REMOVED -> run(verify=True) -> dict with
    # verification["status"] == "skipped", no raise.
    _remove_fake_verifier()
    try:
        d2 = asyncio.run(b.run("no verifier here", strategy="react",
                               verify=True))
        raised2 = False
    except Exception as e:
        raised2, d2 = True, str(e)
    check("A: missing verifier -> no raise", not raised2,
          f"raised: {d2!r}" if raised2 else "")
    check("A: missing verifier -> dict with verification.status == 'skipped'",
          isinstance(d2, dict)
          and d2.get("verification", {}).get("status") == "skipped",
          f"got {d2!r}"[:200])
    check("A: skipped verification carries a reason",
          isinstance(d2, dict) and bool(d2["verification"].get("reason")))
    check("A: sys.modules cleaned (no leftover verify.verifier/verify)",
          "verify.verifier" not in sys.modules and "verify" not in sys.modules)


# ======================================================================
# Part B -- task checkpoint/resume contract (REFERENCE IMPLEMENTATION)
# ======================================================================
#
# REFERENCE IMPLEMENTATION OF THE CONTRACT -- the real tasks/ module must
# satisfy this observable contract:
#   * tasks(id, title, goal, status) + steps(task_id, idx, desc, status, result)
#   * every finished step is COMMITTED immediately (checkpoint per step)
#   * a NEW runner on the same db file resumes: only non-"done" steps run
# ======================================================================

class TaskStore:
    """REFERENCE IMPLEMENTATION OF THE CONTRACT (test-double)."""

    def __init__(self, path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS tasks("
            "id TEXT PRIMARY KEY, title TEXT, goal TEXT, status TEXT)")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS steps("
            "task_id TEXT, idx INTEGER, desc TEXT, status TEXT, result TEXT,"
            " PRIMARY KEY(task_id, idx))")
        self.conn.commit()

    def create_task(self, task_id, title, goal, step_descs):
        c = self.conn
        c.execute("INSERT OR REPLACE INTO tasks VALUES(?,?,?,?)",
                  (task_id, title, goal, "pending"))
        for i, d in enumerate(step_descs):
            c.execute("INSERT OR REPLACE INTO steps VALUES(?,?,?,?,?)",
                      (task_id, i, d, "pending", None))
        c.commit()

    def pending_steps(self, task_id):
        return self.conn.execute(
            "SELECT idx, desc FROM steps WHERE task_id=? AND status!='done'"
            " ORDER BY idx", (task_id,)).fetchall()

    def mark_step_done(self, task_id, idx, result):
        self.conn.execute(
            "UPDATE steps SET status='done', result=? WHERE task_id=? AND idx=?",
            (result, task_id, idx))
        self.conn.commit()  # CHECKPOINT: durable immediately after each step

    def mark_task_done(self, task_id):
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?",
                          (task_id,))
        self.conn.commit()

    def get_status(self, task_id):
        row = self.conn.execute("SELECT status FROM tasks WHERE id=?",
                                (task_id,)).fetchone()
        done = self.conn.execute(
            "SELECT COUNT(*) FROM steps WHERE task_id=? AND status='done'",
            (task_id,)).fetchone()[0]
        total = self.conn.execute("SELECT COUNT(*) FROM steps WHERE task_id=?",
                                  (task_id,)).fetchone()[0]
        return {"task_status": row[0] if row else None,
                "done": done, "total": total}

    def step_rows(self, task_id):
        return self.conn.execute(
            "SELECT idx, desc, status, result FROM steps WHERE task_id=?"
            " ORDER BY idx", (task_id,)).fetchall()

    def close(self):
        self.conn.close()


class TaskRunner:
    """REFERENCE IMPLEMENTATION OF THE CONTRACT (test-double)."""

    def __init__(self, store, step_fn):
        self.store = store
        self.step_fn = step_fn  # fake brain: desc -> result string

    def create(self, task_id, title, goal):
        # Split the goal into 3 assumed steps (contract placeholder).
        descs = [f"{title}: step {i + 1} of 3 -- {goal}" for i in range(3)]
        self.store.create_task(task_id, title, goal, descs)
        return task_id

    def run_one_step(self, task_id):
        pending = self.store.pending_steps(task_id)
        if not pending:
            return False
        idx, desc = pending[0]
        result = self.step_fn(desc)
        self.store.mark_step_done(task_id, idx, result)
        return True

    def run_task(self, task_id):
        while self.run_one_step(task_id):
            pass
        self.store.mark_task_done(task_id)


def part_b():
    print("\n=== Part B: task checkpoint/resume contract "
          "(REFERENCE IMPLEMENTATION -- test-double) ===")
    tmp = tempfile.TemporaryDirectory()  # real tmp dir
    db_path = os.path.join(tmp.name, "tasks.db")

    calls = []

    def fake_brain(desc):
        calls.append(desc)
        return f"RESULT::{desc}"

    # B1: create a 3-step task.
    store = TaskStore(db_path)
    runner = TaskRunner(store, fake_brain)
    runner.create("t1", "demo task", "demo goal")
    check("B: task created with 3 pending steps",
          len(store.pending_steps("t1")) == 3)

    # B2: run 2 steps, then SIMULATE CRASH (close + discard runner/store).
    runner.run_one_step("t1")
    runner.run_one_step("t1")
    check("B: fake brain called exactly 2x before crash", len(calls) == 2,
          f"calls={len(calls)}")
    store.close()
    del runner, store  # <-- process "dies" here; only committed data survives

    # B3: checkpoint durability -- a FRESH connection sees steps 1-2 done.
    probe = TaskStore(db_path)
    rows = probe.step_rows("t1")
    check("B: steps 1-2 are 'done' after crash (checkpoint committed)",
          rows[0][2] == "done" and rows[1][2] == "done" and rows[2][2] != "done",
          f"statuses={[r[2] for r in rows]}")
    check("B: step results persisted (non-null)",
          all(r[3] for r in rows[:2]), f"results={[r[3] for r in rows]}")
    probe.close()

    # B4: NEW runner on the SAME db file resumes -> only step 3 runs.
    # The store is created INSIDE the worker thread: sqlite3 connections are
    # thread-local, so sharing one across asyncio.to_thread is a
    # ProgrammingError. asyncio.to_thread still exercises the real asyncio.
    def _resume():
        s = TaskStore(db_path)
        try:
            TaskRunner(s, fake_brain).run_task("t1")
        finally:
            s.close()

    asyncio.run(asyncio.to_thread(_resume))  # real asyncio + real sqlite3
    store2 = TaskStore(db_path)
    check("B: resume ran only the remaining step (brain +1 call, total 3)",
          len(calls) == 3, f"calls={len(calls)}")
    st = store2.get_status("t1")
    check("B: get_status -> task done", st["task_status"] == "done",
          f"status={st!r}")
    check("B: get_status -> 3/3 steps done",
          st["done"] == 3 and st["total"] == 3, f"status={st!r}")
    check("B: steps table has exactly 3 rows",
          len(store2.step_rows("t1")) == 3)
    check("B: db file exists and is non-empty",
          os.path.exists(db_path) and os.path.getsize(db_path) > 0)

    # B5: resume is idempotent -- running again does nothing.
    TaskRunner(store2, fake_brain).run_task("t1")
    check("B: second run_task adds 0 brain calls (idempotent)",
          len(calls) == 3, f"calls={len(calls)}")
    store2.close()
    tmp.cleanup()


# ======================================================================
# Part C -- environment §5e wire resilience (replicated pattern)
# ======================================================================

def part_c():
    print("\n=== Part C: environment §5e wire resilience "
          "(replicated pattern, real environment NOT imported) ===")

    # C0: confirm the replicated pattern matches the real §5e source.
    env_path = INTEGRATION_DIR / "environment.py"
    src = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    check("C: real §5e pattern present in environment.py "
          "(getattr/import_module/build_tools + 'unavailable')",
          'getattr(import_module(module_name), "build_tools", None)' in src
          and "unavailable" in src)

    # --- exact replication of the §5e loop (copied logic) ---
    ROOT = object()  # sentinel for root_dir
    ENV = object()   # sentinel for the env itself
    registry = {}
    build_calls = []

    def fake_build_tools(arg):
        build_calls.append(arg)
        return {"tasks_tool": {"from": "tasks"}, "verify_tool": {"from": "verify"}}

    def fake_import_module(name):
        if name == "tools.nonexistent_xyz":
            raise ImportError("No module named 'tools.nonexistent_xyz'")
        if name == "tasks.broken":
            m = types.ModuleType(name)

            def boom(arg):
                raise RuntimeError("broken factory")

            m.build_tools = boom
            return m
        if name == "tasks.noop":
            return types.ModuleType(name)  # no build_tools attribute
        m = types.ModuleType(name)
        m.build_tools = fake_build_tools
        return m

    module_names = ["tasks.tools", "verify.tools", "tools.nonexistent_xyz",
                    "tasks.broken", "tasks.noop"]

    raised = None
    try:
        # This block is the §5e pattern verbatim (modulo the fake import fn):
        for module_name, use_root_dir in (
            ("tasks.tools", True),
            ("verify.tools", True),
            ("tools.nonexistent_xyz", False),
            ("tasks.broken", True),
            ("tasks.noop", False),
        ):
            try:
                factory = getattr(fake_import_module(module_name),
                                  "build_tools", None)
                if factory is not None:
                    registry.update(factory(ROOT if use_root_dir else ENV))
            except Exception as e:
                print(f"[tools] {module_name} unavailable: {e}")
    except Exception as e:  # must never happen
        raised = e

    check("C: wire loop never raises", raised is None,
          f"raised {raised!r}" if raised else "")
    check("C: tasks.tools loaded", "tasks_tool" in registry)
    check("C: verify.tools loaded", "verify_tool" in registry)
    check("C: build_tools got the exact root_dir arg for use_root_dir=True",
          build_calls == [ROOT, ROOT], f"calls={len(build_calls)}")
    check("C: missing module skipped silently (no raise, nothing registered)",
          "tools.nonexistent_xyz" not in str(registry))
    check("C: module whose build_tools raises is skipped (no raise)",
          all("broken" not in str(v) for v in registry.values()))
    check("C: module without build_tools attr is skipped (no raise)",
          "noop" not in str(registry))
    check("C: registry holds exactly the 2 expected tools",
          set(registry) == {"tasks_tool", "verify_tool"},
          f"keys={sorted(registry)}")


# ======================================================================
# Part D -- regression vs evals/test_brain.py on main (293 lines, 40 checks)
# Re-asserted here with shim+fake; behaviours needing the full
# brain.py + dag/mass modules are listed as INFO (not faked as PASS).
# ======================================================================

def part_d():
    print("\n=== Part D: regression re-asserts from evals/test_brain.py "
          "(main) ===")
    info("source: evals/test_brain.py @ TheOwlOps/Boom main "
         "(40 checks, FakeLoop, no network)")

    b = Brain(make_loop=make_fake_factory([]), llm=None)

    # -- router (re-assert of test_router, 5 checks) --
    s, note = b._choose_strategy("dịch câu này sang tiếng Việt", "auto")
    strat = s.value if hasattr(s, "value") else s
    check("D: router simple -> react",
          strat == "react" and note == "", f"s={strat!r} note={note!r}")
    s, _ = b._choose_strategy("làm bước 1 rồi sau đó làm bước 2", "auto")
    check("D: router dep words -> dag",
          (s.value if hasattr(s, "value") else s) == "dag")
    s, _ = b._choose_strategy("1. viết báo cáo\n2. vẽ biểu đồ\n3. gửi mail",
                              "auto")
    check("D: router list -> mass",
          (s.value if hasattr(s, "value") else s) == "mass")
    s, _ = b._choose_strategy("dịch câu này", "mass")
    check("D: router explicit respected",
          (s.value if hasattr(s, "value") else s) == "mass")
    s, note = b._choose_strategy("dịch câu này", "bogus")
    check("D: router bogus -> react + note",
          (s.value if hasattr(s, "value") else s) == "react"
          and "bogus" in note, f"note={note!r}")

    # -- run dispatch (re-assert of test_run_dispatch, 2 checks) --
    created = []
    b2 = Brain(make_loop=make_fake_factory(created), llm=None)
    r = asyncio.run(b2.run("hello", strategy="react", session_id="d1"))
    check("D: run react strategy+text",
          getattr(r, "strategy", None) == "react" and r.text == "done",
          f"strategy={getattr(r, 'strategy', None)!r} text={r.text!r}")
    check("D: run react created exactly one loop",
          len(created) == 1, f"loops={len(created)}")

    # -- react path (re-assert of test_react, 2 checks) --
    class BehaviorLoop:
        def __init__(self, behavior=None, **kw):
            self.behavior = behavior

        def step(self, prompt):
            return self.behavior(prompt)

    b3 = Brain(make_loop=lambda **kw: BehaviorLoop(
        behavior=lambda p: {"action": "hello world", "success": True}), llm=None)
    r3 = asyncio.run(b3.run_react("x"))
    check("D: react text from loop action",
          r3.text == "hello world" and r3.strategy == "react",
          f"text={r3.text!r}")

    b4 = Brain(make_loop=lambda **kw: BehaviorLoop(
        behavior=lambda p: {"error": "model exploded"}), llm=None)
    r4 = asyncio.run(b4.run_react("x"))
    check("D: react error recorded, text empty",
          "react" in r4.errors and r4.text == "",
          f"errors={r4.errors!r}")

    # -- never raises (re-assert of test_no_raise, 2 checks) --
    def bad_factory(**kw):
        raise RuntimeError("factory dead")

    b5 = Brain(make_loop=bad_factory, llm=None)
    r5 = asyncio.run(b5.run("hello", strategy="react"))
    check("D: run never raises (dead factory, react)",
          hasattr(r5, "text") and "react" in r5.errors,
          f"errors={getattr(r5, 'errors', None)!r}")
    r6 = asyncio.run(b5.run("hello", strategy="mass"))
    check("D: run never raises (dead factory, mass)",
          hasattr(r6, "text") and bool(r6.errors),
          f"errors={getattr(r6, 'errors', None)!r}")

    # -- bogus-strategy note flows into errors (real run() behaviour) --
    r7 = asyncio.run(b5.run("x", strategy="bogus"))
    check("D: bogus strategy note recorded in errors['strategy']",
          "bogus" in str(r7.errors.get("strategy", "")),
          f"errors={r7.errors!r}")

    # -- behaviours NOT re-assertable with the minimal shim (documented) --
    info("not re-runnable offline with the minimal shim (need full brain.py "
         "+ dag/mass modules + pydantic); covered by evals/test_brain.py "
         "on main (40/40 PASS):")
    for documented in [
        "dag order respects deps; node_outputs keys; dep context passed; "
        "text from terminals; planner session id + tool filtering",
        "dag node error recorded; dependent blocked; node1 output kept; "
        "fallback to react on unparseable plan ('fallback' in errors)",
        "_parse_plan: ids/deps/name stripping; ValueError on bad plans",
        "mass ran parallel (>=2 concurrent); outputs per role; labelled "
        "text; per-role tool filtering; role prompt carries contract",
        "mass role error recorded; others still done; failed role not "
        "labelled; unknown role -> errors",
    ]:
        info(f"  - (documented) {documented}")


# ======================================================================

def main():
    part_a()
    part_b()
    part_c()
    part_d()
    print()
    if FAILURES:
        print(f"INTEGRATION TESTS FAILED: {len(FAILURES)}/{CHECKS} "
              f"checks failed:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print(f"ALL INTEGRATION TESTS PASSED ({CHECKS} checks)")
    return 0


def test_integration_all():
    """pytest entry point: runs the whole suite, fails on any check."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
