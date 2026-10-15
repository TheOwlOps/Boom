"""Trigger models cho Boom. Chỉ stdlib, JSON-serializable.

Trigger = điều kiện kiểm tra định kỳ (do worker checkers tự poll),
khi điều kiện đúng thì thực hiện action (vd: gửi tin nhắn, chạy task).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


#: Các kind trigger hợp lệ. from_dict KHÔNG validate — checker tự xử.
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
        # Mặc định created_at = thời điểm tạo mới (nếu rỗng).
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
        # KHÔNG validate kind ở đây — checker tự quyết định xử lý.
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
