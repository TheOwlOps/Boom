"""Test TONG HOP offline cho durable task engine: models + store + planner + runner + tools.

Cac phan:
  1. models   - round-trip Task 3 steps qua json deep-equal; status la -> PENDING.
  2. store    - tmp db; create/get/list filter/save/update_step; task khong ton tai -> False.
  3. planner  - fake llm tra JSON 2 buoc -> parse dung; tra rac -> fallback; raise -> fallback.
  4. runner   - fake brain sync: run 3 steps -> DONE + checkpoint persist;
                fail o step 2 -> FAILED, step 3 khong chay; resume sau fix -> DONE,
                step 1 khong chay lai.
  5. tools shape      - 5 keys; moi tool co name/func/description/schema; name khop key.
  6. tools end-to-end - fake env (root_dir, llm, brain): create/resume/status/list/cancel + errors.
  7. tools khong brain - task_resume -> {"error"} chua "brain".
  8. tools khong root_dir - build_tools(object()) -> task_list -> {"error"} chua "root_dir".

Chay: python3 evals/test_tasks.py  (tu ~/workspace/boom-mods/tasks-verify/tasks/)
Chi stdlib. Exit 1 neu co fail.
"""

import json
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent" / "src"))

from tasks.models import Task, TaskStatus, StepStatus, TaskStep
from tasks.planner import plan_goal
from tasks.runner import TaskRunner
from tasks.store import TaskStore
from tasks.tools import build_tools

PASSES = 0
FAILURES = []


def check(name, cond):
    global PASSES
    if cond:
        PASSES += 1
        print(f"PASS: {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL: {name}")


class OkBrain:
    """Fake brain sync: ghi lai desc da chay, luon tra 'ok'."""

    def __init__(self):
        self.calls = []

    def run_brain(self, desc, strategy="auto"):
        self.calls.append(desc)
        return "ok"


def _linear3(tid):
    return Task(
        id=tid,
        title=f"title-{tid}",
        goal="linear goal",
        steps=[
            TaskStep(id="1", desc="step 1"),
            TaskStep(id="2", desc="step 2", depends_on=["1"]),
            TaskStep(id="3", desc="step 3", depends_on=["2"]),
        ],
    )


def part_models():
    t = Task(
        id="m1",
        title="T",
        goal="G",
        status=TaskStatus.RUNNING,
        steps=[
            TaskStep(id="1", desc="a"),
            TaskStep(id="2", desc="b", depends_on=["1"], status=StepStatus.DONE, result="ok"),
            TaskStep(id="3", desc="c", depends_on=["1", "2"]),
        ],
        created_at="2026-10-15T00:00:00",
        updated_at="2026-10-15T00:01:00",
    )
    rt = Task.from_dict(json.loads(json.dumps(t.to_dict())))
    check("models: round-trip Task 3 steps qua json deep-equal", rt.to_dict() == t.to_dict())

    weird = Task.from_dict(
        {
            "id": "w",
            "title": "",
            "goal": "",
            "status": "trang-thai-la",
            "steps": [{"id": "1", "desc": "d", "status": "khong-biet"}],
        }
    )
    check(
        "models: status la -> PENDING (task + step)",
        weird.status == TaskStatus.PENDING and weird.steps[0].status == StepStatus.PENDING,
    )


def part_store(tmp):
    db = tmp / "store.db"
    store = TaskStore(db)
    task = _linear3("s1")
    store.create(task)
    got = store.get("s1")
    check("store: create + get round-trip deep-equal", got is not None and got.to_dict() == task.to_dict())
    check("store: get khong ton tai -> None", store.get("nope") is None)

    store.create(Task(id="s2", title="t2", goal="g", status=TaskStatus.DONE, steps=[]))
    done_only = store.list(status="done")
    check("store: list filter status='done'", [t.id for t in done_only] == ["s2"])
    check("store: list tat ca", len(store.list()) == 2)

    task.title = "doi-title"
    store.save(task)
    check("store: save cap nhat", store.get("s1").title == "doi-title")

    check("store: update_step task co -> True", store.update_step("s1", "1", StepStatus.DONE, result="r1"))
    got = store.get("s1")
    st1 = [s for s in got.steps if s.id == "1"][0]
    check("store: update_step persist status+result", st1.status == StepStatus.DONE and st1.result == "r1")
    check("store: update_step task khong ton tai -> False", store.update_step("nope", "1", "done") is False)
    check("store: update_step step khong ton tai -> False", store.update_step("s1", "zz", "done") is False)


