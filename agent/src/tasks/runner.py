"""Chay task tung buoc qua brain, checkpoint SQLite sau moi buoc.

Nguyen tac:
- Moi buoc duoc checkpoint qua TaskStore.update_step truoc va sau khi chay.
- Step fail -> task FAILED va DUNG, nhung KHONG bao gio crash ra ngoai
  (exception tu brain bi bat va ghi vao step.error dang "{Type}: {msg}").
- Cycle trong depends_on -> ValueError, nhung task duoc danh FAILED va
  persist TRUOC khi raise.
"""

import asyncio
import concurrent.futures
import inspect
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from tasks.models import Task, TaskStatus, StepStatus, TaskStep
from tasks.store import TaskStore


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _await_sync(awaitable):
    """Resolve ket qua brain du no la gia tri thuong hay coroutine.

    Brain that la async; brain test co the la sync. Neu dang o trong mot
    event loop (vd: goi tu code async), chay coroutine tren mot thread
    rieng de tranh deadlock voi loop hien tai.
    """
    if not inspect.isawaitable(awaitable):
        return awaitable
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, awaitable).result()


def _extract(res) -> Tuple[str, Optional[str]]:
    """Tra (text, error).

    Ho tro:
    - None -> ("", None)
    - str -> (str, None)
    - dict -> lay "text"/"error"
    - object co thuoc tinh .text/.error
    """
    if res is None:
        return "", None
    if isinstance(res, str):
        return res, None
    if isinstance(res, dict):
        err = res.get("error")
        if err:
            return "", str(err)
        return str(res.get("text", "")), None
    err = getattr(res, "error", None)
    if err:
        return "", str(err)
    text = getattr(res, "text", None)
    if text is None:
        return str(res), None
    return str(text), None


def _topo(task: Task) -> List[TaskStep]:
    """Sap xep topo theo depends_on (thuat toan Kahn).

    Dep id la (khong co trong steps) thi bo qua. Cycle -> ValueError.
    Giua cac buoc cung muc giu nguyen thu tu khai bao trong task.steps.
    """
    steps = list(task.steps)
    by_id: Dict[str, TaskStep] = {s.id: s for s in steps}
    indegree: Dict[str, int] = {s.id: 0 for s in steps}
    children: Dict[str, List[str]] = {s.id: [] for s in steps}
    for s in steps:
        for d in s.depends_on or []:
            if d in by_id:
                indegree[s.id] += 1
                children[d].append(s.id)
    queue: List[str] = [s.id for s in steps if indegree[s.id] == 0]
    order: List[TaskStep] = []
    while queue:
        sid = queue.pop(0)
        order.append(by_id[sid])
        for c in children[sid]:
            indegree[c] -= 1
            if indegree[c] == 0:
                queue.append(c)
    if len(order) != len(steps):
        raise ValueError("dependency cycle detected in task steps")
    return order


_TERMINAL_TASK = (TaskStatus.DONE, TaskStatus.CANCELLED)


