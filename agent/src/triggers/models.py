"""Trigger models for Boom. Stdlib only, JSON-serializable.

A trigger = a periodically-checked condition (polled by worker checkers);
when the condition holds, an action runs (e.g. sends a message, runs a task).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


#: Valid trigger kinds. from_dict does NOT validate — checkers decide.
VALID_KINDS = frozenset({"gmail_new", "price_below", "keyword_news", "webhook"})


@dataclass
class Trigger:
    id: str
    name: str
    kind: str
    config: Dict[str, Any] = field(default_factory=dict)
    action: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    cooldown_sec: int = 3600
    last_fired_at: Optional[str] = None
    created_at: str = ""

    def __post_init__(self) -> None:
        # Defaults created_at to the creation moment (when empty).
        if not self.created_at:
            self.created_at = datetime.now().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "config": dict(self.config),
            "action": dict(self.action),
            "enabled": bool(self.enabled),
            "cooldown_sec": int(self.cooldown_sec),
            "last_fired_at": self.last_fired_at,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Trigger":
        # Kind is NOT validated here — the checker decides how to handle it.
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            kind=d.get("kind", ""),
            config=dict(d.get("config") or {}),
            action=dict(d.get("action") or {}),
            enabled=bool(d.get("enabled", True)),
            cooldown_sec=int(d.get("cooldown_sec", 3600)),
            last_fired_at=d.get("last_fired_at"),
            created_at=d.get("created_at", ""),
        )