def part_planner():
    two = plan_goal(
        "g",
        lambda prompt: '[{"id": "1", "desc": "A", "depends_on": []}, '
        '{"id": "2", "desc": "B", "depends_on": ["1"]}]',
    )
    check(
        "planner: fake llm JSON 2 buoc -> parse dung",
        len(two) == 2
        and two[0].id == "1"
        and two[0].desc == "A"
        and two[1].depends_on == ["1"],
    )

    garbage = plan_goal("my goal", lambda prompt: "xin chao, day khong phai json")
    check(
        "planner: fake llm tra rac -> fallback 1 step",
        len(garbage) == 1 and garbage[0].id == "1" and garbage[0].desc == "my goal",
    )

    def _boom(prompt):
        raise RuntimeError("llm chet")

    raised = plan_goal("my goal", _boom)
    check(
        "planner: fake llm raise -> fallback 1 step",
        len(raised) == 1 and raised[0].id == "1" and raised[0].desc == "my goal",
    )


def part_runner(tmp):
    db = tmp / "run.db"
    s = TaskStore(db)

    # 4a. 3 steps tuyen tinh -> DONE, checkpoint persist doc duoc tu store thu 2
    s.create(_linear3("r1"))
    brain = OkBrain()
    out = TaskRunner(s, brain).run("r1")
    check("runner: 3 steps tuyen tinh -> DONE", out.status == TaskStatus.DONE)
    check("runner: brain chay du 3 steps dung thu tu", brain.calls == ["step 1", "step 2", "step 3"])
    s2 = TaskStore(db)
    got = s2.get("r1")
    check(
        "runner: checkpoint persist (store thu 2 doc duoc)",
        got is not None
        and got.status == TaskStatus.DONE
        and all(st.status == StepStatus.DONE for st in got.steps),
    )

    # 4b. fail o step 2 -> FAILED, step 3 khong chay
    class FailBrain(OkBrain):
        def run_brain(self, desc, strategy="auto"):
            self.calls.append(desc)
            if desc == "step 2":
                raise RuntimeError("nope")
            return "ok"

    s.create(_linear3("r2"))
    fail_brain = FailBrain()
    out2 = TaskRunner(s, fail_brain).run("r2")
    check("runner: fail o step 2 -> task FAILED", out2.status == TaskStatus.FAILED)
    st2 = {x.id: x for x in out2.steps}
    check(
        "runner: step 2 FAILED, step 3 khong chay (PENDING)",
        st2["2"].status == StepStatus.FAILED
        and st2["2"].error.startswith("RuntimeError:")
        and st2["3"].status == StepStatus.PENDING
        and fail_brain.calls == ["step 1", "step 2"],
    )

    # 4c. resume sau khi fix -> DONE, step 1 khong chay lai
    brain2 = OkBrain()
    out3 = TaskRunner(s, brain2).resume("r2")
    check("runner: resume sau fix -> DONE", out3.status == TaskStatus.DONE)
    check("runner: resume khong chay lai step 1", brain2.calls == ["step 2", "step 3"])


def part_tools_shape(tmp):
    tools = build_tools(SimpleNamespace(root_dir=str(tmp / "env_shape")))
    check(
        "tools: build_tools tra dung 5 keys",
        set(tools.keys()) == {"task_create", "task_list", "task_status", "task_resume", "task_cancel"},
    )
    ok = True
    for k, td in tools.items():
        if not isinstance(td, dict):
            ok = False
            break
        if not ({"name", "func", "description", "schema"} <= set(td.keys())):
            ok = False
            break
        if td["name"] != k or not callable(td["func"]):
            ok = False
            break
        if not isinstance(td["schema"], dict) or "properties" not in td["schema"]:
            ok = False
            break
    check("tools: moi tool co du name/func/description/schema, name khop key", ok)


