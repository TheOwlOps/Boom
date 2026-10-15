"""Agent-facing tools cho RBAC.

Tool dicts theo repo convention {"name", "func", "description", "schema"} va
failure envelope {"error": ...}. Moi tool tra ve dict thuan; khong raise
ra ngoai agent loop.

Du lieu luu tai <root_dir>/memories/authz.db (SQLite, tao lazy khi goi tool).
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from authz.models import Role, User
from authz.store import AuthzStore
from authz.checker import can, PermissionDenied


def _schema(properties: Dict[str, Any] = None,
            required: List[str] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties or {},
            "required": required or []}


def _resolve_root(env_or_root_dir) -> Optional[Path]:
    """Chap nhan environment (co .root_dir, convention cua repo) hoac path thuan."""
    root = getattr(env_or_root_dir, "root_dir", env_or_root_dir)
    try:
        return Path(root) if root is not None else None
    except (TypeError, ValueError):
        return None


def _store_for(env_or_root_dir) -> AuthzStore:
    root = _resolve_root(env_or_root_dir)
    if root is None:
        raise ValueError("cannot resolve root_dir for authz.db")
    return AuthzStore(root / "memories" / "authz.db")


def _err(msg: str) -> Dict[str, Any]:
    return {"error": msg}


def build_tools(env_or_root_dir=None):  # type: ignore[no-untyped-def]
    """Tra ve dict tools RBAC. Store duoc mo lazy moi lan goi tool."""

    def authz_add_user(user_id: str = "", name: str = "",
                       roles: List[str] = None) -> Dict[str, Any]:
        try:
            store = _store_for(env_or_root_dir)
            uid = store.add_user(User(id=(user_id or "").strip(),
                                      name=(name or "").strip(),
                                      roles=list(roles or [])))
            user = store.get_user(uid)
            return {"user": user.to_dict() if user else {"id": uid}}
        except (ValueError, OSError) as e:
            return _err(str(e))

    def authz_assign_role(user_id: str, role_name: str) -> Dict[str, Any]:
        try:
            store = _store_for(env_or_root_dir)
            user = store.assign_role(user_id, role_name)
            return {"ok": True, "user": user.to_dict()}
        except (ValueError, OSError) as e:
            return _err(str(e))

    def authz_revoke_role(user_id: str, role_name: str) -> Dict[str, Any]:
        try:
            store = _store_for(env_or_root_dir)
            user = store.revoke_role(user_id, role_name)
            return {"ok": True, "user": user.to_dict()}
        except (ValueError, OSError) as e:
            return _err(str(e))

    def authz_check(user_id: str, permission: str) -> Dict[str, Any]:
        try:
            store = _store_for(env_or_root_dir)
            user = store.get_user(user_id)
            if user is None:
                return _err(f"unknown user: {user_id!r}")
            return {"allowed": can(user, permission, store),
                    "user_id": user_id, "permission": permission}
        except (ValueError, OSError) as e:
            return _err(str(e))

    def authz_list_users() -> Dict[str, Any]:
        try:
            store = _store_for(env_or_root_dir)
            return {"users": [u.to_dict() for u in store.list_users()]}
        except (ValueError, OSError) as e:
            return _err(str(e))

    def authz_create_role(name: str, permissions: List[str],
                          description: str = "") -> Dict[str, Any]:
        try:
            store = _store_for(env_or_root_dir)
            created = store.create_role(Role(name=name,
                                             permissions=list(permissions or []),
                                             description=description or ""))
            return {"ok": True, "role": created}
        except (ValueError, OSError) as e:
            return _err(str(e))

    def authz_list_roles() -> Dict[str, Any]:
        try:
            store = _store_for(env_or_root_dir)
            return {"roles": [r.to_dict() for r in store.list_roles()]}
        except (ValueError, OSError) as e:
            return _err(str(e))

    return {
        "authz_add_user": {
            "name": "authz_add_user",
            "func": authz_add_user,
            "description": "Them user moi (roles tuy chon, phai ton tai).",
            "schema": _schema(
                {"user_id": {"type": "string", "description": "De trong de tu sinh"},
                 "name": {"type": "string"},
                 "roles": {"type": "array", "items": {"type": "string"}}},
                []),
        },
        "authz_assign_role": {
            "name": "authz_assign_role",
            "func": authz_assign_role,
            "description": "Gan role cho user.",
            "schema": _schema(
                {"user_id": {"type": "string"}, "role_name": {"type": "string"}},
                ["user_id", "role_name"]),
        },
        "authz_revoke_role": {
            "name": "authz_revoke_role",
            "func": authz_revoke_role,
            "description": "Go role khoi user.",
            "schema": _schema(
                {"user_id": {"type": "string"}, "role_name": {"type": "string"}},
                ["user_id", "role_name"]),
        },
        "authz_check": {
            "name": "authz_check",
            "func": authz_check,
            "description": "Kiem tra user co quyen khong. Tra allowed=true/false.",
            "schema": _schema(
                {"user_id": {"type": "string"}, "permission": {"type": "string"}},
                ["user_id", "permission"]),
        },
        "authz_list_users": {
            "name": "authz_list_users",
            "func": authz_list_users,
            "description": "Liet ke tat ca users kem roles.",
            "schema": _schema(),
        },
        "authz_create_role": {
            "name": "authz_create_role",
            "func": authz_create_role,
            "description": "Tao role custom (khong trung ten builtin).",
            "schema": _schema(
                {"name": {"type": "string"},
                 "permissions": {"type": "array", "items": {"type": "string"}},
                 "description": {"type": "string"}},
                ["name", "permissions"]),
        },
        "authz_list_roles": {
            "name": "authz_list_roles",
            "func": authz_list_roles,
            "description": "Liet ke tat ca roles (builtin + custom).",
            "schema": _schema(),
        },
    }
