"""Offline eval for CJK full-text search in memory.state_db (no network)."""

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from memory.state_db import StateDB, segment_cjk

ZH = "内存泄漏的修复方法：用智能指针避免引用环。"
JA = "メモリリークの修正方法を説明します。"
EN = "How do I fix memory leaks in Rust? Use Arc and weak references."

# The pre-CJK schema: raw trigger, unsegmented FTS rows.
LEGACY_SQL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, model TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL,
    message_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT NOT NULL, tool_calls TEXT, timestamp REAL NOT NULL
);
CREATE VIRTUAL TABLE messages_fts USING fts5(
    content, session_id UNINDEXED, role UNINDEXED, tokenize = 'porter unicode61'
);
CREATE TRIGGER trg_messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, session_id, role)
    VALUES (new.id, new.content, new.session_id, new.role);
END;
"""


def test_cjk_search():
    tmp = Path(tempfile.mkdtemp(prefix="cjk_search_eval_"))
    try:
        # 1. Segmentation is a no-op for ASCII and splits each CJK character.
        assert segment_cjk(EN) == EN
        assert segment_cjk("中文") == "中 文 "
        assert segment_cjk("メモリ") == "メ モ リ "
        print("[PASS] segment_cjk leaves ASCII alone and splits CJK per character.")

        # 2. Upgrading a pre-CJK database re-indexes its existing rows.
        legacy_path = tmp / "legacy.db"
        conn = sqlite3.connect(str(legacy_path))
        conn.executescript(LEGACY_SQL)
        conn.execute("INSERT INTO sessions (id, model, started_at) VALUES ('s0', 'm', 1)")
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s0', 'user', ?, 1)",
            (ZH,),
        )
        conn.commit()
        before = conn.execute("SELECT content FROM messages_fts WHERE rowid = 1").fetchone()[0]
        conn.close()
        assert before == ZH, "fixture must start with unsegmented FTS rows"

        legacy = StateDB(legacy_path)
        hits = legacy.search_fts("内存泄漏")
        assert [h["content"] for h in hits] == [ZH], hits
        conn = sqlite3.connect(str(legacy_path))
        triggers = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name='trg_messages_ai'"
        ).fetchone()[0]
        # A surviving raw trigger would re-insert unsegmented text on any connection.
        assert triggers == 0, "raw FTS trigger must be dropped by the migration"
        conn.close()
        legacy.close()
        print("[PASS] pre-CJK database re-indexed; raw FTS trigger dropped.")

        # 3. Fresh connection: inserts index segmented text, searches find it.
        db = StateDB(tmp / "state.db")
        db.create_session("s1")
        db.append_message("s1", "user", ZH)
        db.append_message("s1", "user", JA)
        db.append_message("s1", "user", EN)

        hits = db.search_fts("内存泄漏")
        assert [h["content"] for h in hits] == [ZH], hits
        hits = db.search_fts("泄漏")  # substring, not a whole token
        assert [h["content"] for h in hits] == [ZH], hits
        hits = db.search_fts("智能指针")
        assert [h["content"] for h in hits] == [ZH], hits
        print("[PASS] Chinese query and substrings match the Chinese row.")

        hits = db.search_fts("メモリリーク")
        assert [h["content"] for h in hits] == [JA], hits
        hits = db.search_fts("リーク")  # Katakana substring
        assert [h["content"] for h in hits] == [JA], hits
        print("[PASS] Japanese query and substrings match the Japanese row.")

        # 4. English behaviour is unchanged (and unaffected by the CJK rows).
        hits = db.search_fts("memory leaks")
        assert [h["content"] for h in hits] == [EN], hits
        hits = db.search_fts("leak")  # porter stemming still applies
        assert [h["content"] for h in hits] == [EN], hits
        assert db.search_fts("zzzznope") == []
        print("[PASS] English queries unchanged; no cross-script false hits.")

        # 5. A CJK row is reachable from a second StateDB on the same file.
        db.close()
        reopened = StateDB(tmp / "state.db")
        assert [h["content"] for h in reopened.search_fts("泄漏")] == [ZH]
        # Reopening must not re-run the migration or duplicate rows.
        assert len(reopened.search_fts("memory leaks")) == 1
        reopened.close()
        print("[PASS] reopening the database keeps one indexed row per message.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("ALL CJK SEARCH TESTS PASSED.")


if __name__ == "__main__":
    test_cjk_search()
