"""Kiem tra quyen: wildcard matching + require() raise ro rang."""

from typing import List, TYPE_CHECKING

from authz.models import User

if TYPE_CHECKING:
    from authz.store import AuthzStore


class PermissionDenied(Exception):
    """Raise khi user thieu quyen. Message ghi ro user + permission."""
    pass


def match_permission(granted: str, required: str) -> bool:
    """So khop 1 permission duoc cap voi permission yeu cau.

    - "*" khop moi thu.
    - "admin.*" khop "admin.users", "admin.anything" (khong khop "administrator.x").
    - Con lai: khop chinh xac.
    """
    granted = (granted or "").strip()
    required = (required or "").strip()
    if not granted or not required:
        return False
    if granted == "*":
        return True
    if granted.endswith(".*"):
        prefix = granted[:-2]
        return required == prefix or required.startswith(prefix + ".")
    return granted == required


def effective_permissions(user: User, store: "AuthzStore") -> List[str]:
    """Hop (union) permissions tu tat ca roles cua user. Role khong ton tai -> bo qua."""
    perms: List[str] = []
    seen = set()
    for role_name in user.roles or []:
        role = store.get_role(role_name)
        if role is None:
            continue
        for p in role.permissions or []:
            if p not in seen:
                seen.add(p)
                perms.append(p)
    return perms


def can(user: User, permission: str, store: "AuthzStore") -> bool:
    """User co quyen `permission` khong? Ho tro wildcard."""
    if not isinstance(permission, str) or not permission.strip():
        return False
    for granted in effective_permissions(user, store):
        if match_permission(granted, permission):
            return True
    return False


def require(user: User, permission: str, store: "AuthzStore") -> None:
    """Doi quyen; thieu -> raise PermissionDenied (khong tra False)."""
    if not can(user, permission, store):
        raise PermissionDenied(
            f"user {user.id!r} (roles: {', '.join(user.roles) or 'none'}) "
            f"lacks permission {permission!r}"
        )
