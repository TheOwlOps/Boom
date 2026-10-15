"""Autonomous controller: plan -> execute (tools) -> verify -> debug, lap toi khi xong.

Chi stdlib. Khong bao gio raise ra ngoai — moi loi bat lai thanh status dict.

Duong chay that:
  1. brain.plan(goal, history) neu brain co method do, khong thi plan mac dinh 1 step.
  2. Moi step: sanitize_tool_call (guard) -> goi tools[name](**args).
     - path ngoai allowed_roots hoac lenh pha hoai -> dung ngay, status "blocked".
     - tool return {"needs_human": True} hoac raise NeedsHuman -> status "needs_human".
  3. verify_fn(state) -> {"ok": bool, "detail": str}; khong co verify_fn thi coi nhu
     ok neu tools chay khong loi.
  4. verify fail + co debug_fn -> debug_fn(state, verify_result) roi lap lai.
  5. Log moi iter vao task_store (neu co): tao Task 1 lan, append 1 step/iter,
     tuong thich tasks.store.TaskStore that (qua tasks.models) lan fake toi thieu
     chi can create/get/save.

Import root la agent/src::

    sys.path.insert(0, "<...>/agent/src")
    from autonomous.controller import run_goal
"""

import os
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .guard import sanitize_tool_call


class NeedsHuman(Exception):
    """Tool raise de yeu cau con nguoi can thiep."""

    def __init__(self, reason: str = ""):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Task-store logging (best-effort, khong bao gio lam hong vong lap chinh)
# ---------------------------------------------------------------------------

@dataclass
class _ShimStep:
    id: str
    desc: str
    status: str = "pending"
    result: str = ""
    error: str = ""


@dataclass
class _ShimTask:
    id: str = ""
    title: str = ""
    goal: str = ""
    status: str = "running"
    steps: List[_ShimStep] = field(default_factory=list)


def _make_task(goal: str):
    """Dung tasks.models that neu import duoc (tuong thich TaskStore that)."""
    try:
        from tasks.models import Task, TaskStatus
        return Task(id="", title=(goal or "autonomous goal")[:80],
                    goal=goal, status=TaskStatus.RUNNING)
    except Exception:
        return _ShimTask(title=(goal or "autonomous goal")[:80], goal=goal)


def _make_step(step_id: str, desc: str, ok: bool, detail: str):
    try:
        from tasks.models import TaskStep, StepStatus
        return TaskStep(id=step_id, desc=desc,
                        status=StepStatus.DONE if ok else StepStatus.FAILED,
                        result=(detail or "")[:4000] if ok else "",
                        error="" if ok else (detail or "")[:4000])
    except Exception:
        return _ShimStep(id=step_id, desc=desc,
                         status="done" if ok else "failed",
                         result=(detail or "")[:4000] if ok else "",
                         error="" if ok else (detail or "")[:4000])


def _task_begin(task_store, goal: str) -> Optional[str]:
    if task_store is None:
        return None
    try:
        create = getattr(task_store, "create", None)
        if not callable(create):
            return None
        task = _make_task(goal)
        rid = create(task)
        if isinstance(rid, str) and rid:
            return rid
        tid = getattr(task, "id", None)
        return tid if isinstance(tid, str) and tid else None
    except Exception:
        return None


def _task_log_step(task_store, task_id: Optional[str], n: int,
                   desc: str, ok: bool, detail: str) -> None:
    if task_store is None or not task_id:
        return
    try:
        step = _make_step("iter_%d" % n, desc, ok, detail)
        get = getattr(task_store, "get", None)
        task = get(task_id) if callable(get) else None
        if task is not None and hasattr(task, "steps"):
            task.steps.append(step)
            save = getattr(task_store, "save", None)
            if callable(save):
                save(task)
            return
        update_step = getattr(task_store, "update_step", None)
        if callable(update_step):
            update_step(task_id, step.id, "done" if ok else "failed",
                        step.result, step.error)
    except Exception:
        pass


