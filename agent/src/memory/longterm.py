"""Long-term memory for the Boom agent.

Long-term memory: stores everything the agent learns over time, full-text
search, scoring that combines relevance + importance, summarizing old
memories, and gradually forgetting low-importance information.

Unlike MemoryStore (store.py) which only stores simple key/values:
- Full-text search via FTS5 (LIKE fallback when sqlite lacks it)
- Each memory has importance (0.0-1.0) and tags
- Scoring: relevance * (0.5 + 0.5 * importance)
- summarize(): groups memories older than 30 days by tags, summarizes once
- forget_old(): deletes memories that are both old and low-importance

Stdlib only: sqlite3, threading, json, time.
"""

import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

# Memories older than this many days become eligible for summarize()
SUMMARIZE_AFTER_DAYS = 30


class LongTermMemory:
    """SQLite-backed long-term memory, thread-safe."""

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Thread-safe: every DB operation goes through this lock.
        # check_same_thread=False because the connection is shared across
        # threads; the lock guarantees 2 threads never use it at once.
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._fts5 = False  # True when sqlite supports FTS5
        with self._lock:
            self._init_db()

    # ------------------------------------------------------------------ init

    def _init_db(self):
        """Creates the schema. Tries FTS5, falls back to LIKE on failure."""
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
        # Try creating the FTS5 table + sync triggers. The triggers keep
        # memories_fts always matching the memories table (insert/update/delete).
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
                # When the memories table already has data but the FTS index
                # is empty (e.g. DB created before FTS existed), rebuild to match.
                n_mem = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
                n_fts = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM memories_fts").fetchone()["n"]
                if n_mem and not n_fts:
                    self.conn.execute(
                        "INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            self._fts5 = True
        except sqlite3.OperationalError:
            # this sqlite build has no FTS5 -> recall() uses LIKE
            self._fts5 = False

    # ------------------------------------------------------------------ write

    def remember(self, key: str, value: str, importance: float = 1.0,
                 tags: Optional[List[str]] = None,
                 created_at: Optional[float] = None) -> None:
        """Stores one memory.

        - importance: 0.0 (junk) -> 1.0 (very important), auto-clamped.
        - tags: e.g. ["ops", "deploy"]. None -> [].
        - created_at: epoch seconds; None = now. Exists for backdating
          imported old memories or for tests.
        - Overwriting an existing key: updates value/importance/tags/updated_at,
          keeps the original created_at, resets the summarized flag so it gets
          summarized again.
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
        """Turns a user query into a safe FTS5 MATCH.

        Every token is double-quoted so special chars (-, *, ", ...) become
        literals instead of breaking MATCH syntax. Tokens joined with OR.
        """
        tokens = [t for t in query.split() if t]
        return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)

    def _recall_fts(self, query: str) -> List[sqlite3.Row]:
        """Searches with FTS5, returns rows with raw bm25 (negative, more = better)."""
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
        """Fallback without FTS5: LIKE over key/value/tags."""
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
        """Finds the memories most relevant to a query.

        Scoring: relevance * (0.5 + 0.5 * importance)
        - relevance from FTS5 bm25 through a sigmoid: 1/(1+exp(bm25)) in (0,1),
          better matches closer to 1. LIKE fallback: relevance = 1.0.
        - higher importance pushes the score up, but an irrelevant memory can
          never sneak in (no match = not present).

        Returns a list of dicts sorted by score descending, at most `limit` items.
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
                    # FTS5 bm25: negative numbers, more negative = better match
                    # -> inverted sigmoid
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
        """No-LLM summary: compactly lists each memory in the group.

        NOTE: kept in Vietnamese — the summary output language is a product
        decision (mirrors digest/briefing.py).
        """
        lines = [f"Tóm tắt nhóm '{tag}' ({len(items)} memories):"]
        for r in items:
            val = r["value"]
            if len(val) > 200:
                val = val[:200] + "..."
            day = time.strftime("%Y-%m-%d", time.localtime(r["created_at"]))
            lines.append(f"- [{r['key']}] {val} ({day}, importance={r['importance']:.1f})")
        return "\n".join(lines)

    def summarize(self, llm_fn: Optional[Callable[[str], str]] = None) -> str:
        """Summarizes memories older than SUMMARIZE_AFTER_DAYS days, grouped by tags.

        - With llm_fn: calls llm_fn(prompt) per tag group to summarize.
        - Without: uses the simple listing template.
        - Each tag group stores 1 row in the summaries table; summarized
          memories are flagged summarized=1 so they're never summarized twice.
        - Returns the combined summary string (empty when nothing to summarize).
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
        # Group by tag; multi-tag memories appear in several groups,
        # untagged memories go to the "untagged" group.
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
                    # NOTE: prompt kept in Vietnamese — the summary output
                    # language is a product decision.
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
            # Flag as summarized so next runs skip them
            ids = [r["id"] for r in rows]
            for i in range(0, len(ids), 500):  # chunked to dodge SQL variable limits
                chunk = ids[i:i + 500]
                self.conn.execute(
                    f"UPDATE memories SET summarized = 1 "
                    f"WHERE id IN ({','.join('?' * len(chunk))})",
                    chunk,
                )
        return "\n\n".join(parts)

    # ---------------------------------------------------------------- forget

    def forget_old(self, days: int = 90, min_importance: float = 0.3) -> int:
        """Deletes memories BOTH older than `days` days AND below min_importance.

        Old but important (>= min_importance) memories are kept.
        New but unimportant memories are kept too (not old enough yet).
        FTS triggers clean the index along. Returns the deleted count.
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
        """Stats: totals, per-tag counts, oldest/newest, summarized count."""
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
        """Closes the connection (same pattern as MemoryStore)."""
        with self._lock:
            self.conn.close()