def part_tools_e2e(tmp):
    root = tmp / "env6"
    llm = lambda prompt: (  # noqa: E731
        '[{"id": "1", "desc": "plan A", "depends_on": []}, '
        '{"id": "2", "desc": "plan B", "depends_on": ["1"]}]'
    )
    t = build_tools(SimpleNamespace(root_dir=str(root), llm=llm, brain=OkBrain()))

    res = t["task_create"]["func"](goal="lam banh mi")
    check("tools e2e: task_create -> task co 2 steps", "task" in res and len(res["task"]["steps"]) == 2)
    tid = res["task"]["id"]
    check("tools e2e: title mac dinh = 60 ky tu dau goal", res["task"]["title"] == "lam banh mi")
    check("tools e2e: db lazy tao tai memories/tasks.db", (root / "memories" / "tasks.db").exists())

    res2 = t["task_resume"]["func"](task_id=tid)
    check("tools e2e: task_resume -> DONE", res2.get("task", {}).get("status") == "done")

    res3 = t["task_status"]["func"](task_id=tid)
    check("tools e2e: task_status -> DONE", res3.get("task", {}).get("status") == "done")

    res4 = t["task_list"]["func"](status="done")
    check(
        "tools e2e: task_list(status='done') -> total 1",
        res4.get("total") == 1 and len(res4.get("tasks", [])) == 1,
    )
    res4b = t["task_list"]["func"]()
    check("tools e2e: task_list() khong filter -> total 1", res4b.get("total") == 1)

    check("tools e2e: task_list(status='weird') -> error", "error" in t["task_list"]["func"](status="weird"))
    check("tools e2e: task_create('') -> error", "error" in t["task_create"]["func"](goal=""))
    check("tools e2e: task_create('   ') -> error", "error" in t["task_create"]["func"](goal="   "))
    check("tools e2e: task_status('nope') -> error", "error" in t["task_status"]["func"](task_id="nope"))
    check("tools e2e: task_resume('nope') -> error", "error" in t["task_resume"]["func"](task_id="nope"))

    rc = t["task_create"]["func"](goal="task de cancel")
    resc = t["task_cancel"]["func"](task_id=rc["task"]["id"])
    check("tools e2e: task_cancel -> CANCELLED", resc.get("task", {}).get("status") == "cancelled")
    check("tools e2e: task_cancel('nope') -> error", "error" in t["task_cancel"]["func"](task_id="nope"))


def part_tools_no_brain(tmp):
    t = build_tools(SimpleNamespace(root_dir=str(tmp / "env7")))
    rc = t["task_create"]["func"](goal="goal khong llm")
    check(
        "tools no-brain: task_create fallback 1 step desc=goal",
        "task" in rc and len(rc["task"]["steps"]) == 1 and rc["task"]["steps"][0]["desc"] == "goal khong llm",
    )
    rr = t["task_resume"]["func"](task_id=rc["task"]["id"])
    check(
        "tools no-brain: task_resume -> error chua 'brain'",
        "error" in rr and "brain" in rr["error"],
    )


def part_tools_no_root():
    t = build_tools(object())
    r = t["task_list"]["func"]()
    check(
        "tools no-root: task_list -> error chua 'root_dir'",
        "error" in r and "root_dir" in r["error"],
    )
    r2 = t["task_create"]["func"](goal="g")
    check(
        "tools no-root: task_create -> error chua 'root_dir'",
        "error" in r2 and "root_dir" in r2["error"],
    )


def main():
    tmp = Path(tempfile.mkdtemp(prefix="boom_tasks_total_"))
    try:
        print("== 1. models ==")
        part_models()
        print("== 2. store ==")
        part_store(tmp)
        print("== 3. planner ==")
        part_planner()
        print("== 4. runner ==")
        part_runner(tmp)
        print("== 5. tools shape ==")
        part_tools_shape(tmp)
        print("== 6. tools end-to-end ==")
        part_tools_e2e(tmp)
        print("== 7. tools khong brain ==")
        part_tools_no_brain(tmp)
        print("== 8. tools khong root_dir ==")
        part_tools_no_root()
    except Exception:
        traceback.print_exc()
        FAILURES.append("uncaught-exception")

    print(f"\nTOTAL: {PASSES} PASS, {len(FAILURES)} FAIL")
    if FAILURES:
        print("FAILED:", FAILURES)
        sys.exit(1)


if __name__ == "__main__":
    main()
