"""Durable ledger of the owner's long-term goals.

Goals persist at <root_dir>/memories/goals.json as a plain JSON list, following the same
conventions as the cron ledger (tools.cronjob_tool.CronStore): an absent or corrupt file
reads as no goals, and every write goes through a beside-the-ledger temp file swapped in
with os.replace so a crash mid-write leaves the old file whole.

A goal is a dict:
    {"id", "title", "description", "status", "created_at", "updated_at",
     "progress_log": [{"at", "note"}]}
`status` is one of "active", "paused", "completed".
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("boom.goals")

GOALS_FILE = "goals.json"
STATUSES = ("active", "paused", "completed")


def _goals_path(root_dir) -> Path:
    """Where goals live, beside the other memories/ state (cf. cronjob_tool._jobs_path)."""
    return Path(root_dir) / "memories" / GOALS_FILE


class GoalStore:
    """Goal ledger. An absent or corrupt file reads as no goals; writes are atomic."""

    def __init__(self, goals_file: Optional[Path] = None,
                 clock: Callable[[], datetime] = datetime.now):
        self.goals_file = Path(goals_file) if goals_file else (Path.cwd() / "memories" / GOALS_FILE)
        self.clock = clock

    def list(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every goal in ledger order, optionally filtered by status. Never raises."""
        if not self.goals_file.exists():
            return []
        try:
            raw = json.loads(self.goals_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("goals ledger %s unreadable (%s); starting empty", self.goals_file, e)
            return []
        if not isinstance(raw, list):
            return []
        goals = [g for g in raw if isinstance(g, dict) and g.get("id")]
        if status is not None:
            self._check_status(status)
            goals = [g for g in goals if g.get("status") == status]
        return goals

    def get(self, goal_id: str) -> Optional[Dict[str, Any]]:
        """One goal by id. None when there is no such goal; never raises."""
        return next((g for g in self.list() if g["id"] == goal_id), None)

    def _save(self, goals: List[Dict[str, Any]]) -> None:
        self.goals_file.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the ledger and swap it in: a crash mid-write leaves the old file whole.
        tmp = self.goals_file.with_name(self.goals_file.name + ".tmp")
        tmp.write_text(json.dumps(goals, indent=2), encoding="utf-8")
        os.replace(tmp, self.goals_file)

    @staticmethod
    def _check_status(status: str) -> str:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
        return status

    def add(self, title: str, description: str = "",
            goal_id: Optional[str] = None) -> Dict[str, Any]:
        """Append a goal and return it.

        ValueError if the title is empty, the status would be invalid, or the id is taken.
        """
        if not (title or "").strip():
            raise ValueError("a goal needs a non-empty title")
        goals = self.list()
        gid = goal_id or f"goal_{int(self.clock().timestamp() * 1000)}"
        if any(g["id"] == gid for g in goals):
            raise ValueError(f"a goal with id {gid!r} already exists")
        now = self.clock().isoformat(timespec="seconds")
        goal = {
            "id": gid,
            "title": title.strip(),
            "description": (description or "").strip(),
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "progress_log": [],
        }
        goals.append(goal)
        self._save(goals)
        return goal

    def log_progress(self, goal_id: str, note: str) -> Optional[Dict[str, Any]]:
        """Append a timestamped progress note. None if the goal is gone.

        ValueError if the note is empty.
        """
        if not (note or "").strip():
            raise ValueError("a progress note cannot be empty")
        goals = self.list()
        for goal in goals:
            if goal["id"] == goal_id:
                log = goal.setdefault("progress_log", [])
                if not isinstance(log, list):
                    goal["progress_log"] = log = []
                log.append({"at": self.clock().isoformat(timespec="seconds"),
                            "note": note.strip()})
                goal["updated_at"] = self.clock().isoformat(timespec="seconds")
                self._save(goals)
                return goal
        return None

    def set_status(self, goal_id: str, status: str) -> Optional[Dict[str, Any]]:
        """Move a goal between active/paused/completed. None if the goal is gone.

        ValueError if the status is not one of STATUSES.
        """
        self._check_status(status)
        goals = self.list()
        for goal in goals:
            if goal["id"] == goal_id:
                goal["status"] = status
                goal["updated_at"] = self.clock().isoformat(timespec="seconds")
                self._save(goals)
                return goal
        return None

    def remove(self, goal_id: str) -> bool:
        """Delete a goal. False when there is no such goal."""
        goals = self.list()
        kept = [g for g in goals if g["id"] != goal_id]
        if len(kept) == len(goals):
            return False
        self._save(kept)
        return True
