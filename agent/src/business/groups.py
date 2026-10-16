"""Group Manager for Boom (business): manages groups across channels.

Registers groups (telegram/discord/zalo...), tracks message/member stats per
group, and attaches allow/deny command policies at the group level.

Repo pattern: SQLite + threading.Lock, each operation opens a fresh connection
(`with self._lock, self._connect()`), dict fields stored as JSON, errors
returned as {"error": ...} dicts instead of raising (getters return None).

Stdlib only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now() -> str:
    """Current timestamp in ISO (local time), same format as broadcast.py."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _normalize_command(command: Any) -> str:
    """Normalizes a command name for policy comparison.

    Strips whitespace, strips the leading "/" (Telegram-style "/help"), lowercases.
    """
    return str(command or "").strip().lstrip("/").lower()


def _normalize_command_list(values: Any) -> List[str]:
    """Normalizes a policy's command list into list[str]."""
    if not isinstance(values, list):
        return []
    return [_normalize_command(v) for v in values if _normalize_command(v)]


def _decode_json(value: Any, default: Any) -> Any:
    """Decodes JSON from the DB; corrupt data → returns default, no crash."""
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class GroupManager:
    """Manages groups per channel, SQLite backend. Thread-safe."""

    def __init__(self, db_path: Any) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS groups (
                       channel TEXT NOT NULL,
                       group_id TEXT NOT NULL,
                       name TEXT NOT NULL,
                       settings_json TEXT NOT NULL DEFAULT '{}',
                       message_count INTEGER NOT NULL DEFAULT 0,
                       member_count INTEGER NOT NULL DEFAULT 0,
                       policy_json TEXT NOT NULL DEFAULT '{}',
                       created_at TEXT NOT NULL,
                       last_active TEXT,
                       PRIMARY KEY (channel, group_id)
                   )""")

    # ------------------------------------------------------------ internals

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "channel": row["channel"],
            "group_id": row["group_id"],
            "name": row["name"],
            "settings": _decode_json(row["settings_json"], {}),
            "message_count": row["message_count"],
            "member_count": row["member_count"],
            "policy": _decode_json(row["policy_json"], {}),
            "created_at": row["created_at"],
            "last_active": row["last_active"],
        }

    @staticmethod
    def _validate_ident(channel: Any, group_id: Any) -> Optional[str]:
        """Validates channel/group_id; returns an error message or None."""
        if not isinstance(channel, str) or not channel.strip():
            return "channel must be a non-empty string"
        if not isinstance(group_id, str) or not group_id.strip():
            return "group_id must be a non-empty string"
        return None

    def _get_row(self, conn: sqlite3.Connection,
                 channel: str, group_id: str) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM groups WHERE channel = ? AND group_id = ?",
            (channel, group_id)).fetchone()

    # ------------------------------------------------------------ registration

    def register_group(self, channel: str, group_id: str, name: str,
                       settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Registers a new group. Duplicate (channel, group_id) → clear error, no overwrite."""
        err = self._validate_ident(channel, group_id)
        if err:
            return {"error": err}
        if not isinstance(name, str) or not name.strip():
            return {"error": "name must be a non-empty string"}
        if settings is not None and not isinstance(settings, dict):
            return {"error": "settings must be a dict or None"}
        try:
            settings_json = json.dumps(settings or {})
        except (TypeError, ValueError) as e:
            return {"error": "settings is not JSON-serializable: %s" % e}

        channel, group_id, name = channel.strip(), group_id.strip(), name.strip()
        with self._lock, self._connect() as conn:
            if self._get_row(conn, channel, group_id) is not None:
                return {"error": "group already registered: channel=%r group_id=%r"
                                 % (channel, group_id)}
            conn.execute(
                "INSERT INTO groups (channel, group_id, name, settings_json,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (channel, group_id, name, settings_json, _now()))
            row = self._get_row(conn, channel, group_id)
        return {"ok": True, "group": self._row_to_dict(row)}

    def unregister_group(self, channel: str, group_id: str) -> Dict[str, Any]:
        """Unregisters a group. Missing group → error."""
        err = self._validate_ident(channel, group_id)
        if err:
            return {"error": err}
        with self._lock, self._connect() as conn:
            row = self._get_row(conn, channel.strip(), group_id.strip())
            if row is None:
                return {"error": "group not registered: channel=%r group_id=%r"
                                 % (channel, group_id)}
            conn.execute(
                "DELETE FROM groups WHERE channel = ? AND group_id = ?",
                (channel.strip(), group_id.strip()))
        return {"ok": True}

    def list_groups(self, channel: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lists groups (with message_count/member_count stats).
        channel=None → all channels."""
        with self._lock, self._connect() as conn:
            if channel is None:
                rows = conn.execute(
                    "SELECT * FROM groups ORDER BY channel, group_id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM groups WHERE channel = ? ORDER BY group_id",
                    (channel,)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ------------------------------------------------------------ activity

    def record_message(self, channel: str, group_id: str) -> Dict[str, Any]:
        """Records 1 message: bumps message_count, updates last_active.

        Unregistered group → auto-created with a default name (the message
        pipeline must not be blocked by a newly appearing group).
        """
        err = self._validate_ident(channel, group_id)
        if err:
            return {"error": err}
        channel, group_id = channel.strip(), group_id.strip()
        with self._lock, self._connect() as conn:
            row = self._get_row(conn, channel, group_id)
            if row is None:
                # Auto-register a new group: default name "channel:group_id".
                conn.execute(
                    "INSERT INTO groups (channel, group_id, name, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (channel, group_id, "%s:%s" % (channel, group_id), _now()))
            conn.execute(
                "UPDATE groups SET message_count = message_count + 1,"
                " last_active = ? WHERE channel = ? AND group_id = ?",
                (_now(), channel, group_id))
            row = self._get_row(conn, channel, group_id)
        return {"ok": True, "group": self._row_to_dict(row)}

    def get_group_stats(self, channel: str, group_id: str) -> Optional[Dict[str, Any]]:
        """Stats for one group. Missing → None."""
        err = self._validate_ident(channel, group_id)
        if err:
            return None
        with self._lock, self._connect() as conn:
            row = self._get_row(conn, channel.strip(), group_id.strip())
        return self._row_to_dict(row) if row else None

    def set_member_count(self, channel: str, group_id: str, n: int) -> Dict[str, Any]:
        """Updates a group's member count. n must be an integer >= 0."""
        err = self._validate_ident(channel, group_id)
        if err:
            return {"error": err}
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            return {"error": "member count must be an integer >= 0"}
        with self._lock, self._connect() as conn:
            row = self._get_row(conn, channel.strip(), group_id.strip())
            if row is None:
                return {"error": "group not registered: channel=%r group_id=%r"
                                 % (channel, group_id)}
            conn.execute(
                "UPDATE groups SET member_count = ?"
                " WHERE channel = ? AND group_id = ?",
                (n, channel.strip(), group_id.strip()))
        return {"ok": True, "member_count": n}

    # ------------------------------------------------------------ policy

    def set_group_policy(self, channel: str, group_id: str,
                         policy: Dict[str, Any]) -> Dict[str, Any]:
        """Attaches a policy to a group. policy is a dict, e.g.:
        {"allowed_commands": ["help", "report"], "require_approval": ["broadcast"]}.
        """
        err = self._validate_ident(channel, group_id)
        if err:
            return {"error": err}
        if not isinstance(policy, dict):
            return {"error": "policy must be a dict"}
        try:
            policy_json = json.dumps(policy)
        except (TypeError, ValueError) as e:
            return {"error": "policy is not JSON-serializable: %s" % e}
        with self._lock, self._connect() as conn:
            row = self._get_row(conn, channel.strip(), group_id.strip())
            if row is None:
                return {"error": "group not registered: channel=%r group_id=%r"
                                 % (channel, group_id)}
            conn.execute(
                "UPDATE groups SET policy_json = ?"
                " WHERE channel = ? AND group_id = ?",
                (policy_json, channel.strip(), group_id.strip()))
        return {"ok": True, "policy": policy}

    def get_group_policy(self, channel: str, group_id: str) -> Dict[str, Any]:
        """Returns a group's policy; default {} (no policy → everything allowed)."""
        err = self._validate_ident(channel, group_id)
        if err:
            return {}
        with self._lock, self._connect() as conn:
            row = self._get_row(conn, channel.strip(), group_id.strip())
        if row is None:
            return {}
        policy = _decode_json(row["policy_json"], {})
        return policy if isinstance(policy, dict) else {}

    def is_command_allowed(self, channel: str, group_id: str, command: Any) -> bool:
        """Checks whether a command is allowed in a group per its policy.

        Logic (never raises):
        - No policy → everything allowed.
        - "denied_commands" contains the command → denied (highest priority).
        - "allowed_commands" present (list) → only commands in the list.
        - "require_approval" doesn't deny a command, only marks it as needing
          approval (approval is handled on another layer, e.g. Boom's
          ApprovalGate).
        """
        try:
            policy = self.get_group_policy(channel, group_id)
        except Exception:
            return True  # policy read failed → fail-open so the pipeline isn't blocked
        if not policy:
            return True

        cmd = _normalize_command(command)
        if not cmd:
            return False

        denied = _normalize_command_list(policy.get("denied_commands"))
        if cmd in denied:
            return False

        allowed = policy.get("allowed_commands")
        if isinstance(allowed, list):
            return cmd in _normalize_command_list(allowed)
        return True


__all__ = ["GroupManager"]
