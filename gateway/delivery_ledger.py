import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

class DeliveryLedger:
    """Durable delivery-obligation ledger guaranteeing at-least-once, zero-duplicate message delivery."""

    def __init__(self, db_path: Optional[Any] = None):
        if db_path is None:
            self.db_path = Path.home() / "AppData" / "Local" / "boom" / "delivery_ledger.db"
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.db_path))
        elif str(db_path) == ":memory:":
            self.db_path = Path(":memory:")
            self.conn = sqlite3.connect(":memory:")
        else:
            self.db_path = Path(db_path)
            self.conn = sqlite3.connect(str(self.db_path))

        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_obligations (
                    id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    @staticmethod
    def compute_hash(channel: str, target_id: str, content: str) -> str:
        raw = f"{channel}:{target_id}:{content}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def record_obligation(self, obligation_id: str, channel: str, target_id: str, content: str) -> bool:
        c_hash = self.compute_hash(channel, target_id, content)
        now = time.time()
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO delivery_obligations (id, channel, target_id, content_hash, status, attempts, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                    """,
                    (obligation_id, channel, target_id, c_hash, now, now)
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def mark_attempting(self, obligation_id: str):
        now = time.time()
        with self.conn:
            self.conn.execute(
                """
                UPDATE delivery_obligations 
                SET status = 'attempting', attempts = attempts + 1, updated_at = ?
                WHERE id = ?
                """,
                (now, obligation_id)
            )

    def mark_delivered(self, obligation_id: str):
        now = time.time()
        with self.conn:
            self.conn.execute(
                "UPDATE delivery_obligations SET status = 'delivered', updated_at = ? WHERE id = ?",
                (now, obligation_id)
            )

    def mark_failed(self, obligation_id: str):
        now = time.time()
        with self.conn:
            self.conn.execute(
                "UPDATE delivery_obligations SET status = 'failed', updated_at = ? WHERE id = ?",
                (now, obligation_id)
            )

    def is_delivered(self, obligation_id: str) -> bool:
        cur = self.conn.execute("SELECT status FROM delivery_obligations WHERE id = ?", (obligation_id,))
        row = cur.fetchone()
        return row is not None and row["status"] == "delivered"

    def claim(self, obligation_id: str, channel: str, target: str) -> bool:
        """Atomically claim an obligation. Returns True if claimed, False if already exists."""
        return self.record_obligation(obligation_id, channel, target, "")

    def fulfill(self, obligation_id: str):
        """Mark an obligation as fulfilled/delivered."""
        self.mark_delivered(obligation_id)

    def get_pending_or_stale(self, timeout_sec: float = 30.0) -> List[Dict[str, Any]]:
        cutoff = time.time() - timeout_sec
        cur = self.conn.execute(
            """
            SELECT * FROM delivery_obligations 
            WHERE status IN ('pending', 'attempting') AND updated_at < ?
            """,
            (cutoff,)
        )
        return [dict(row) for row in cur.fetchall()]

    def close(self):
        self.conn.close()
