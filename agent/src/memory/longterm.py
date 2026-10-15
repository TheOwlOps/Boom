"""Long-term memory cho Boom agent.

Bộ nhớ dài hạn: lưu mọi thứ agent học được theo thời gian, tìm kiếm toàn văn,
chấm điểm kết hợp độ liên quan + mức độ quan trọng, tóm tắt memories cũ
và tự quên dần thông tin ít quan trọng.

Khác với MemoryStore (store.py) chỉ lưu key/value đơn giản:
- Tìm kiếm toàn văn bằng FTS5 (fallback LIKE nếu sqlite không hỗ trợ)
- Mỗi memory có importance (0.0-1.0) và tags
- Scoring: relevance * (0.5 + 0.5 * importance)
- summarize(): gom memories cũ >30 ngày theo tags, tóm tắt 1 lần
- forget_old(): xóa memories vừa cũ vừa ít quan trọng

Stdlib only: sqlite3, threading, json, time.
"""

import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

# Memories cũ hơn số ngày này thì đủ điều kiện đưa vào summarize()
SUMMARIZE_AFTER_DAYS = 30


class LongTermMemory:
    """Bộ nhớ dài hạn dùng SQLite, thread-safe."""

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Thread-safe: mọi thao tác DB đều đi qua lock này.
        # check_same_thread=False vì connection được dùng chung giữa các thread,
        # lock đảm bảo không bao giờ có 2 thread dùng connection cùng lúc.
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._fts5 = False  # True nếu sqlite hỗ trợ FTS5
        with self._lock:
            self._init_db()

    # ------------------------------------------------------------------ init

    def _init_db(self):
        """Tạo schema. Thử bật FTS5, thất bại thì fallback LIKE."""
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL,
                    importance REAL NOT NULL DEFAULT 1.0,
                    tags TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    summarized INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tag TEXT NOT NULL,
                    content TEXT NOT NULL,
                    memory_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_created "
                "ON memories(created_at)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_importance "
                "ON memories(importance)"
            )
        # Thử tạo bảng FTS5 + trigger đồng bộ. Trigger giữ memories_fts
        # luôn khớp với bảng memories (insert/update/delete).
        try:
            with self.conn:
                self.conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
                    "USING fts5(key, value, tags)"
                )
                self.conn.execute("DROP TRIGGER IF EXISTS memories_ai")
                self.conn.execute("DROP TRIGGER IF EXISTS memories_au")
                self.conn.execute("DROP TRIGGER IF EXISTS memories_ad")
                self.conn.execute(
                    """
                    CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
                        INSERT INTO memories_fts(rowid, key, value, tags)
                        VALUES (new.id, new.key, new.value, new.tags);
                    END
                    """
                )
                self.conn.execute(
                    """
                    CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
                        DELETE FROM memories_fts WHERE rowid = old.id;
                        INSERT INTO memories_fts(rowid, key, value, tags)
                        VALUES (new.id, new.key, new.value, new.tags);
                    END
                    """
                )
                self.conn.execute(
                    """
                    CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
                        DELETE FROM memories_fts WHERE rowid = old.id;
                    END
                    """
                )
                # Nếu bảng memories đã có dữ liệu mà FTS index còn trống
                # (VD: DB tạo từ trước khi có FTS), rebuild cho khớp.
                n_mem = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
                n_fts = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM memories_fts").fetchone()["n"]
                if n_mem and not n_fts:
                    self.conn.execute(
                        "INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            self._fts5 = True
        except sqlite3.OperationalError:
            # sqlite build này không có FTS5 -> recall() dùng LIKE
            self._fts5 = False

    # ------------------------------------------------------------------ write

    def remember(self, key: str, value: str, importance: float = 1.0,
                 tags: Optional[List[str]] = None,
                 created_at: Optional[float] = None) -> None:
        """Lưu một memory.

        - importance: 0.0 (rác) -> 1.0 (rất quan trọng), tự clamp vào khoảng.
        - tags: VD ["ops", "deploy"]. None -> [].
        - created_at: epoch giây; None = bây giờ. Tham số này để backdate
          khi import memories cũ hoặc để test.
        - Ghi đè key đã tồn tại: cập nhật value/importance/tags/updated_at,
          giữ nguyên created_at gốc, reset cờ summarized để tóm tắt lại.
        """
        importance = max(0.0, min(1.0, float(importance)))
        tags_json = json.dumps(list(tags or []), ensure_ascii=False)
        now = time.time()
        ts = now if created_at is None else float(created_at)
        with self._lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO memories
                    (key, value, importance, tags, created_at, updated_at, summarized)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    importance=excluded.importance,
                    tags=excluded.tags,
                    updated_at=excluded.updated_at,
                    summarized=0
                """,
                (key, str(value), importance, tags_json, ts, now),
            )

    # ------------------------------------------------------------------ read

    def _fts5_query(self, query: str) -> str:
        """Biến query người dùng thành FTS5 MATCH an toàn.

        Mỗi token được quote kép nên ký tự đặc biệt (-, *, ", ...) thành
        literal, không phá cú pháp MATCH. Các token nối bằng OR.
        """
        tokens = [t for t in query.split() if t]
        return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)

    def _recall_fts(self, query: str) -> List[sqlite3.Row]:
        """Tìm bằng FTS5, trả về kèm bm25 thô (âm, càng âm càng khớp)."""
        match = self._fts5_query(query)
        return self.conn.execute(
            """
            SELECT m.*, bm25(memories_fts) AS bm25raw
            FROM memories_fts
            JOIN memories m ON m.id = memories_fts.rowid
            WHERE memories_fts MATCH ?
            """,
            (match,),
        ).fetchall()

    @staticmethod
    def _like_escape(s: str) -> str:
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _recall_like(self, query: str) -> List[sqlite3.Row]:
        """Fallback khi không có FTS5: LIKE trên key/value/tags."""
        pat = f"%{self._like_escape(query)}%"
        return self.conn.execute(
            """
            SELECT *, 0.0 AS bm25raw
            FROM memories
            WHERE key LIKE ? ESCAPE '\\'
               OR value LIKE ? ESCAPE '\\'
               OR tags LIKE ? ESCAPE '\\'
            """,
            (pat, pat, pat),
        ).fetchall()

    def recall(self, query: str, limit: int = 5) -> List[Dict]:
        """Tìm memories liên quan nhất tới query.

        Scoring: relevance * (0.5 + 0.5 * importance)
        - relevance từ FTS5 bm25 qua sigmoid: 1/(1+exp(bm25)) trong (0,1),
          khớp càng tốt càng gần 1. Fallback LIKE: relevance = 1.0.
        - importance càng cao càng đẩy score lên, nhưng memory không liên
          quan vẫn không bao giờ lọt vào (không match = không có mặt).

        Trả về list dict sắp xếp theo score giảm dần, tối đa `limit` phần tử.
        """
        query = (query or "").strip()
        if not query:
            return []
        with self._lock:
            rows = (self._recall_fts(query) if self._fts5
                    else self._recall_like(query))
            results = []
            for r in rows:
                if self._fts5:
                    # bm25 FTS5: số âm, càng âm càng khớp -> sigmoid đảo chiều
                    relevance = 1.0 / (1.0 + math.exp(r["bm25raw"]))
                else:
                    relevance = 1.0
                importance = float(r["importance"])
                score = relevance * (0.5 + 0.5 * importance)
                try:
                    tags = json.loads(r["tags"])
                except Exception:
                    tags = []
                results.append({
                    "id": r["id"],
                    "key": r["key"],
                    "value": r["value"],
                    "importance": importance,
                    "tags": tags,
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "summarized": bool(r["summarized"]),
                    "score": score,
                })
        results.sort(key=lambda d: d["score"], reverse=True)
        return results[: max(0, limit)]

    # -------------------------------------------------------------- summarize

    def _template_summary(self, tag: str, items: List[sqlite3.Row]) -> str:
        """Tóm tắt không cần LLM: liệt kê gọn từng memory trong nhóm."""
        lines = [f"Tóm tắt nhóm '{tag}' ({len(items)} memories):"]
        for r in items:
            val = r["value"]
            if len(val) > 200:
                val = val[:200] + "..."
            day = time.strftime("%Y-%m-%d", time.localtime(r["created_at"]))
            lines.append(f"- [{r['key']}] {val} ({day}, importance={r['importance']:.1f})")
        return "\n".join(lines)

    def summarize(self, llm_fn: Optional[Callable[[str], str]] = None) -> str:
        """Tóm tắt memories cũ hơn SUMMARIZE_AFTER_DAYS ngày, nhóm theo tags.

        - Có llm_fn: gọi llm_fn(prompt) cho từng nhóm tag để tóm tắt.
        - Không có: dùng template liệt kê đơn giản.
        - Mỗi nhóm tag lưu 1 dòng vào bảng summaries; memories đã tóm tắt
          được đánh dấu summarized=1 nên không bao giờ tóm tắt lại.
        - Trả về chuỗi tóm tắt gộp (rỗng nếu không có gì để tóm tắt).
        """
        cutoff = time.time() - SUMMARIZE_AFTER_DAYS * 86400
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, key, value, importance, tags, created_at
                FROM memories
                WHERE created_at < ? AND summarized = 0
                ORDER BY created_at
                """,
                (cutoff,),
            ).fetchall()
        if not rows:
            return ""
        # Nhóm theo tag; memory nhiều tag thì xuất hiện ở nhiều nhóm,
        # memory không tag nào thì vào nhóm "untagged".
        groups: Dict[str, List[sqlite3.Row]] = {}
        for r in rows:
            try:
                tags = json.loads(r["tags"]) or ["untagged"]
            except Exception:
                tags = ["untagged"]
            for t in tags:
                groups.setdefault(str(t), []).append(r)

        parts: List[str] = []
        now = time.time()
        with self._lock, self.conn:
            for tag, items in groups.items():
                if llm_fn is not None:
                    prompt_lines = [
                        f"Tóm tắt các ghi nhớ cũ sau (nhóm tag: {tag}):"]
                    for r in items:
                        day = time.strftime("%Y-%m-%d",
                                            time.localtime(r["created_at"]))
                        prompt_lines.append(
                            f"- [{r['key']}] {r['value']} "
                            f"({day}, importance={r['importance']:.1f})")
                    prompt_lines.append(
                        "Viết tóm tắt ngắn gọn bằng tiếng Việt, giữ lại "
                        "thông tin quan trọng nhất.")
                    content = llm_fn("\n".join(prompt_lines))
                else:
                    content = self._template_summary(tag, items)
                self.conn.execute(
                    """
                    INSERT INTO summaries (tag, content, memory_count, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (tag, content, len(items), now),
                )
                parts.append(f"## {tag}\n{content}")
            # Đánh dấu đã tóm tắt để lần sau không tóm tắt lại
            ids = [r["id"] for r in rows]
            for i in range(0, len(ids), 500):  # chunk tránh giới hạn biến SQL
                chunk = ids[i:i + 500]
                self.conn.execute(
                    f"UPDATE memories SET summarized = 1 "
                    f"WHERE id IN ({','.join('?' * len(chunk))})",
                    chunk,
                )
        return "\n\n".join(parts)

    # ---------------------------------------------------------------- forget

    def forget_old(self, days: int = 90, min_importance: float = 0.3) -> int:
        """Xóa memories VỪA cũ hơn `days` ngày VỪA có importance < min_importance.

        Memory cũ nhưng quan trọng (>= min_importance) được giữ lại.
        Memory mới nhưng ít quan trọng cũng được giữ (chưa đủ cũ).
        Trigger FTS tự dọn index theo. Trả về số lượng đã xóa.
        """
        cutoff = time.time() - days * 86400
        with self._lock, self.conn:
            cur = self.conn.execute(
                "DELETE FROM memories WHERE created_at < ? AND importance < ?",
                (cutoff, float(min_importance)),
            )
            return cur.rowcount

    # ----------------------------------------------------------------- stats

    def get_stats(self) -> Dict:
        """Thống kê: tổng số, đếm theo tag, oldest/newest, số đã tóm tắt."""
        with self._lock:
            total = self.conn.execute(
                "SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
            summarized = self.conn.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE summarized = 1"
            ).fetchone()["n"]
            summaries = self.conn.execute(
                "SELECT COUNT(*) AS n FROM summaries").fetchone()["n"]
            oldest = self.conn.execute(
                "SELECT MIN(created_at) AS t FROM memories").fetchone()["t"]
            newest = self.conn.execute(
                "SELECT MAX(created_at) AS t FROM memories").fetchone()["t"]
            by_tag: Dict[str, int] = {}
            for r in self.conn.execute("SELECT tags FROM memories"):
                try:
                    tags = json.loads(r["tags"])
                except Exception:
                    tags = []
                for t in tags or ["untagged"]:
                    t = str(t)
                    by_tag[t] = by_tag.get(t, 0) + 1
        return {
            "total": total,
            "by_tag": by_tag,
            "oldest": oldest,
            "newest": newest,
            "summarized": summarized,
            "summaries": summaries,
            "fts5": self._fts5,
        }

    def close(self):
        """Đóng connection (giống pattern của MemoryStore)."""
        with self._lock:
            self.conn.close()
