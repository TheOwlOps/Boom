"""AuditEvent model."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict


@dataclass
class AuditEvent:
    """One immutable audit record.

    action examples: "channel.send", "channel.kick", "channel.ban",
    "trigger.create", "authz.assign", "approval.resolve".
    result: "ok" or "error".
    """

    id: str = ""
    ts: str = ""            # ISO-8601, set by the store if empty
    actor: str = ""         # who did it: user id, "system", "cron:job_id"
    action: str = ""        # what happened
    target: str = ""        # what it happened to: chat id, trigger id, ...
    channel: str = ""       # telegram/discord/... ("" when not channel-related)
    details: Dict[str, Any] = field(default_factory=dict)
    result: str = "ok"      # "ok" | "error"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AuditEvent":
        details = d.get("details", {})
        if isinstance(details, str):
            details = json.loads(details)
        return cls(
            id=str(d.get("id", "")),
            ts=str(d.get("ts", "")),
            actor=str(d.get("actor", "")),
            action=str(d.get("action", "")),
            target=str(d.get("target", "")),
            channel=str(d.get("channel", "")),
            details=details if isinstance(details, dict) else {},
            result=str(d.get("result", "ok")),
        )
