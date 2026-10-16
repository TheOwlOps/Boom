"""Custom Bot Manager for Boom (desktop bot-backend).

Lets users create their own bots (Grok-style): each bot has its own name,
description, system prompt, avatar emoji, tool allow/deny lists and channel
bindings. Exactly one bot can be marked active at a time; the agent layer
reads the active bot and uses its system prompt + effective tool list.

Repo pattern (from agent/src/business/groups.py): SQLite + threading.Lock,
each operation opens a fresh connection (`with self._lock, self._connect()`),
list/dict fields stored as JSON, mutating ops return {"ok": True, ...} /
{"error": ...} dicts instead of raising, getters return None on miss.

Stdlib only (sqlite3 + json + threading + uuid + time).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now() -> str:
    """Current timestamp in ISO (local time), same format as groups.py."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _decode_json(value: Any, default: Any) -> Any:
    """Decodes JSON from the DB; corrupt data -> returns default, no crash."""
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _normalize_str_list(values: Any, field: str) -> List[str]:
    """Normalizes a tool/binding list into list[str]. Raises ValueError on bad input."""
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("%s must be a list of strings or None" % field)
    out: List[str] = []
    for v in values:
        s = str(v or "").strip()
        if s:
            out.append(s)
    # De-duplicate while preserving order.
    seen = set()
    deduped = [x for x in out if not (x in seen or seen.add(x))]  # type: ignore[func-returns-value]
    return deduped


