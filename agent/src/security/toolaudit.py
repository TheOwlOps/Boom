# -*- coding: utf-8 -*-
"""Audit moi tool call: SQLite + redact secret + phat hien bat thuong.

Lop chinh:
- ``ToolCallRecord``: 1 ban ghi audit (pydantic model).
- ``ToolAuditLog``: SQLite store, bang ``tool_calls`` voi cac cot
  (id, timestamp, user, tool_name, args_redacted, result_summary,
  duration_ms, success):
    - ``log_call()``: ghi 1 call; args duoc redact secret bang
      ``security.redact.redact_args`` truoc khi persist.
    - ``get_history(tool_name, limit)``: lich su goi cua 1 tool.
    - ``get_user_activity(user, limit)``: lich su goi cua 1 user.
    - ``detect_anomaly()``: phat hien bat thuong:
        * cung user goi 1 tool > 50 lan trong 1 gio,
        * 1 tool fail lien tuc > 10 lan.

Chi dung stdlib + pydantic. Import theo style repo (``from security.xxx``)
de khi copy file vao ``agent/src/security/`` thi chay ngay khong can sua.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field

from security.redact import redact_args, redact_text

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Nguong phat hien bat thuong
# ---------------------------------------------------------------------------
HIGH_FREQ_THRESHOLD = 50        # cung user goi 1 tool > 50 lan / cua so gio
HIGH_FREQ_WINDOW_HOURS = 1     # cua so thoi gian (gio)
CONSECUTIVE_FAIL_THRESHOLD = 10  # 1 tool fail lien tuc > 10 lan
RESULT_SUMMARY_MAX_LEN = 500  # do dai toi da cua result_summary khi luu

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    user TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args_redacted TEXT NOT NULL,
    result_summary TEXT NOT NULL,
    duration_ms REAL NOT NULL,
    success INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_ts
    ON tool_calls (tool_name, timestamp);
CREATE INDEX IF NOT EXISTS idx_tool_calls_user_ts
    ON tool_calls (user, timestamp);
"""


def _now_iso() -> str:
    """Timestamp ISO-8601 hien tai (so sanh lexicographic van dung)."""
    return datetime.now().isoformat(timespec="seconds")


class ToolCallRecord(BaseModel):
    """1 ban ghi tool call da audit."""

    id: int
    timestamp: str = ""
    user: str = ""
    tool_name: str = ""
    args_redacted: Dict[str, Any] = Field(default_factory=dict)
    result_summary: str = ""
    duration_ms: float = 0.0
    success: bool = True


