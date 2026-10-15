"""SQLite-backed learning store. Thread-safe: lock + commit sau mỗi write.

Pattern tái dùng từ tasks/store.py: _gen_id() dạng learn_<ms>_<4hex>
bằng secrets, retry khi đụng id trong cùng millisecond.
"""

import sqlite3
import threading
import time
import secrets
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

from learning.models import Learning, LearningType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS learnings (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence REAL NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    superseded_by TEXT NULL
)
"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _gen_id() -> str:
    """Id duy nhất: millisecond + 4 hex ngẫu nhiên (tránh đụng độ cùng-ms)."""
    return f"learn_{int(time.time() * 1000)}_{secrets.token_hex(2)}"


class LearningStore:
    """LearningStore lưu SQLite, thread-safe qua threading.Lock."""

    def __init__(self, db_path: Union[str, Path]):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    # -- helpers ---------------------------------------------------------

    def _row_to_learning(self, row: sqlite3.Row) -> Learning:
        return Learning.from_dict(
            {
                "id": row["id"],
                "type": row["type"],
                "content": row["content"],
                "confidence": row["confidence"],
                "source": row["source"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "superseded_by": row["superseded_by"],
            }
        )

    @staticmethod
    def _resolve_type(ltype: Union[LearningType, str, None]) -> Optional[str]:
        if ltype is None:
            return None
        if isinstance(ltype, LearningType):
            return ltype.value
        if isinstance(ltype, str):
            return LearningType(ltype).value  # ValueError nếu type lạ
        raise ValueError(f"unknown learning type: {ltype!r}")

    # -- API -------------------------------------------------------------

    def add(self, learning: Learning) -> str:
        """INSERT learning mới. Sinh id nếu rỗng; id tường minh đã tồn tại -> ValueError."""
        with self._lock:
            auto = not learning.id
            if auto:
                learning.id = _gen_id()
            for _ in range(10):
                cur = self._conn.execute("SELECT 1 FROM learnings WHERE id = ?", (learning.id,))
                if cur.fetchone() is None:
                    break
                if not auto:
                    raise ValueError(f"learning id already exists: {learning.id!r}")
                learning.id = _gen_id()  # đụng độ hiếm: sinh lại
            else:
                raise ValueError("could not generate a unique learning id")
            now = _now_iso()
            if not learning.created_at:
                learning.created_at = now
            if not learning.updated_at:
                learning.updated_at = now
            self._conn.execute(
                "INSERT INTO learnings (id, type, content, confidence, source,"
                " created_at, updated_at, superseded_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    learning.id,
                    learning.type.value,
                    learning.content,
                    learning.confidence,
                    learning.source,
                    learning.created_at,
                    learning.updated_at,
                    learning.superseded_by,
                ),
            )
            self._conn.commit()
            return learning.id

    def get(self, learning_id: str) -> Optional[Learning]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM learnings WHERE id = ?", (learning_id,))
            row = cur.fetchone()
            return self._row_to_learning(row) if row is not None else None

    def list(
        self,
        type: Union[LearningType, str, None] = None,
        include_superseded: bool = False,
    ) -> List[Learning]:
        """Liệt kê learning. Mặc định ẩn các mẩu đã bị supersede."""
        type_val = self._resolve_type(type)
        clauses: List[str] = []
        params: List[str] = []
        if type_val is not None:
            clauses.append("type = ?")
            params.append(type_val)
        if not include_superseded:
            clauses.append("superseded_by IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            cur = self._conn.execute(
                f"SELECT * FROM learnings {where} ORDER BY created_at", params
            )
            rows = cur.fetchall()
        return [self._row_to_learning(r) for r in rows]

    def search(self, query: str) -> List[Learning]:
        """Tìm LIKE %query% trên content, chỉ trả non-superseded, confidence desc."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM learnings"
                " WHERE content LIKE ? AND superseded_by IS NULL"
                " ORDER BY confidence DESC",
                (f"%{query}%",),
            )
            rows = cur.fetchall()
        return [self._row_to_learning(r) for r in rows]

    def supersede(self, old_id: str, new_id: str) -> None:
        """Đánh dấu old_id bị new_id thay thế. old không tồn tại -> ValueError."""
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM learnings WHERE id = ?", (old_id,))
            if cur.fetchone() is None:
                raise ValueError(f"learning id not found: {old_id!r}")
            self._conn.execute(
                "UPDATE learnings SET superseded_by = ?, updated_at = ? WHERE id = ?",
                (new_id, _now_iso(), old_id),
            )
            self._conn.commit()

    def remove(self, learning_id: str) -> bool:
        """Xóa learning. Không tồn tại -> False."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM learnings WHERE id = ?", (learning_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def count(self) -> int:
        """Tổng số learning trong store (kể cả superseded)."""
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM learnings")
            return int(cur.fetchone()[0])
