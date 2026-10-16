import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

class MemoryStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    key TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def save(self, category: str, key: str, value: Any):
        now = time.time()
        val_str = json.dumps(value) if not isinstance(value, str) else value
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO memories (category, key, value, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    category=excluded.category,
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (category, key, val_str, now, now)
            )

    def get(self, key: str) -> Optional[str]:
        cur = self.conn.execute("SELECT value FROM memories WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def search(self, pattern: str) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT category, key, value FROM memories WHERE key LIKE ? OR value LIKE ?",
            (f"%{pattern}%", f"%{pattern}%")
        )
        return [dict(row) for row in cur.fetchall()]

    def save_lesson(self, topic: str, problem: str, solution: str) -> int:
        now = time.time()
        val = json.dumps({"topic": topic, "problem": problem, "solution": solution})
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO memories (category, key, value, created_at, updated_at)
                VALUES ('lesson', ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (topic, val, now, now)
            )
            return cur.lastrowid or 1

    def search_lessons(self, query: str) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT key, value FROM memories WHERE category = 'lesson' AND (key LIKE ? OR value LIKE ?)",
            (f"%{query}%", f"%{query}%")
        )
        lessons = []
        for row in cur.fetchall():
            try:
                data = json.loads(row["value"])
                lessons.append(data)
            except Exception:
                pass
        return lessons

    def close(self):
        self.conn.close()