class ToolAuditLog:
    """SQLite audit log cho tool call — thread-safe.

    Args:
        db_path: duong dan file SQLite, hoac ``":memory:"`` cho test.

    Ghi chu: bang nay la append-only theo thiet ke (khong co ham xoa/sua),
    giong triet ly cua ``audit/store.py`` trong repo.
    """

    def __init__(self, db_path: Union[str, Path]) -> None:
        str_path = str(db_path)
        self._memory = str_path == ":memory:"
        if self._memory:
            self._db_path = ":memory:"
        else:
            p = Path(str_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._db_path = str(p)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------ internal
    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ToolCallRecord:
        """Chuyen 1 row SQLite thanh ToolCallRecord."""
        try:
            args = json.loads(row["args_redacted"])
        except Exception:
            args = {}
        if not isinstance(args, dict):
            args = {"_raw": args}
        return ToolCallRecord(
            id=int(row["id"]),
            timestamp=str(row["timestamp"]),
            user=str(row["user"]),
            tool_name=str(row["tool_name"]),
            args_redacted=args,
            result_summary=str(row["result_summary"]),
            duration_ms=float(row["duration_ms"]),
            success=bool(row["success"]),
        )

    def _consecutive_failures(self, tool_name: str) -> int:
        """Dem so lan fail LIEN TUC gan nhat cua 1 tool (gap success thi dung)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT success FROM tool_calls WHERE tool_name = ?"
                " ORDER BY id DESC LIMIT ?",
                (tool_name, CONSECUTIVE_FAIL_THRESHOLD + 20),
            )
            rows = cur.fetchall()
        fails = 0
        for r in rows:
            if r["success"]:
                break
            fails += 1
        return fails

    # ---------------------------------------------------------------- api
    def log_call(
        self,
        user: str,
        tool_name: str,
        args: Optional[Dict[str, Any]],
        result: Any,
        duration_ms: float,
        success: bool,
    ) -> int:
        """Ghi 1 tool call vao audit.

        - ``args`` duoc redact secret (``redact_args``) truoc khi luu —
          khong bao gio luu secret tho vao DB.
        - ``result_summary`` cung duoc redact + cat ngan.
        - Fail-open: KHONG BAO GIO raise; loi thi log warning va tra ve -1.
        """
        try:
            redacted = redact_args(tool_name, args)
            if not isinstance(redacted, dict):
                redacted = {}
            summary = redact_text(result)
            if len(summary) > RESULT_SUMMARY_MAX_LEN:
                summary = summary[:RESULT_SUMMARY_MAX_LEN] + "...[cắt bớt]"
            with self._lock:
                cur = self._conn.execute(
                    "INSERT INTO tool_calls"
                    " (timestamp, user, tool_name, args_redacted,"
                    "  result_summary, duration_ms, success)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        _now_iso(),
                        str(user),
                        str(tool_name),
                        json.dumps(redacted, ensure_ascii=False, default=str),
                        summary,
                        float(duration_ms),
                        1 if success else 0,
                    ),
                )
                self._conn.commit()
                return int(cur.lastrowid)
        except Exception as exc:  # noqa: BLE001 — audit khong duoc pha tool
            _log.warning("log_call that bai (fail-open): %r", exc)
            return -1

    def get_history(
        self, tool_name: str, limit: int = 100
    ) -> List[ToolCallRecord]:
        """Lich su goi cua 1 tool, moi nhat truoc. ``limit`` toi da 1000."""
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM tool_calls WHERE tool_name = ?"
                " ORDER BY id DESC LIMIT ?",
                (str(tool_name), limit),
            )
            rows = cur.fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_user_activity(
        self, user: str, limit: int = 100
    ) -> List[ToolCallRecord]:
        """Lich su goi tool cua 1 user, moi nhat truoc. ``limit`` toi da 1000."""
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM tool_calls WHERE user = ?"
                " ORDER BY id DESC LIMIT ?",
                (str(user), limit),
            )
            rows = cur.fetchall()
        return [self._row_to_record(r) for r in rows]

    def count(self) -> int:
        """Tong so ban ghi — de monitor/debug."""
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) AS n FROM tool_calls")
            return int(cur.fetchone()["n"])

    def detect_anomaly(self) -> List[Dict[str, Any]]:
        """Phat hien bat thuong trong lich su tool call.

        Tra ve list dict, moi dict co ``type`` la mot trong:
        - ``"high_frequency"``: cung user goi 1 tool > 50 lan trong 1 gio
          gan nhat (dau hieu spam / vong lap agent bi ket).
        - ``"consecutive_failures"``: 1 tool fail lien tuc > 10 lan gan
          nhat (dau hieu tool hong / du lieu dau vao sai he thong).
        List rong = khong co bat thuong.
        """
        anomalies: List[Dict[str, Any]] = []

        # Rule 1: tan suat cao bat thuong theo cap (user, tool).
        since = (
            datetime.now() - timedelta(hours=HIGH_FREQ_WINDOW_HOURS)
        ).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "SELECT user, tool_name, COUNT(*) AS n FROM tool_calls"
                " WHERE timestamp >= ?"
                " GROUP BY user, tool_name HAVING n > ?",
                (since, HIGH_FREQ_THRESHOLD),
            )
            freq_rows = cur.fetchall()
            cur = self._conn.execute("SELECT DISTINCT tool_name FROM tool_calls")
            tools = [r["tool_name"] for r in cur.fetchall()]
        for row in freq_rows:
            anomalies.append(
                {
                    "type": "high_frequency",
                    "user": row["user"],
                    "tool_name": row["tool_name"],
                    "count": int(row["n"]),
                    "window": "%dh" % HIGH_FREQ_WINDOW_HOURS,
                    "threshold": HIGH_FREQ_THRESHOLD,
                }
            )

        # Rule 2: fail lien tuc (kiem tung tool, gap success thi reset).
        for t in tools:
            fails = self._consecutive_failures(t)
            if fails > CONSECUTIVE_FAIL_THRESHOLD:
                anomalies.append(
                    {
                        "type": "consecutive_failures",
                        "tool_name": t,
                        "fail_count": fails,
                        "threshold": CONSECUTIVE_FAIL_THRESHOLD,
                    }
                )
        return anomalies