class BotManager:
    """Manages user-defined custom bots, SQLite backend. Thread-safe."""

    #: Fields that update_bot() is allowed to change.
    UPDATABLE_FIELDS = frozenset(
        {"name", "description", "system_prompt", "avatar_emoji",
         "allowed_tools", "denied_tools", "channel_bindings"}
    )

    def __init__(self, db_path: Any = None) -> None:
        self.db_path = Path(db_path) if db_path is not None \
            else Path.home() / ".boom" / "bots.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS bots (
                       id TEXT PRIMARY KEY,
                       name TEXT NOT NULL,
                       description TEXT NOT NULL DEFAULT '',
                       system_prompt TEXT NOT NULL DEFAULT '',
                       avatar_emoji TEXT NOT NULL DEFAULT '🤖',
                       allowed_tools TEXT NOT NULL DEFAULT '[]',
                       denied_tools TEXT NOT NULL DEFAULT '[]',
                       channel_bindings TEXT NOT NULL DEFAULT '[]',
                       created_at TEXT NOT NULL,
                       updated_at TEXT NOT NULL,
                       is_active INTEGER NOT NULL DEFAULT 0
                   )""")

    # ------------------------------------------------------------ internals

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "system_prompt": row["system_prompt"],
            "avatar_emoji": row["avatar_emoji"],
            "allowed_tools": _decode_json(row["allowed_tools"], []),
            "denied_tools": _decode_json(row["denied_tools"], []),
            "channel_bindings": _decode_json(row["channel_bindings"], []),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "is_active": bool(row["is_active"]),
        }

    def _get_row(self, conn: sqlite3.Connection,
                 bot_id: str) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()

    # ------------------------------------------------------------ CRUD

    def create_bot(self, name: str, description: str = "",
                   system_prompt: str = "", avatar_emoji: str = "🤖",
                   allowed_tools: Optional[List[str]] = None,
                   denied_tools: Optional[List[str]] = None,
                   channel_bindings: Optional[List[str]] = None) -> Dict[str, Any]:
        """Creates a new bot. Returns {"ok": True, "bot": {...}} or {"error": ...}.

        allowed_tools=None or [] means "all tools" (deny-only mode).
        """
        if not isinstance(name, str) or not name.strip():
            return {"error": "name must be a non-empty string"}
        if not isinstance(description, str):
            return {"error": "description must be a string"}
        if not isinstance(system_prompt, str):
            return {"error": "system_prompt must be a string"}
        if not isinstance(avatar_emoji, str):
            return {"error": "avatar_emoji must be a string"}
        try:
            allowed = _normalize_str_list(allowed_tools, "allowed_tools")
            denied = _normalize_str_list(denied_tools, "denied_tools")
            bindings = _normalize_str_list(channel_bindings, "channel_bindings")
        except ValueError as e:
            return {"error": str(e)}

        bot_id = uuid.uuid4().hex
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO bots (id, name, description, system_prompt,"
                " avatar_emoji, allowed_tools, denied_tools, channel_bindings,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (bot_id, name.strip(), description, system_prompt,
                 avatar_emoji or "🤖",
                 json.dumps(allowed), json.dumps(denied), json.dumps(bindings),
                 now, now))
            row = self._get_row(conn, bot_id)
        return {"ok": True, "bot": self._row_to_dict(row)}

    def list_bots(self) -> List[Dict[str, Any]]:
        """Lists all bots, active bot first, then by name."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM bots ORDER BY is_active DESC, name, id").fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_bot(self, bot_id: Any) -> Optional[Dict[str, Any]]:
        """Returns one bot as a dict. Missing/blank id -> None."""
        if not isinstance(bot_id, str) or not bot_id.strip():
            return None
        with self._lock, self._connect() as conn:
            row = self._get_row(conn, bot_id.strip())
        return self._row_to_dict(row) if row else None

    def update_bot(self, bot_id: Any, **fields: Any) -> Dict[str, Any]:
        """Partial update of a bot. Only fields in UPDATABLE_FIELDS are honored;
        unknown fields are ignored. Returns {"ok": True, "bot": {...}} or
        {"error": ...} (missing bot, empty update, or bad field value)."""
        if not isinstance(bot_id, str) or not bot_id.strip():
            return {"error": "bot_id must be a non-empty string"}
        bot_id = bot_id.strip()

        updates: Dict[str, Any] = {}
        for key, value in fields.items():
            if key not in self.UPDATABLE_FIELDS:
                continue  # silently ignore unknown fields
            if key == "name":
                if not isinstance(value, str) or not value.strip():
                    return {"error": "name must be a non-empty string"}
                updates[key] = value.strip()
            elif key in ("description", "system_prompt"):
                if not isinstance(value, str):
                    return {"error": "%s must be a string" % key}
                updates[key] = value
            elif key == "avatar_emoji":
                if not isinstance(value, str):
                    return {"error": "avatar_emoji must be a string"}
                updates[key] = value or "🤖"
            else:  # allowed_tools / denied_tools / channel_bindings
                try:
                    updates[key] = json.dumps(
                        _normalize_str_list(value, key))
                except ValueError as e:
                    return {"error": str(e)}
        if not updates:
            return {"error": "no updatable fields given"}

        updates["updated_at"] = _now()
        set_clause = ", ".join("%s = ?" % k for k in updates)
        params = list(updates.values()) + [bot_id]
        with self._lock, self._connect() as conn:
            row = self._get_row(conn, bot_id)
            if row is None:
                return {"error": "bot not found: %r" % bot_id}
            conn.execute("UPDATE bots SET %s WHERE id = ?" % set_clause, params)
            row = self._get_row(conn, bot_id)
        return {"ok": True, "bot": self._row_to_dict(row)}

    def delete_bot(self, bot_id: Any) -> bool:
        """Deletes a bot. Returns True if a row was removed, False if missing.

        Deleting the active bot simply leaves no active bot (get_active_bot()
        returns None afterwards); nothing else is affected."""
        if not isinstance(bot_id, str) or not bot_id.strip():
            return False
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM bots WHERE id = ?",
                               (bot_id.strip(),))
        return cur.rowcount > 0

    # ------------------------------------------------------------ active bot

    def set_active_bot(self, bot_id: Any) -> bool:
        """Marks one bot active and clears the flag on all others.
        Unknown bot -> False (active bot stays as-is)."""
        if not isinstance(bot_id, str) or not bot_id.strip():
            return False
        bot_id = bot_id.strip()
        with self._lock, self._connect() as conn:
            if self._get_row(conn, bot_id) is None:
                return False
            conn.execute("UPDATE bots SET is_active = 0")
            conn.execute("UPDATE bots SET is_active = 1 WHERE id = ?",
                         (bot_id,))
        return True

    def get_active_bot(self) -> Optional[Dict[str, Any]]:
        """Returns the active bot, or None if none is marked active."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM bots WHERE is_active = 1 LIMIT 1").fetchone()
        return self._row_to_dict(row) if row else None

    # ------------------------------------------------------------ tools

    def get_effective_tools(self, bot_id: Any,
                            all_tool_names: List[str]) -> List[str]:
        """Computes the tools a bot may use: allowed minus denied.

        Rules (never raises):
        - Unknown bot -> fail-open: returns all_tool_names unchanged, so the
          pipeline is never blocked by a missing bot record.
        - allowed_tools empty -> start from all_tool_names; otherwise from
          allowed_tools intersected with all_tool_names (unknown tool names
          in the list are dropped).
        - denied_tools always wins: removes any matching name.
        - Order follows all_tool_names.
        """
        try:
            all_names = [str(n) for n in (all_tool_names or [])]
            bot = self.get_bot(bot_id)
            if bot is None:
                return list(all_names)
            allowed = bot.get("allowed_tools") or []
            denied = set(bot.get("denied_tools") or [])
            if allowed:
                pool = [n for n in all_names if n in set(allowed)]
            else:
                pool = list(all_names)
            return [n for n in pool if n not in denied]
        except Exception:
            return list(all_tool_names or [])


__all__ = ["BotManager"]
