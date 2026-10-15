"""SQLite-backed task store. Thread-safe, WAL khong can — dung lock + commit sau moi write."""

import json
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

from tasks.models import Task, TaskStatus, StepStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _gen_id() -> str:
    """Id duy nhat: millisecond + 4 hex ngau nhien (tranh dung do cung-ms)."""
    return f"task_{int(time.time() * 1000)}_{secrets.token_hex(2)}"


class TaskStore:
    """TaskStore luu SQLite, thread-safe qua threading.Lock."""

    def __init__(self, db_path: Union[str, Path]):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        steps = json.loads(row["steps_json"])
        return Task.from_dict(
            {
                "id": row["id"],
                "title": row["title"],
                "goal": row["goal"],
                "status": row["status"],
                "steps": steps,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    def create(self, task: Task) -> str:
        """INSERT task moi. Sinh id neu rong; id da ton tai -> ValueError. Commit ngay.

        Id tu sinh co hau to ngau nhien + retry tranh dung do 2 task tao cung millisecond.
        """
        with self._lock:
            auto = not task.id
            if auto:
                task.id = _gen_id()
            for _ in range(10):
                cur = self._conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task.id,))
                if cur.fetchone() is None:
                    break
                if not auto:
                    raise ValueError(f"task id already exists: {task.id!r}")
                task.id = _gen_id()  # dung do hiem: sinh lai
            else:
                raise ValueError(f"could not generate a unique task id")
            now = _now_iso()
            if not task.created_at:
                task.created_at = now
            if not task.updated_at:
                task.updated_at = now
            self._conn.execute(
                "INSERT INTO tasks (id, title, goal, status, steps_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task.id,
                    task.title,
                    task.goal,
                    task.status.value,
                    json.dumps([s.to_dict() for s in task.steps]),
                    task.created_at,
                    task.updated_at,
                ),
            )
            self._conn.commit()
            return task.id

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            return self._row_to_task(row) if row is not None else None

    def list(self, status: Optional[Union[TaskStatus, str]] = None) -> List[Task]:
        if status is None:
            st_val: Optional[str] = None
        elif isinstance(status, TaskStatus):
            st_val = status.value
        elif isinstance(status, str):
            try:
                st_val = TaskStatus(status).value
            except ValueError:
                raise ValueError(f"unknown task status: {status!r}")
        else:
            raise ValueError(f"unknown task status: {status!r}")
        with self._lock:
            if st_val is None:
                cur = self._conn.execute("SELECT * FROM tasks ORDER BY created_at")
                rows = cur.fetchall()
            else:
                cur = self._conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY created_at",
                    (st_val,),
                )
                rows = cur.fetchall()
        return [self._row_to_task(r) for r in rows]

    def save(self, task: Task) -> None:
        """INSERT OR REPLACE toan bo (upsert). id rong -> ValueError. Commit ngay."""
        if not task.id:
            raise ValueError("task.id is empty")
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks"
                " (id, title, goal, status, steps_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task.id,
                    task.title,
                    task.goal,
                    task.status.value,
                    json.dumps([s.to_dict() for s in task.steps]),
                    task.created_at or _now_iso(),
                    task.updated_at or _now_iso(),
                ),
            )
            self._conn.commit()

    def update_step(
        self,
        task_id: str,
        step_id: str,
        status: Union[StepStatus, str],
        result: str = "",
        error: str = "",
    ) -> bool:
        """Checkpoint sau moi buoc: cap nhat step + updated_at, commit ngay.
        Task/step khong ton tai -> False."""
        if isinstance(status, StepStatus):
            st = status
        elif isinstance(status, str):
            try:
                st = StepStatus(status)
            except ValueError:
                raise ValueError(f"unknown step status: {status!r}")
        else:
            raise ValueError(f"unknown step status: {status!r}")
        with self._lock:
            cur = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if row is None:
                return False
            task = self._row_to_task(row)
            target = None
            for s in task.steps:
                if s.id == step_id:
                    target = s
                    break
            if target is None:
                return False
            target.status = st
            target.result = result
            target.error = error
            task.updated_at = _now_iso()
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks"
                " (id, title, goal, status, steps_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task.id,
                    task.title,
                    task.goal,
                    task.status.value,
                    json.dumps([s.to_dict() for s in task.steps]),
                    task.created_at or _now_iso(),
                    task.updated_at,
                ),
            )
            self._conn.commit()
            return True

    def close(self) -> None:
        with self._lock:
            self._conn.close()
