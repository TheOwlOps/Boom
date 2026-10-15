"""Phan quyen RBAC cho Boom (doanh nghiep): ai duoc lam gi.

Chi stdlib, JSON-serializable, theo pattern cua tasks/models.py.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class Role:
    name: str
    permissions: List[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "permissions": list(self.permissions),
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Role":
        return cls(
            name=d.get("name", ""),
            permissions=list(d.get("permissions") or []),
            description=d.get("description", ""),
        )


@dataclass
class User:
    id: str
    name: str = ""
    roles: List[str] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "roles": list(self.roles),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "User":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            roles=list(d.get("roles") or []),
            created_at=d.get("created_at", ""),
        )


def default_roles() -> List[Role]:
    """3 role mac dinh cua Boom."""
    return [
        Role(
            name="admin",
            permissions=["*"],
            description="Toan quyen: quan tri user, role, moi chuc nang.",
        ),
        Role(
            name="operator",
            permissions=[
                "channel.send",
                "channel.react",
                "channel.edit",
                "channel.delete",
                "channel.sticker",
                "channel.info",
                "trigger.*",
                "task.*",
                "ocr.*",
            ],
            description="Van hanh hang ngay: gui tin, trigger, task, OCR.",
        ),
        Role(
            name="viewer",
            permissions=[
                "channel.info",
                "trigger.list",
                "task.status",
            ],
            description="Chi xem: info nhom/thanh vien, danh sach trigger, trang thai task.",
        ),
    ]


BUILTIN_ROLES = frozenset({"admin", "operator", "viewer"})
