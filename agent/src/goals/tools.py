"""Agent-facing tools for the goal ledger (goals.store.GoalStore).

Tool dicts follow the repo convention {"name", "func", "description", "schema"} and the
{"error": ...} failure envelope. Every tool returns a plain dict; nothing here raises
into the agent loop.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from goals.store import GoalStore, _goals_path


def _schema(properties: Dict[str, Any] = None, required: List[str] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or []}


def _resolve_root(env_or_root_dir) -> Optional[Path]:
    """Accept either an environment (has .root_dir, the repo convention) or a plain path."""
    root = getattr(env_or_root_dir, "root_dir", env_or_root_dir)
    return Path(root) if root is not None else None


def build_tools(env_or_root_dir) -> Dict[str, Dict[str, Any]]:
    """Tool dicts for the environment: goal_add, goal_list, goal_log, goal_pause,
    goal_complete, goal_get.

    Goals live at <root_dir>/memories/goals.json. Accepts the environment itself
    (environment.py calls factory(self)) or a root_dir path directly.
    """
    root_dir = _resolve_root(env_or_root_dir)
    store = GoalStore(_goals_path(root_dir)) if root_dir is not None else None

    def _need_store() -> Optional[Dict[str, Any]]:
        if store is None:
            return {"error": "environment has no root_dir: nowhere to keep goals"}
        return None

    def _add(title: str, description: str = "") -> Dict[str, Any]:
        err = _need_store()
        if err:
            return err
        try:
            return {"goal": store.add(title, description)}
        except ValueError as e:
            return {"error": str(e)}
        except OSError as e:
            return {"error": f"could not write the goals ledger: {e}"}

    def _list(status: Optional[str] = None) -> Dict[str, Any]:
        err = _need_store()
        if err:
            return err
        try:
            goals = store.list(status=status)
        except ValueError as e:
            return {"error": str(e)}
        return {"goals": goals, "total": len(goals)}

    def _log(goal_id: str, note: str) -> Dict[str, Any]:
        err = _need_store()
        if err:
            return err
        try:
            goal = store.log_progress(goal_id, note)
        except ValueError as e:
            return {"error": str(e)}
        except OSError as e:
            return {"error": f"could not write the goals ledger: {e}"}
        if goal is None:
            return {"error": f"no goal {goal_id!r}"}
        return {"goal": goal}

    def _set_status(goal_id: str, status: str) -> Dict[str, Any]:
        err = _need_store()
        if err:
            return err
        try:
            goal = store.set_status(goal_id, status)
        except ValueError as e:
            return {"error": str(e)}
        except OSError as e:
            return {"error": f"could not write the goals ledger: {e}"}
        if goal is None:
            return {"error": f"no goal {goal_id!r}"}
        return {"goal": goal}

    def _get(goal_id: str) -> Dict[str, Any]:
        err = _need_store()
        if err:
            return err
        goal = store.get(goal_id)
        if goal is None:
            return {"error": f"no goal {goal_id!r}"}
        return {"goal": goal}

    return {
        "goal_add": {
            "name": "goal_add",
            "func": _add,
            "description": (
                "Record a new long-term goal. Goals persist in memories/goals.json and keep "
                "a timestamped progress log. Returns the created goal."
            ),
            "schema": _schema(
                {
                    "title": {"type": "string", "description": "Short goal title, e.g. 'Ship the Boom web UI'."},
                    "description": {"type": "string", "description": "Longer detail; optional."},
                },
                ["title"],
            ),
        },
        "goal_list": {
            "name": "goal_list",
            "func": _list,
            "description": "List goals, optionally filtered by status ('active', 'paused', 'completed').",
            "schema": _schema(
                {
                    "status": {"type": "string",
                               "description": "Filter: 'active', 'paused' or 'completed'. Omit for all."},
                },
                [],
            ),
        },
        "goal_log": {
            "name": "goal_log",
            "func": _log,
            "description": "Append a timestamped progress note to a goal's log. Returns the updated goal.",
            "schema": _schema(
                {
                    "goal_id": {"type": "string", "description": "Goal id from goal_list."},
                    "note": {"type": "string", "description": "What happened / what was decided."},
                },
                ["goal_id", "note"],
            ),
        },
        "goal_pause": {
            "name": "goal_pause",
            "func": lambda goal_id: _set_status(goal_id, "paused"),
            "description": "Pause a goal without deleting it. Returns the updated goal.",
            "schema": _schema(
                {"goal_id": {"type": "string", "description": "Goal id from goal_list."}},
                ["goal_id"],
            ),
        },
        "goal_complete": {
            "name": "goal_complete",
            "func": lambda goal_id: _set_status(goal_id, "completed"),
            "description": "Mark a goal completed. Returns the updated goal.",
            "schema": _schema(
                {"goal_id": {"type": "string", "description": "Goal id from goal_list."}},
                ["goal_id"],
            ),
        },
        "goal_get": {
            "name": "goal_get",
            "func": _get,
            "description": "Fetch one goal with its full progress log.",
            "schema": _schema(
                {"goal_id": {"type": "string", "description": "Goal id from goal_list."}},
                ["goal_id"],
            ),
        },
    }
