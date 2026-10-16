"""Auto Report for Boom: logs group messages then generates reports automatically.

Follows the repo's exact patterns:
- SQLite + threading.Lock (like gateway/broadcast.py)
- stdlib only: sqlite3, csv, re, datetime, collections
- Sends reports via a caller-provided send_fn (like daemon.send_to being
  bound into the cron deliver hook in agent/src/tools/cronjob_tool.py).

Usage cycle:
1. Every group message passes through: gen.log_message(channel, group_id, ...)
2. Cron runs `cron_daily_job(channel, group_id, send_fn)` at 7am to send
   yesterday's summary, `cron_weekly_job(send_fn)` Monday morning for the
   weekly digest. (Attach via cron_add + deliver_to, or call directly from
   the scheduler.)
3. CSV export when the business needs it: gen.export_csv(...)
"""

from __future__ import annotations

import csv
import re
import sqlite3
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

# ---------------------------------------------------------------------------
# Stopwords: extract meaningful keywords, drop Vietnamese/English function
# words + words shorter than 3 chars.
# NOTE: the word list below is functional keyword data — kept as-is.
# ---------------------------------------------------------------------------
_STOPWORDS = frozenset("""
của là và có trong với không được cho các đã đang một này những tôi bạn anh
chị em mình thế rất để tại về từ nhưng nếu thì mà như cũng nên sẽ lên đó đây
kia ấy thì là thì
the and for with that this you your are was were have has is not but from
they will can all our its their what when where how about who which his her
she him also than then
""".split())

_WORD_RE = re.compile(r"\w+", re.UNICODE)

_TOP_SENDERS = 5   # most active senders in the daily summary
_TOP_KEYWORDS = 10  # keywords in the daily summary
_TOP_GROUPS = 10   # most active groups in the weekly digest


def _as_date(d: Optional[Union[date, str]]) -> date:
    """Normalizes a date parameter: date | 'YYYY-MM-DD' | None -> date (None = today)."""
    if d is None:
        return date.today()
    if isinstance(d, date):
        return d
    try:
        return datetime.strptime(str(d), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"date must be a datetime.date or 'YYYY-MM-DD', got {d!r}")


def _day_bounds(d: date) -> tuple:
    """The [start_ts, end_ts) range of one day in the host's local time."""
    start = datetime(d.year, d.month, d.day)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


