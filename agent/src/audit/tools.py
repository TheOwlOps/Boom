"""Agent tools for the audit log: query and fetch events."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Union

from audit.store import AuditStore


def _resolve_root(env_or_root_dir: Any) -> Optional[Path]:
    if env_or_root_dir is None:
        return None
    if isinstance(env_or_root_dir, (str, Path)):
        return Path(env_or_root_dir)
    for attr in ("root_dir", "workspace", "home"):
        v = getattr(env_or_root_dir, attr, None)
        if v:
            return Path(v)
    if isinstance(env_or_root_dir, dict) and env_or_root_dir.get("root_dir"):
        return Path(env_or_root_dir["root_dir"])
    return None


def build_tools(env_or_root_dir: Any = None) -> Dict[str, Dict[str, Any]]:
    """2 tools: audit_query, audit_get.

    DB at <root_dir>/memories/audit.db (created lazily on first call).
    root_dir None -> every tool returns {"error"}.
    """
    root_dir = _resolve_root(env_or_root_dir)

    def _need_store():
        if root_dir is None:
            return None, {"error": "environment has no root_dir: nowhere to keep the audit log"}
        try:
            return AuditStore(root_dir / "memories" / "audit.db"), None
        except OSError as e:
            return None, {"error": "could not open the audit db: %s" % (e,)}

    def _query(
        actor: Optional[str] = None,
        action: Optional[str] = None,
        channel: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        store, err = _need_store()
        if err:
            return err
        try:
            events = store.query(actor=actor, action=action, channel=channel,
                                 since=since, until=until, limit=limit)
        except Exception as e:
            return {"error": "audit query failed: %s: %s" % (type(e).__name__, e)}
        return {"events": [ev.to_dict() for ev in events], "count": len(events)}

    def _get(event_id: str) -> Dict[str, Any]:
        store, err = _need_store()
        if err:
            return err
        if not (event_id or "").strip():
            return {"error": "event_id is empty"}
        try:
            ev = store.get(event_id.strip())
        except Exception as e:
            return {"error": "audit get failed: %s: %s" % (type(e).__name__, e)}
        if ev is None:
            return {"error": "no audit event with id %r" % (event_id,)}
        return {"event": ev.to_dict()}

    return {
        "audit_query": {
            "name": "audit_query",
            "func": _query,
            "description": "Search the append-only audit log (who did what, when).",
            "schema": {
                "type": "object",
                "properties": {
                    "actor": {"type": "string"},
                    "action": {"type": "string"},
                    "channel": {"type": "string"},
                    "since": {"type": "string", "description": "ISO-8601 lower bound"},
                    "until": {"type": "string", "description": "ISO-8601 upper bound"},
                    "limit": {"type": "integer", "default": 100},
                },
            },
        },
        "audit_get": {
            "name": "audit_get",
            "func": _get,
            "description": "Fetch one audit event by id.",
            "schema": {
                "type": "object",
                "properties": {"event_id": {"type": "string"}},
                "required": ["event_id"],
            },
        },
    }
