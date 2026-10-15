"""SQLite store cho RBAC: users + roles. Thread-safe, theo pattern tasks/store.py."""

import json
import sqlite3
import threading
import time
import secrets
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

from authz.models import Role, User, default_roles, BUILTIN_ROLES

_SCHEMA = """
CREATE TABLE IF NOT EXISTS authz_users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    roles_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS authz_roles (
    name TEXT PRIMARY KEY,
    permissions_json TEXT NOT NULL,
    description TEXT NOT NULL,
    is_builtin INTEGER NOT NULL DEFAULT 0
);
"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _gen_user_id() -> str:
    return f"usr_{int(time.time() * 1000)}_{secrets.token_hex(2)}"


class AuthzStore:
    """Luu users/roles vao SQLite, thread-safe qua threading.Lock."""

    def __init__(self, db_path: Union[str, Path]):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._seed_builtin_roles()
            self._conn.commit()

    # -- roles --

    def _seed_builtin_roles(self) -> None:
        for role in default_roles():
            cur = self._conn.execute(
                "SELECT 1 FROM authz_roles WHERE name = ?", (role.name,))
            if cur.fetchone() is None:
                self._conn.execute(
                    "INSERT INTO authz_roles (name, permissions_json, description, is_builtin)"
                    " VALUES (?, ?, ?, 1)",
                    (role.name, json.dumps(role.permissions), role.description),
                )

    def _row_to_role(self, row: sqlite3.Row) -> Role:
        return Role(
            name=row["name"],
            permissions=json.loads(row["permissions_json"]),
            description=row["description"] or "",
        )

    def get_role(self, name: str) -> Optional[Role]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM authz_roles WHERE name = ?", (name,))
            row = cur.fetchone()
            return self._row_to_role(row) if row else None

    def list_roles(self) -> List[Role]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM authz_roles ORDER BY name")
            return [self._row_to_role(r) for r in cur.fetchall()]

    def create_role(self, role: Role) -> str:
        """Tao role custom. Ten da ton tai (ke ca builtin) -> ValueError."""
        name = (role.name or "").strip()
        if not name:
            raise ValueError("role name is required")
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM authz_roles WHERE name = ?", (name,))
            if cur.fetchone() is not None:
                raise ValueError(f"role already exists: {name!r}")
            self._conn.execute(
                "INSERT INTO authz_roles (name, permissions_json, description, is_builtin)"
                " VALUES (?, ?, ?, 0)",
                (name, json.dumps(list(role.permissions)), role.description),
            )
            self._conn.commit()
        return name

    def delete_role(self, name: str) -> bool:
        """Xoa role custom. Role builtin -> ValueError. Role dang duoc user dung -> ValueError."""
        if name in BUILTIN_ROLES:
            raise ValueError(f"cannot delete builtin role: {name!r}")
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM authz_roles WHERE name = ?", (name,))
            if cur.fetchone() is None:
                return False
            # role dang duoc gan cho user nao?
            for row in self._conn.execute("SELECT id, roles_json FROM authz_users"):
                roles = json.loads(row["roles_json"])
                if name in roles:
                    raise ValueError(
                        f"role {name!r} is still assigned to user {row['id']!r}")
            self._conn.execute("DELETE FROM authz_roles WHERE name = ?", (name,))
            self._conn.commit()
            return True

    # -- users --

    def _row_to_user(self, row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            name=row["name"] or "",
            roles=json.loads(row["roles_json"]),
            created_at=row["created_at"] or "",
        )

    def add_user(self, user: User) -> str:
        """Them user. Sinh id neu rong; id da ton tai -> ValueError."""
        with self._lock:
            if not user.id:
                user.id = _gen_user_id()
                for _ in range(10):
                    cur = self._conn.execute(
                        "SELECT 1 FROM authz_users WHERE id = ?", (user.id,))
                    if cur.fetchone() is None:
                        break
                    user.id = _gen_user_id()
                else:
                    raise ValueError("could not generate a unique user id")
            else:
                cur = self._conn.execute(
                    "SELECT 1 FROM authz_users WHERE id = ?", (user.id,))
                if cur.fetchone() is not None:
                    raise ValueError(f"user id already exists: {user.id!r}")
            # validate roles ton tai
            for r in user.roles:
                cur = self._conn.execute(
                    "SELECT 1 FROM authz_roles WHERE name = ?", (r,))
                if cur.fetchone() is None:
                    raise ValueError(f"unknown role: {r!r}")
            if not user.created_at:
                user.created_at = _now_iso()
            # dedup roles, giu thu tu
            seen = set()
            user.roles = [r for r in user.roles
                          if not (r in seen or seen.add(r))]
            self._conn.execute(
                "INSERT INTO authz_users (id, name, roles_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (user.id, user.name, json.dumps(user.roles), user.created_at),
            )
            self._conn.commit()
            return user.id

    def get_user(self, user_id: str) -> Optional[User]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM authz_users WHERE id = ?", (user_id,))
            row = cur.fetchone()
            return self._row_to_user(row) if row else None

    def list_users(self) -> List[User]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM authz_users ORDER BY created_at")
            return [self._row_to_user(r) for r in cur.fetchall()]

    def delete_user(self, user_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM authz_users WHERE id = ?", (user_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def assign_role(self, user_id: str, role_name: str) -> User:
        """Gan role cho user. User/role khong ton tai -> ValueError."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM authz_users WHERE id = ?", (user_id,))
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"unknown user: {user_id!r}")
            cur = self._conn.execute(
                "SELECT 1 FROM authz_roles WHERE name = ?", (role_name,))
            if cur.fetchone() is None:
                raise ValueError(f"unknown role: {role_name!r}")
            roles = json.loads(row["roles_json"])
            if role_name not in roles:
                roles.append(role_name)
                self._conn.execute(
                    "UPDATE authz_users SET roles_json = ? WHERE id = ?",
                    (json.dumps(roles), user_id),
                )
                self._conn.commit()
            user = self._row_to_user(row)
            user.roles = roles
            return user

    def revoke_role(self, user_id: str, role_name: str) -> User:
        """Go role khoi user. Khong co role do -> tra user nguyen (khong raise)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM authz_users WHERE id = ?", (user_id,))
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"unknown user: {user_id!r}")
            roles = [r for r in json.loads(row["roles_json"]) if r != role_name]
            self._conn.execute(
                "UPDATE authz_users SET roles_json = ? WHERE id = ?",
                (json.dumps(roles), user_id),
            )
            self._conn.commit()
            user = self._row_to_user(row)
            user.roles = roles
            return user

    def close(self) -> None:
        with self._lock:
            self._conn.close()