class ReportGenerator:
    """Logs group messages to SQLite and generates reports. Thread-safe."""

    def __init__(self, db_path: Any) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS messages (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       channel TEXT NOT NULL,
                       group_id TEXT NOT NULL,
                       sender_id TEXT NOT NULL DEFAULT '',
                       sender_name TEXT NOT NULL DEFAULT '',
                       text TEXT NOT NULL DEFAULT '',
                       timestamp REAL NOT NULL
                   )"""
            )
            # Index for fast per-day/per-hour queries of one group.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_day "
                "ON messages (channel, group_id, timestamp)"
            )

    # -- plumbing ------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # -- log a message -------------------------------------------------------
    def log_message(self, channel: str, group_id: str, sender_id: str,
                    sender_name: str, text: str,
                    timestamp: Optional[float] = None) -> int:
        """Logs one message. Returns the id of the inserted row.

        `timestamp`: epoch seconds; None = now.
        Never raises on bad input: validation failures -> clear ValueError.
        """
        channel = (channel or "").strip()
        group_id = (group_id or "").strip()
        if not channel or not group_id:
            raise ValueError("channel and group_id must not be empty")
        ts = time.time() if timestamp is None else float(timestamp)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages (channel, group_id, sender_id, sender_name, text, timestamp)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (channel, group_id, sender_id or "", sender_name or "",
                 text or "", ts),
            )
            return int(cur.lastrowid)

    def _fetch(self, channel: str, group_id: Optional[str],
               start_ts: float, end_ts: float) -> List[sqlite3.Row]:
        """Fetches one group's messages (or a whole channel's) in a time range."""
        sql = ("SELECT * FROM messages WHERE timestamp >= ? AND timestamp < ?")
        params: List[Any] = [start_ts, end_ts]
        if channel is not None:
            sql += " AND channel = ?"
            params.append(channel)
        if group_id is not None:
            sql += " AND group_id = ?"
            params.append(group_id)
        sql += " ORDER BY timestamp"
        with self._lock, self._connect() as conn:
            return conn.execute(sql, params).fetchall()

    # -- daily report --------------------------------------------------------
    def daily_summary(self, channel: str, group_id: str,
                      d: Optional[Union[date, str]] = None) -> Dict[str, Any]:
        """Summarizes one day of one group.

        Returns a dict: {date, total_messages, unique_senders,
        top_senders: [{sender_name, count}], top_keywords: [{word, count}],
        hour_histogram: [24 numbers]}.
        """
        day = _as_date(d)
        start_ts, end_ts = _day_bounds(day)
        rows = self._fetch(channel, group_id, start_ts, end_ts)

        senders = Counter()
        keywords = Counter()
        hours = [0] * 24
        for row in rows:
            name = row["sender_name"] or row["sender_id"] or "?"
            senders[name] += 1
            hours[datetime.fromtimestamp(row["timestamp"]).hour] += 1
            for w in _extract_keywords(row["text"]):
                keywords[w] += 1

        return {
            "date": day.isoformat(),
            "channel": channel,
            "group_id": group_id,
            "total_messages": len(rows),
            "unique_senders": len(senders),
            "top_senders": [
                {"sender_name": name, "count": c}
                for name, c in senders.most_common(_TOP_SENDERS)
            ],
            "top_keywords": [
                {"word": w, "count": c}
                for w, c in keywords.most_common(_TOP_KEYWORDS)
            ],
            "hour_histogram": hours,
        }

    # -- weekly digest -------------------------------------------------------
    def weekly_digest(self, channel: Optional[str] = None) -> Dict[str, Any]:
        """Aggregates the last 7 days (including today). channel=None -> all channels.

        Returns {week_start, week_end, total_messages, active_groups,
        top_groups: [{channel, group_id, count}], total_senders}.
        """
        today = date.today()
        week_start = today - timedelta(days=6)
        start_ts, _ = _day_bounds(week_start)
        _, end_ts = _day_bounds(today)  # through the end of today
        rows = self._fetch(channel, None, start_ts, end_ts)

        groups = Counter()
        senders = set()
        for row in rows:
            groups[(row["channel"], row["group_id"])] += 1
            senders.add(row["sender_id"] or row["sender_name"])

        return {
            "week_start": week_start.isoformat(),
            "week_end": today.isoformat(),
            "channel": channel,  # None = all channels
            "total_messages": len(rows),
            "active_groups": len(groups),
            "top_groups": [
                {"channel": ch, "group_id": gid, "count": c}
                for (ch, gid), c in groups.most_common(_TOP_GROUPS)
            ],
            "total_senders": len(senders),
        }

    # -- CSV export ----------------------------------------------------------
    def export_csv(self, channel: str, group_id: str,
                   start_ts: float, end_ts: float,
                   path: Any) -> int:
        """Exports messages to CSV. Columns: timestamp, sender_name, text.

        `start_ts`/`end_ts`: epoch seconds. Returns rows exported (excluding header).
        """
        path = Path(path)
        if path.parent != Path(".") and str(path.parent):
            path.parent.mkdir(parents=True, exist_ok=True)
        rows = self._fetch(channel, group_id, float(start_ts), float(end_ts))
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "sender_name", "text"])
            for row in rows:
                writer.writerow([
                    datetime.fromtimestamp(row["timestamp"]).isoformat(timespec="seconds"),
                    row["sender_name"],
                    row["text"],
                ])
        return len(rows)

    # -- prune old data ------------------------------------------------------
    def prune_old(self, days: int = 90) -> int:
        """Deletes messages older than `days` days. Returns rows deleted."""
        cutoff = time.time() - int(days) * 86400
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))
            return int(cur.rowcount)

    # -- raw count -----------------------------------------------------------
    def count(self) -> int:
        """Total messages stored (for tests/monitoring)."""
        with self._lock, self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])


def _extract_keywords(text: str) -> List[str]:
    """Tokenizes, lowercases, drops Vietnamese/English stopwords and <3-char words."""
    out = []
    for w in _WORD_RE.findall((text or "").lower()):
        if len(w) >= 3 and w not in _STOPWORDS:
            out.append(w)
    return out


# ---------------------------------------------------------------------------
# Render Vietnamese text for Telegram.
# NOTE: the exact wording below is pinned by evals/test_reports.py
# ("BÁO CÁO NGÀY", "TỔNG KẾT TUẦN", ...) — keep as-is.
# ---------------------------------------------------------------------------
def format_daily_text(summary: Dict[str, Any]) -> str:
    """Renders a daily_summary dict into pretty text for Telegram."""
    day = summary.get("date", "")
    try:
        day_vn = datetime.strptime(day, "%Y-%m-%d").strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        day_vn = str(day)
    lines = [
        "📊 BÁO CÁO NGÀY " + day_vn,
        "━━━━━━━━━━━━━━━",
        f"💬 Tổng tin nhắn: {summary.get('total_messages', 0)}",
        f"👥 Người tham gia: {summary.get('unique_senders', 0)}",
    ]
    top = summary.get("top_senders") or []
    if top:
        lines.append("")
        lines.append("🏆 Tích cực nhất:")
        for i, s in enumerate(top, 1):
            lines.append(f"  {i}. {s['sender_name']} — {s['count']} tin")
    kw = summary.get("top_keywords") or []
    if kw:
        lines.append("")
        lines.append("🔑 Từ khóa nổi bật: " + ", ".join(k["word"] for k in kw[:8]))
    hist = summary.get("hour_histogram") or []
    if hist and sum(hist) > 0:
        peak = max(range(24), key=lambda h: hist[h])
        lines.append(f"⏰ Giờ cao điểm: {peak:02d}:00 ({hist[peak]} tin)")
    lines.append("━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def format_weekly_text(digest: Dict[str, Any]) -> str:
    """Renders a weekly_digest dict into Vietnamese text for Telegram."""
    try:
        ws = datetime.strptime(digest["week_start"], "%Y-%m-%d").strftime("%d/%m")
        we = datetime.strptime(digest["week_end"], "%Y-%m-%d").strftime("%d/%m/%Y")
    except (ValueError, TypeError, KeyError):
        ws, we = digest.get("week_start", ""), digest.get("week_end", "")
    lines = [
        f"🗓️ TỔNG KẾT TUẦN ({ws} – {we})",
        "━━━━━━━━━━━━━━━",
        f"💬 Tổng tin nhắn: {digest.get('total_messages', 0)}",
        f"🏘️ Groups hoạt động: {digest.get('active_groups', 0)}",
        f"👥 Người tham gia: {digest.get('total_senders', 0)}",
    ]
    top = digest.get("top_groups") or []
    if top:
        lines.append("")
        lines.append("🔥 Groups sôi nổi nhất:")
        for i, g in enumerate(top, 1):
            lines.append(f"  {i}. [{g['channel']}] {g['group_id']} — {g['count']} tin")
    lines.append("━━━━━━━━━━━━━━━")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cron integration: two job functions, send_fn provided by the caller.
# send_fn(text) -> bool. A job never raises on send errors: the send result is
# recorded in the returned dict, like the repo's cron runner records delivery.
# ---------------------------------------------------------------------------
def cron_daily_job(channel: str, group_id: str,
                   send_fn: Callable[[str], Any],
                   db_path: Any,
                   d: Optional[Union[date, str]] = None) -> Dict[str, Any]:
    """Morning job: builds YESTERDAY's daily_summary and sends it via send_fn."""
    gen = ReportGenerator(db_path)
    yesterday = (_as_date(d) if d is not None else date.today()) - timedelta(days=1)
    summary = gen.daily_summary(channel, group_id, yesterday)
    text = format_daily_text(summary)
    try:
        ok = bool(send_fn(text))
        return {"ok": True, "summary": summary, "sent": ok}
    except Exception as e:  # send errors don't fail the job, per repo pattern
        return {"ok": False, "summary": summary, "error": f"{type(e).__name__}: {e}"}


def cron_weekly_job(send_fn: Callable[[str], Any],
                    db_path: Any,
                    channel: Optional[str] = None) -> Dict[str, Any]:
    """Monday-morning job: builds the weekly_digest and sends it via send_fn."""
    gen = ReportGenerator(db_path)
    digest = gen.weekly_digest(channel)
    text = format_weekly_text(digest)
    try:
        ok = bool(send_fn(text))
        return {"ok": True, "digest": digest, "sent": ok}
    except Exception as e:
        return {"ok": False, "digest": digest, "error": f"{type(e).__name__}: {e}"}
