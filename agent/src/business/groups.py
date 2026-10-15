"""Group Manager cho Boom (doanh nghiệp): quản lý nhiều group trên nhiều kênh.

Đăng ký group (telegram/discord/zalo...), thống kê tin nhắn/member theo
từng group, và gắn policy cho phép/cấm command ở cấp group.

Pattern theo repo: SQLite + threading.Lock, mỗi thao tác mở connection
mới (`with self._lock, self._connect()`), field dict lưu dạng JSON,
lỗi trả về dạng dict {"error": ...} thay vì raise (trừ getters trả None).

Chỉ dùng stdlib.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now() -> str:
    """Timestamp ISO hiện tại (giờ local), cùng format với broadcast.py."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _normalize_command(command: Any) -> str:
    """Chuẩn hoá tên command để so sánh với policy.

    Bỏ khoảng trắng, bỏ dấu "/" đầu (kiểu Telegram "/help"), lowercase.
    """
    return str(command or "").strip().lstrip("/").lower()


def _normalize_command_list(values: Any) -> List[str]:
    """Chuẩn hoá danh sách command trong policy thành list[str]."""
    if not isinstance(values, list):
        return []
    return [_normalize_command(v) for v in values if _normalize_command(v)]


def _decode_json(value: Any, default: Any) -> Any:
    """Giải mã JSON từ DB; dữ liệu hỏng → trả default, không crash."""
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class GroupManager:
    """Quản lý groups theo từng channel, backend SQLite. Thread-safe."""

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

    # ------------------------------------------------------------ nội bộ

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
        """Kiểm tra channel/group_id hợp lệ; trả về message lỗi hoặc None."""
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

    # ------------------------------------------------------------ đăng ký

    def register_group(self, channel: str, group_id: str, name: str,
                       settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Đăng ký group mới. Trùng (channel, group_id) → báo lỗi rõ, không đè."""
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
        """Gỡ đăng ký group. Group không tồn tại → báo lỗi."""
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
        """Liệt kê groups (kèm stats message_count/member_count).
        channel=None → tất cả các kênh."""
        with self._lock, self._connect() as conn:
            if channel is None:
                rows = conn.execute(
                    "SELECT * FROM groups ORDER BY channel, group_id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM groups WHERE channel = ? ORDER BY group_id",
                    (channel,)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ------------------------------------------------------------ hoạt động

    def record_message(self, channel: str, group_id: str) -> Dict[str, Any]:
        """Ghi nhận 1 tin nhắn: tăng message_count, cập nhật last_active.

        Group chưa đăng ký → tự tạo với name mặc định (đường chạy message
        pipeline không được chặn vì group mới xuất hiện).
        """
        err = self._validate_ident(channel, group_id)
        if err:
            return {"error": err}
        channel, group_id = channel.strip(), group_id.strip()
        with self._lock, self._connect() as conn:
            row = self._get_row(conn, channel, group_id)
            if row is None:
                # Tự đăng ký group mới: name mặc định "channel:group_id".
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
        """Stats 1 group. Không tồn tại → None."""
        err = self._validate_ident(channel, group_id)
        if err:
            return None
        with self._lock, self._connect() as conn:
            row = self._get_row(conn, channel.strip(), group_id.strip())
        return self._row_to_dict(row) if row else None

    def set_member_count(self, channel: str, group_id: str, n: int) -> Dict[str, Any]:
        """Cập nhật số member của group. n phải là số nguyên >= 0."""
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
        """Gắn policy cho group. policy là dict, ví dụ:
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
        """Trả về policy của group; mặc định {} (không có policy → cho phép hết)."""
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
        """Kiểm tra command có được phép trong group theo policy không.

        Logic (không bao giờ raise):
        - Không có policy → cho phép hết.
        - "denied_commands" chứa command → cấm (ưu tiên cao nhất).
        - "allowed_commands" có mặt (list) → chỉ cho phép command trong list.
        - "require_approval" không cấm command, chỉ đánh dấu cần duyệt
          (approval xử lý ở tầng khác, ví dụ ApprovalGate của Boom).
        """
        try:
            policy = self.get_group_policy(channel, group_id)
        except Exception:
            return True  # policy đọc lỗi → fail-open để không chặn pipeline
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