class TaskRunner:
    """Chay task tung buoc qua brain voi checkpoint SQLite.

    brain: object co phuong thuc run_brain(prompt, strategy="auto"),
    co the la sync hoac async (duoc resolve qua _await_sync).
    """

    def __init__(self, store: TaskStore, brain):
        self._store = store
        self._brain = brain
        self._lock = threading.Lock()

    # ---------- public API ----------

    def run(self, task_id: str, on_progress: Optional[Callable[[Task, Optional[TaskStep]], None]] = None) -> Task:
        """Chay task. Step fail -> task FAILED va dung, khong crash."""
        with self._lock:
            return self._run_locked(task_id, on_progress)

    def resume(self, task_id: str, on_progress: Optional[Callable[[Task, Optional[TaskStep]], None]] = None) -> Task:
        """Tiep tuc task: bo qua step DONE, chay lai FAILED/PENDING/SKIPPED."""
        with self._lock:
            task = self._store.get(task_id)
            if task is None:
                raise ValueError(f"task not found: {task_id!r}")
            if task.status in _TERMINAL_TASK:
                return task
            return self._run_locked(task_id, on_progress)

    def pause(self, task_id: str) -> Optional[Task]:
        """Danh dau PAUSED. Task khong ton tai -> None."""
        with self._lock:
            task = self._store.get(task_id)
            if task is None:
                return None
            task.status = TaskStatus.PAUSED
            task.updated_at = _now_iso()
            self._store.save(task)
            return task

    def cancel(self, task_id: str) -> Optional[Task]:
        """Danh dau CANCELLED. Task khong ton tai -> None."""
        with self._lock:
            task = self._store.get(task_id)
            if task is None:
                return None
            task.status = TaskStatus.CANCELLED
            task.updated_at = _now_iso()
            self._store.save(task)
            return task

    # ---------- internals ----------

    def _run_locked(self, task_id: str, on_progress) -> Task:
        task = self._store.get(task_id)
        if task is None:
            raise ValueError(f"task not found: {task_id!r}")
        if task.status in _TERMINAL_TASK:
            return task

        task.status = TaskStatus.RUNNING
        task.updated_at = _now_iso()
        self._store.save(task)

        try:
            order = _topo(task)
        except ValueError:
            # Persist FAILED truoc khi bao cycle
            task.status = TaskStatus.FAILED
            task.updated_at = _now_iso()
            self._store.save(task)
            raise

        fresh = self._store.get(task_id)
        assert fresh is not None
        status_map: Dict[str, StepStatus] = {s.id: s.status for s in fresh.steps}
        by_id: Dict[str, TaskStep] = {s.id: s for s in fresh.steps}

        for step in order:
            cur = status_map.get(step.id, StepStatus.PENDING)
            if cur == StepStatus.DONE:
                continue
            dep_ids = [d for d in (step.depends_on or []) if d in by_id]
            if any(status_map.get(d) != StepStatus.DONE for d in dep_ids):
                # Dep chua xong -> bo qua buoc nay (khong crash, khong doi)
                self._store.update_step(
                    task_id, step.id, StepStatus.SKIPPED,
                    error="skipped: dependency not done",
                )
                status_map[step.id] = StepStatus.SKIPPED
                self._notify(on_progress, task_id, step.id)
                continue

            # Checkpoint RUNNING truoc khi goi brain
            self._store.update_step(task_id, step.id, StepStatus.RUNNING)
            status_map[step.id] = StepStatus.RUNNING

            try:
                res = _await_sync(self._brain.run_brain(step.desc, strategy="auto"))
            except Exception as exc:  # never crash vi step fail
                err = f"{type(exc).__name__}: {exc}"
                self._store.update_step(task_id, step.id, StepStatus.FAILED, error=err)
                status_map[step.id] = StepStatus.FAILED
                self._notify(on_progress, task_id, step.id)
                return self._fail_task(task_id)

            text, err = _extract(res)
            if err:
                self._store.update_step(task_id, step.id, StepStatus.FAILED, error=err)
                status_map[step.id] = StepStatus.FAILED
                self._notify(on_progress, task_id, step.id)
                return self._fail_task(task_id)

            self._store.update_step(task_id, step.id, StepStatus.DONE, result=text)
            status_map[step.id] = StepStatus.DONE
            self._notify(on_progress, task_id, step.id)

        # Xong: co step FAILED -> FAILED, nguoc lai -> DONE
        fresh = self._store.get(task_id)
        assert fresh is not None
        if any(s.status == StepStatus.FAILED for s in fresh.steps):
            fresh.status = TaskStatus.FAILED
        else:
            fresh.status = TaskStatus.DONE
        fresh.updated_at = _now_iso()
        self._store.save(fresh)
        return self._store.get(task_id)

    def _fail_task(self, task_id: str) -> Task:
        task = self._store.get(task_id)
        assert task is not None
        task.status = TaskStatus.FAILED
        task.updated_at = _now_iso()
        self._store.save(task)
        return self._store.get(task_id)

    def _notify(self, on_progress, task_id: str, step_id: str) -> None:
        """Goi callback voi task fresh; callback hong khong pha run."""
        if on_progress is None:
            return
        try:
            fresh_task = self._store.get(task_id)
            step = None
            if fresh_task is not None:
                for s in fresh_task.steps:
                    if s.id == step_id:
                        step = s
                        break
            on_progress(fresh_task, step)
        except Exception:
            pass
