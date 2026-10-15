"""SQLite-backed append-only audit store. Thread-safe.

Deliberately no delete() or update(): an audit log you can rewrite is
not an audit trail. Retention/purging, if ever needed, is an ops decision
outside this module.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

from audit.models import AuditEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    channel TEXT NOT NULL,
    details_json TEXT NOT NULL,
    result TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events (ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_events (actor);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events (action);
"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _gen_id() -> str:
    return f"aud_{int(time.time() * 1000)}_{secrets.token_hex(2)}"


class AuditStore:
    """Append-only audit store, thread-safe via threading.Lock."""

    def __init__(self, db_path: Union[str, Path]):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> AuditEvent:
        return AuditEvent.from_dict(
            {
                "id": row["id"],
                "ts": row["ts"],
                "actor": row["actor"],
                "action": row["action"],
                "target": row["target"],
                "channel": row["channel"],
                "details": row["details_json"],
                "result": row["result"],
            }
        )

    def log(self, event: AuditEvent) -> str:
        """Append one event. Generates id/ts when empty. Returns the id."""
        if not event.id:
            event.id = _gen_id()
        if not event.ts:
            event.ts = _now_iso()
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM audit_events WHERE id = ?", (event.id,))
            if cur.fetchone() is not None:
                raise ValueError(f"audit id already exists: {event.id!r}")
            self._conn.execute(
                "INSERT INTO audit_events (id, ts, actor, action, target, channel, details_json, result)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.id, event.ts, event.actor, event.action, event.target,
                    event.channel, json.dumps(event.details, ensure_ascii=False), event.result,
                ),
            )
            self._conn.commit()
        return event.id

    def get(self, event_id: str) -> Optional[AuditEvent]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM audit_events WHERE id = ?", (event_id,))
            row = cur.fetchone()
        return self._row_to_event(row) if row else None

    def query(
        self,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        channel: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
    ) -> List[AuditEvent]:
        """Filter events. since/until are ISO-8601 strings (lexicographic compare)."""
        clauses, params = [], []
        if actor:
            clauses.append("actor = ?"); params.append(actor)
        if action:
            clauses.append("action = ?"); params.append(action)
        if channel:
            clauses.append("channel = ?"); params.append(channel)
        if since:
            clauses.append("ts >= ?"); params.append(since)
        if until:
            clauses.append("ts <= ?"); params.append(until)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            cur = self._conn.execute(
                f"SELECT * FROM audit_events {where} ORDER BY ts DESC, id DESC LIMIT ?",
                (*params, limit),
            )
            rows = cur.fetchall()
        return [self._row_to_event(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) AS n FROM audit_events")
            return int(cur.fetchone()["n"])
