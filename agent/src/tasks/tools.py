"""Agent-facing tools for the durable task engine.

Tool dicts follow the repo convention {"name", "func", "description", "schema"} and the
{"error": ...} failure envelope. Every tool returns a plain dict; nothing here raises
into the agent loop.

Tasks live at <root_dir>/memories/tasks.db (SQLite, created lazily on first tool call).
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from tasks.models import Task, TaskStatus, StepStatus, TaskStep
from tasks.planner import plan_goal
from tasks.runner import TaskRunner
from tasks.store import TaskStore


def _schema(properties: Dict[str, Any] = None, required: List[str] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or []}


def _resolve_root(env_or_root_dir) -> Optional[Path]:
    """Accept either an environment (has .root_dir, the repo convention) or a plain path."""
    root = getattr(env_or_root_dir, "root_dir", env_or_root_dir)
    try:
        return Path(root) if root is not None else None
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def build_tools(env_or_root_dir) -> Dict[str, Dict[str, Any]]:
    """5 tools: task_create, task_list, task_status, task_resume, task_cancel.

    DB at <root_dir>/memories/tasks.db (created lazily on first tool call).
    llm comes from getattr(env, "llm", None) — plan_goal when present, otherwise
    a 1-step fallback with desc=goal.
    brain comes from getattr(env, "brain", None) — task_resume needs brain,
    returns {"error"} without it.
    """
    root_dir = _resolve_root(env_or_root_dir)

    def _need_store():
        if root_dir is None:
            return None, {"error": "environment has no root_dir: nowhere to keep tasks"}
        try:
            return TaskStore(root_dir / "memories" / "tasks.db"), None
        except OSError as e:
            return None, {"error": f"could not open the tasks db: {e}"}

    def _create(goal: str, title: str = "") -> Dict[str, Any]:
        store, err = _need_store()
        if err:
            return err
        if not goal or not goal.strip():
            return {"error": "goal is empty: nothing to plan"}
        try:
            llm = getattr(env_or_root_dir, "llm", None)
            if callable(llm):
                steps = plan_goal(goal, llm)
            else:
                steps = [TaskStep(id="1", desc=goal)]
        except ValueError as e:
            return {"error": str(e)}
        task = Task(
            id="",
            title=(title.strip() if title and title.strip() else goal.strip()[:60]),
            goal=goal,
            status=TaskStatus.PENDING,
            steps=steps,
            created_at=_now_iso(),
            updated_at=_now_iso(),
        )
        try:
            store.create(task)
        except ValueError as e:
            return {"error": str(e)}
        except OSError as e:
            return {"error": f"could not write the tasks db: {e}"}
        return {"task": task.to_dict()}

    def _list(status: Optional[str] = None) -> Dict[str, Any]:
        store, err = _need_store()
        if err:
            return err
        if status is not None:
            st_val = status.value if isinstance(status, TaskStatus) else status
            if st_val not in {s.value for s in TaskStatus}:
                return {"error": f"unknown task status: {status!r}"}
        try:
            tasks = store.list(status=status)
        except ValueError as e:
            return {"error": str(e)}
        return {"tasks": [t.to_dict() for t in tasks], "total": len(tasks)}

    def _status(task_id: str) -> Dict[str, Any]:
        store, err = _need_store()
        if err:
            return err
        task = store.get(task_id)
        if task is None:
            return {"error": f"no task {task_id!r}"}
        return {"task": task.to_dict()}

    def _resume(task_id: str) -> Dict[str, Any]:
        store, err = _need_store()
        if err:
            return err
        brain = getattr(env_or_root_dir, "brain", None)
        if brain is None:
            return {"error": "task engine has no brain to run steps"}
        try:
            task = TaskRunner(store, brain).resume(task_id)
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:  # never raises into the agent loop
            return {"error": f"task resume failed: {e}"}
        return {"task": task.to_dict()}

    def _cancel(task_id: str) -> Dict[str, Any]:
        store, err = _need_store()
        if err:
            return err
        task = store.get(task_id)
        if task is None:
            return {"error": f"no task {task_id!r}"}
        task.status = TaskStatus.CANCELLED
        task.updated_at = _now_iso()
        try:
            store.save(task)
        except (ValueError, OSError) as e:
            return {"error": f"could not write the tasks db: {e}"}
        return {"task": task.to_dict()}

    return {
        "task_create": {
            "name": "task_create",
            "func": _create,
            "description": (
                "Create a new durable task from a goal. The goal is split into steps via the "
                "environment's LLM planner (falls back to a single step holding the whole goal "
                "when no LLM is available). Returns the created task."
            ),
            "schema": _schema(
                {
                    "goal": {"type": "string",
                             "description": "The goal to accomplish, e.g. 'Book a flight to Da Nang'."},
                    "title": {"type": "string",
                              "description": "Short title; defaults to the first 60 chars of the goal."},
                },
                ["goal"],
            ),
        },
        "task_list": {
            "name": "task_list",
            "func": _list,
            "description": "List tasks, optionally filtered by status ('pending', 'running', 'paused', 'done', 'failed', 'cancelled').",
            "schema": _schema(
                {
                    "status": {"type": "string",
                               "description": "Filter: one of 'pending', 'running', 'paused', 'done', 'failed', 'cancelled'. Omit for all."},
                },
                [],
            ),
        },
        "task_status": {
            "name": "task_status",
            "func": _status,
            "description": "Fetch one task with its steps and their statuses/results.",
            "schema": _schema(
                {
                    "task_id": {"type": "string", "description": "Task id from task_list."},
                },
                ["task_id"],
            ),
        },
        "task_resume": {
            "name": "task_resume",
            "func": _resume,
            "description": (
                "Run or resume a task step by step through the engine brain. Steps already done "
                "are skipped; each step is checkpointed to SQLite. Returns the final task."
            ),
            "schema": _schema(
                {
                    "task_id": {"type": "string", "description": "Task id from task_list."},
                },
                ["task_id"],
            ),
        },
        "task_cancel": {
            "name": "task_cancel",
            "func": _cancel,
            "description": "Mark a task cancelled without deleting it. Returns the updated task.",
            "schema": _schema(
                {
                    "task_id": {"type": "string", "description": "Task id from task_list."},
                },
                ["task_id"],
            ),
        },
    }