def _task_finish(task_store, task_id: Optional[str], status: str) -> None:
    if task_store is None or not task_id:
        return
    try:
        get = getattr(task_store, "get", None)
        save = getattr(task_store, "save", None)
        if not (callable(get) and callable(save)):
            return
        task = get(task_id)
        if task is None:
            return
        cur = getattr(task, "status", None)
        if cur is not None and not isinstance(cur, str):
            try:
                task.status = cur.__class__(status)
            except Exception:
                pass
        else:
            task.status = status
        save(task)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

_DEFAULT_TOOL_PREFERENCE = ("run_command", "shell", "execute")


def _normalize_plan(raw: Any, tools: Dict[str, Callable]) -> List[Dict[str, Any]]:
    """Chuan hoa output cua brain.plan ve list[{tool, args, desc}]."""
    if isinstance(raw, dict) and isinstance(raw.get("steps"), list):
        raw = raw["steps"]
    if not isinstance(raw, list):
        return []
    steps: List[Dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            tool = item.get("tool") or item.get("action") or item.get("name")
            args = item.get("args") or item.get("arguments") or item.get("params") or {}
            desc = item.get("desc") or item.get("description") or ""
            if tool and isinstance(args, dict):
                steps.append({"tool": str(tool), "args": args, "desc": str(desc)})
        elif isinstance(item, str) and item.strip():
            steps.append({"tool": "", "args": {"command": item}, "desc": item})
    return [s for s in steps if s["tool"] or s["args"]]


def _default_tool(tools: Dict[str, Callable]) -> Optional[str]:
    for name in _DEFAULT_TOOL_PREFERENCE:
        if name in tools:
            return name
    for name in tools:
        return name
    return None


def _build_plan(brain, goal: str, history: List[Dict[str, Any]],
                tools: Dict[str, Callable]) -> List[Dict[str, Any]]:
    if brain is not None:
        plan_fn = getattr(brain, "plan", None)
        if callable(plan_fn):
            try:
                steps = _normalize_plan(plan_fn(goal, history), tools)
                if steps:
                    return steps
            except Exception:
                pass
    tool = _default_tool(tools)
    return [{
        "tool": tool or "run_command",
        "args": {"command": goal},
        "desc": "thuc hien goal: %s" % goal,
    }]


# ---------------------------------------------------------------------------
# Verify / debug wrappers (khong bao gio raise)
# ---------------------------------------------------------------------------

def _run_verify(verify_fn, state: Dict[str, Any],
                tool_error: Optional[str]) -> Dict[str, Any]:
    if verify_fn is None:
        if tool_error:
            return {"ok": False, "detail": tool_error}
        return {"ok": True, "detail": "tools chay khong loi (khong co verify_fn)"}
    try:
        res = verify_fn(state)
    except Exception as e:
        return {"ok": False,
                "detail": "verify_fn raise %s: %s" % (type(e).__name__, e)}
    if isinstance(res, dict):
        return {"ok": bool(res.get("ok")),
                "detail": str(res.get("detail", ""))}
    return {"ok": bool(res), "detail": "verify_fn tra %r" % (res,)}


def _run_debug(debug_fn, state: Dict[str, Any],
               verify_result: Dict[str, Any]) -> Dict[str, Any]:
    if debug_fn is None:
        return {"ran": False}
    try:
        out = debug_fn(state, verify_result)
        return {"ran": True, "output": out}
    except Exception as e:
        return {"ran": True, "error": "%s: %s" % (type(e).__name__, e)}


def _is_tool_failure(res: Any) -> Optional[str]:
    if not isinstance(res, dict):
        return None
    if res.get("error"):
        return str(res["error"])[:500]
    if res.get("success") is False:
        return "tool tra success=False: %s" % str(res.get("error", ""))[:300]
    return None


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def run_goal(goal: str,
             tools: Dict[str, Callable],
             brain=None,
             verify_fn: Optional[Callable] = None,
             debug_fn: Optional[Callable] = None,
             max_iters: int = 10,
             allowed_roots: Optional[List[str]] = None,
             task_store=None) -> Dict[str, Any]:
    """Chay goal tu hanh toi khi verify ok / het max_iters / can human / bi chan.

    Tra dict status, khong bao gio raise:
      {"status": "done", "iters": n, ...}
      {"status": "max_iters", "iters": n, ...}
      {"status": "needs_human", "reason": ..., "iters": n}
      {"status": "blocked", "reason": ..., "iters": n}
      {"status": "error", "reason": ..., "iters": n}  (loi ngoai du kien, da bat)
    """
    started = time.time()
    roots = [os.path.realpath(r) for r in (allowed_roots or [os.getcwd()])
             if isinstance(r, str) and r]
    if not roots:
        roots = [os.path.realpath(os.getcwd())]
    tools = tools or {}
    max_iters = max(1, int(max_iters or 1))

    state: Dict[str, Any] = {
        "goal": goal,
        "iters": 0,
        "history": [],
        "last_results": [],
        "verify": None,
    }
    task_id = _task_begin(task_store, goal)

    def finish(status: str, **extra) -> Dict[str, Any]:
        _task_finish(task_store, task_id,
                     "done" if status == "done" else "failed")
        out = {"status": status, "iters": state["iters"], "goal": goal,
               "elapsed_s": round(time.time() - started, 3)}
        out.update(extra)
        return out

    iters = 0
    last_verify: Dict[str, Any] = {"ok": False, "detail": "chua chay"}
    try:
        while iters < max_iters:
            iters += 1
            state["iters"] = iters
            tool_error: Optional[str] = None
            results: List[Dict[str, Any]] = []
            try:
                plan = _build_plan(brain, goal, state["history"], tools)
                for step in plan:
                    tname = step.get("tool")
                    targs = step.get("args") or {}
                    if not tname or tname not in tools:
                        tool_error = "unknown tool: %r" % (tname,)
                        break
                    ok, reason = sanitize_tool_call(tname, targs, roots)
                    if not ok:
                        _task_log_step(task_store, task_id, iters,
                                       "guard chan: %s" % (step.get("desc") or tname),
                                       False, reason)
                        return finish("blocked", reason=reason,
                                      step=step.get("desc") or tname)
                    try:
                        res = tools[tname](**targs)
                    except NeedsHuman as e:
                        reason = e.reason or str(e) or \
                            "tool %s yeu cau con nguoi" % tname
                        _task_log_step(task_store, task_id, iters,
                                       "needs_human tu %s" % tname, False, reason)
                        return finish("needs_human", reason=reason, tool=tname)
                    if isinstance(res, dict) and res.get("needs_human"):
                        reason = str(res.get("reason") or
                                     "tool %s yeu cau con nguoi" % tname)
                        _task_log_step(task_store, task_id, iters,
                                       "needs_human tu %s" % tname, False, reason)
                        return finish("needs_human", reason=reason, tool=tname)
                    results.append({"tool": tname, "args": targs,
                                    "result": res})
                    state["history"].append(
                        {"iter": iters, "tool": tname, "ok": True})
                    fail = _is_tool_failure(res)
                    if fail:
                        tool_error = "tool %s loi: %s" % (tname, fail)
                        break
            except Exception as e:
                tool_error = "exception khi execute: %s: %s" % (
                    type(e).__name__, e)

            state["last_results"] = results
            last_verify = _run_verify(verify_fn, state, tool_error)
            state["verify"] = last_verify

            if last_verify["ok"]:
                _task_log_step(task_store, task_id, iters,
                               "iter %d: verify OK" % iters, True,
                               last_verify.get("detail", ""))
                return finish("done", verify=last_verify,
                              history=state["history"])

            dbg = _run_debug(debug_fn, state, last_verify)
            _task_log_step(
                task_store, task_id, iters, "iter %d: verify FAIL" % iters,
                False,
                "verify: %s | debug: %s" % (
                    last_verify.get("detail", ""),
                    "ran" if dbg.get("ran") else "no debug_fn"))
            # lap lai iter tiep theo (debug_fn da co co hoi fix trong state/tools)

        return finish("max_iters", verify=last_verify,
                      reason="het %d iters ma verify van fail: %s"
                      % (max_iters, last_verify.get("detail", "")))
    except Exception as e:
        return finish("error",
                      reason="%s: %s\n%s" % (type(e).__name__, e,
                                            traceback.format_exc(limit=5)))
