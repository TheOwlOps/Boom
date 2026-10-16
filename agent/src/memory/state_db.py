import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 2  # bumped whenever messages_fts needs a rebuild

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    message_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_calls TEXT,
    timestamp REAL NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    data TEXT NOT NULL,
    timestamp REAL NOT NULL
);

-- Hot-path indexes: get_session_messages() and read_timeline() filter by
-- session_id and order by id on every call. Without these SQLite falls back to
-- a full table scan whose cost grows with total history (measured 155ms at
-- 800k rows for the current session); with them it is a bounded index seek.
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_timeline_session ON timeline(session_id, id);

-- Full-Text Search (FTS5) for cross-session episodic search.
-- No SQL trigger mirrors `messages` into this table: the tokenizer must see CJK
-- split into single characters, which SQL cannot do, so StateDB.append_message
-- writes both rows in one transaction instead.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    session_id UNINDEXED,
    role UNINDEXED,
    tokenize = 'porter unicode61'
);
"""

# Codepoint ranges the unicode61 tokenizer swallows as a single token, so a Chinese
# query can never match a Chinese message until they are spaced apart per character.
_CJK_RANGES = (
    (0x3040, 0x30FF),  # Hiragana + Katakana
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xAC00, 0xD7AF),  # Hangul syllables
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0xFF10, 0xFF19),  # fullwidth digits
    (0xFF21, 0xFF3A),  # fullwidth Latin capitals
    (0xFF41, 0xFF5A),  # fullwidth Latin small letters
)


def segment_cjk(text: str) -> str:
    """Space out CJK codepoints so FTS5 indexes them one character at a time.

    Applied to both the indexed text and the query. ASCII passes through untouched,
    so English indexing and matching are bit-for-bit unchanged. CJK punctuation is
    left alone: unicode61 already treats it as a separator.
    """
    if not text:
        return text
    out: List[str] = []
    prev_cjk = False
    for ch in text:
        code = ord(ch)
        is_cjk = any(low <= code <= high for low, high in _CJK_RANGES)
        if is_cjk:
            if out and not prev_cjk:
                out.append(" ")
            out.append(ch)
            out.append(" ")
            prev_cjk = True
        else:
            out.append(ch)
            prev_cjk = False
    return "".join(out)


class StateDB:
    """Production-grade persistent SQLite WAL state database with FTS5 cross-session search."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self.conn:
            self.conn.executescript(SCHEMA_SQL)
        version = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version"
        ).fetchone()[0]
        if version < SCHEMA_VERSION:
            self._rebuild_fts()

    def _rebuild_fts(self):
        """Re-index messages_fts with segmented content and drop the old raw trigger.

        Every pre-CJK database holds unsegmented FTS rows, so without this one-time
        rebuild an upgrade would keep missing every Chinese query.
        """
        rows = self.conn.execute("SELECT id, session_id, role, content FROM messages").fetchall()
        with self.conn:
            self.conn.execute("DROP TRIGGER IF EXISTS trg_messages_ai")
            self.conn.execute("DELETE FROM messages_fts")
            self.conn.executemany(
                """
                INSERT INTO messages_fts (rowid, content, session_id, role)
                VALUES (?, ?, ?, ?)
                """,
                [(r["id"], segment_cjk(r["content"]), r["session_id"], r["role"]) for r in rows],
            )
            self.conn.execute("DELETE FROM schema_version")
            self.conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))

    def create_session(self, session_id: str, model: str = "default"):
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO sessions (id, model, started_at)
                VALUES (?, ?, ?)
                """,
                (session_id, model, time.time())
            )

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: Optional[List[Dict[str, Any]]] = None
    ) -> int:
        with self.conn:
            tc_json = json.dumps(tool_calls) if tool_calls else None
            cur = self.conn.execute(
                """
                INSERT INTO messages (session_id, role, content, tool_calls, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, role, content, tc_json, time.time())
            )
            # Indexed in the same transaction as the message; the CJK segmentation
            # this needs is why there is no FTS trigger on `messages`.
            self.conn.execute(
                """
                INSERT INTO messages_fts (rowid, content, session_id, role)
                VALUES (?, ?, ?, ?)
                """,
                (cur.lastrowid, segment_cjk(content), session_id, role),
            )
            # Update session message count
            self.conn.execute(
                "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                (session_id,)
            )
            return cur.lastrowid

    def get_session_messages(self, session_id: str, limit: int = 50,
                             newest: bool = False) -> List[Dict[str, Any]]:
        order = "DESC" if newest else "ASC"
        inner = ("SELECT id, role, content, tool_calls, timestamp FROM messages "
                 f"WHERE session_id = ? ORDER BY id {order} LIMIT ?")
        # `newest` asks for the tail of a long session, which is what a reader rebuilding a live
        # context needs; either way the rows come back oldest-first, the order a conversation is
        # rebuilt in.
        cur = self.conn.execute(
            f"SELECT * FROM ({inner}) ORDER BY id ASC" if newest else inner,
            (session_id, limit)
        )
        return [self._row_to_message(row) for row in cur.fetchall()]

    def get_messages_around(self, session_id: str, anchor_id: int,
                            half: int) -> Optional[List[Dict[str, Any]]]:
        """Window around one message: up to `half` messages before the anchor
        (anchor included) plus up to `half` after, oldest-first.

        Two bounded index seeks instead of fetching a 200-row page and scanning
        it in Python — and unlike that approach it works for anchors past the
        first 200 messages of a long session. Returns None when the anchor id
        is not in this session.
        """
        cols = "id, role, content, tool_calls, timestamp"
        before = self.conn.execute(
            f"SELECT {cols} FROM messages WHERE session_id = ? AND id <= ? "
            f"ORDER BY id DESC LIMIT ?",
            (session_id, anchor_id, half + 1),
        ).fetchall()
        # DESC order: the first row is the greatest id <= anchor, so it equals
        # the anchor exactly when the anchor exists in this session.
        if not before or before[0]["id"] != anchor_id:
            return None
        after = self.conn.execute(
            f"SELECT {cols} FROM messages WHERE session_id = ? AND id > ? "
            f"ORDER BY id ASC LIMIT ?",
            (session_id, anchor_id, half),
        ).fetchall()
        return ([self._row_to_message(r) for r in reversed(before)]
                + [self._row_to_message(r) for r in after])

    @staticmethod
    def _row_to_message(row) -> Dict[str, Any]:
        m = dict(row)
        if m["tool_calls"]:
            try:
                m["tool_calls"] = json.loads(m["tool_calls"])
            except Exception:
                pass
        return m

    def record_session_usage(self, session_id: str, input_tokens: int, output_tokens: int):
        """Fold a turn's billing into the session row, which otherwise stays at 0."""
        with self.conn:
            self.conn.execute(
                """
                UPDATE sessions
                SET input_tokens = COALESCE(input_tokens, 0) + ?,
                    output_tokens = COALESCE(output_tokens, 0) + ?
                WHERE id = ?
                """,
                (input_tokens, output_tokens, session_id)
            )

    def record_timeline_event(self, session_id: str, event_type: str, data: Any):
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO timeline (session_id, event_type, data, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, event_type, json.dumps(data), time.time())
            )

    def read_timeline(self, session_id: Optional[str] = None, event_type: Optional[str] = None,
                      limit: int = 50) -> List[Dict[str, Any]]:
        """The event log, newest first. The loop writes it; before this there was no reader."""
        sql = "SELECT id, session_id, event_type, data, timestamp FROM timeline"
        where: List[str] = []
        args: List[Any] = []
        if session_id:
            where.append("session_id = ?")
            args.append(session_id)
        if event_type:
            where.append("event_type = ?")
            args.append(event_type)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        events = []
        for row in self.conn.execute(sql, args).fetchall():
            entry = dict(row)
            try:
                entry["data"] = json.loads(entry["data"])
            except (TypeError, ValueError):
                pass  # a row written by an older schema is still worth returning raw
            events.append(entry)
        return events

    def prune(self, retention_days: float) -> Dict[str, int]:
        """Delete history older than `retention_days`. Returns what was deleted.

        The FTS index is emptied in the same transaction as `messages`, or a search would
        still return a row whose message is gone. A session row goes only once no message
        of it is left, so nothing is orphaned. 0 (or less) means keep everything.
        """
        if retention_days <= 0:
            return {"messages": 0, "timeline": 0, "sessions": 0}
        cutoff = time.time() - retention_days * 86400
        with self.conn:
            stale = [r[0] for r in self.conn.execute(
                "SELECT id FROM messages WHERE timestamp < ?", (cutoff,)
            ).fetchall()]
            if stale:
                self.conn.executemany("DELETE FROM messages_fts WHERE rowid = ?",
                                      [(rowid,) for rowid in stale])
            messages = self.conn.execute(
                "DELETE FROM messages WHERE timestamp < ?", (cutoff,)
            ).rowcount
            timeline = self.conn.execute(
                "DELETE FROM timeline WHERE timestamp < ?", (cutoff,)
            ).rowcount
            sessions = self.conn.execute(
                """
                DELETE FROM sessions
                WHERE started_at < ? AND id NOT IN (SELECT DISTINCT session_id FROM messages)
                """,
                (cutoff,),
            ).rowcount
        # The file would otherwise keep the freed pages until the process exits.
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"messages": messages, "timeline": timeline, "sessions": sessions}

    @staticmethod
    def _fts_expression(query: str) -> str:
        """Quote every token so FTS5 operators in a model-supplied query cannot raise.

        The query is segmented the same way the indexed text was, so a Chinese query
        matches the per-character tokens a Chinese message was indexed under.
        """
        tokens = [t.replace('"', '""') for t in segment_cjk(query).split()]
        return " ".join(f'"{t}"' for t in tokens if t)

    def search_fts(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search cross-session conversations using SQLite FTS5."""
        expression = self._fts_expression(query or "")
        if not expression:
            return []
        # Joined to `messages` so callers get the original text: the FTS copy is
        # CJK-segmented and would otherwise come back as spaced-out characters.
        cur = self.conn.execute(
            """
            SELECT messages_fts.rowid AS rowid, m.content AS content,
                   m.session_id AS session_id, m.role AS role, messages_fts.rank AS rank
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            WHERE messages_fts MATCH ?
            ORDER BY messages_fts.rank LIMIT ?
            """,
            (expression, limit)
        )
        return [dict(row) for row in cur.fetchall()]

    def close(self):
        self.conn.close()
