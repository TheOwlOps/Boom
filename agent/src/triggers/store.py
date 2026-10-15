"""SQLite-backed trigger store. Thread-safe, lock + commit sau moi write."""

import json
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

from triggers.models import Trigger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS triggers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    config_json TEXT NOT NULL,
    action_json TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    cooldown_sec INTEGER NOT NULL,
    last_fired_at TEXT,
    created_at TEXT NOT NULL
)
"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _gen_id() -> str:
    """Id duy nhat: millisecond + 4 hex ngau nhien (tranh dung do cung-ms)."""
    return f"trg_{int(time.time() * 1000)}_{secrets.token_hex(2)}"


class TriggerStore:
    """TriggerStore luu SQLite, thread-safe qua threading.Lock."""

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

    def _row_to_trigger(self, row: sqlite3.Row) -> Trigger:
        return Trigger.from_dict(
            {
                "id": row["id"],
                "name": row["name"],
                "kind": row["kind"],
                "config": json.loads(row["config_json"]),
                "action": json.loads(row["action_json"]),
                "enabled": bool(row["enabled"]),
                "cooldown_sec": row["cooldown_sec"],
                "last_fired_at": row["last_fired_at"],
                "created_at": row["created_at"],
            }
        )

    def add(self, trigger: Trigger) -> str:
        """INSERT trigger moi. Sinh id `trg_<ms>_<4hex>` neu rong;
        id tuong minh da ton tai -> ValueError. Commit ngay."""
        with self._lock:
            auto = not trigger.id
            if auto:
                trigger.id = _gen_id()
            for _ in range(10):
                cur = self._conn.execute(
                    "SELECT 1 FROM triggers WHERE id = ?", (trigger.id,)
                )
                if cur.fetchone() is None:
                    break
                if not auto:
                    raise ValueError(f"trigger id already exists: {trigger.id!r}")
                trigger.id = _gen_id()  # dung do hiem: sinh lai
            else:
                raise ValueError("could not generate a unique trigger id")
            if not trigger.created_at:
                trigger.created_at = _now_iso()
            self._conn.execute(
                "INSERT INTO triggers"
                " (id, name, kind, config_json, action_json, enabled,"
                " cooldown_sec, last_fired_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trigger.id,
                    trigger.name,
                    trigger.kind,
                    json.dumps(trigger.config),
                    json.dumps(trigger.action),
                    1 if trigger.enabled else 0,
                    int(trigger.cooldown_sec),
                    trigger.last_fired_at,
                    trigger.created_at,
                ),
            )
            self._conn.commit()
            return trigger.id

    def get(self, trigger_id: str) -> Optional[Trigger]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM triggers WHERE id = ?", (trigger_id,)
            )
            row = cur.fetchone()
            return self._row_to_trigger(row) if row is not None else None

    def list(self, enabled_only: bool = False) -> List[Trigger]:
        with self._lock:
            if enabled_only:
                cur = self._conn.execute(
                    "SELECT * FROM triggers WHERE enabled = 1 ORDER BY created_at"
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM triggers ORDER BY created_at"
                )
            rows = cur.fetchall()
        return [self._row_to_trigger(r) for r in rows]

    def delete(self, trigger_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM triggers WHERE id = ?", (trigger_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def toggle(self, trigger_id: str) -> bool:
        """Dao enabled cua trigger. Khong ton tai -> False."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT enabled FROM triggers WHERE id = ?", (trigger_id,)
            )
            row = cur.fetchone()
            if row is None:
                return False
            new_val = 0 if row["enabled"] else 1
            self._conn.execute(
                "UPDATE triggers SET enabled = ? WHERE id = ?",
                (new_val, trigger_id),
            )
            self._conn.commit()
            return True

    def mark_fired(self, trigger_id: str, at: Optional[str] = None) -> bool:
        """Set last_fired_at = at hoac now iso. Khong ton tai -> False."""
        stamp = at or _now_iso()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE triggers SET last_fired_at = ? WHERE id = ?",
                (stamp, trigger_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
