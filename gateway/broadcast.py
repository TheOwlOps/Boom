"""Bulk messaging: send one text to many channel targets.

Enterprise use case: a business managing many groups (Telegram groups,
Zalo OA followers, Discord servers...) needs to push one announcement to
all of them at once, without tripping rate limits.

Design:
- :func:`broadcast` is the core: sequential sends with a pause between
  each so platforms don't rate-limit the burst. One failing target never
  blocks the rest — every target is independent and its outcome is
  recorded.
- :class:`BroadcastStore` keeps a SQLite history of every broadcast.
- :func:`build_tools` exposes ``broadcast_send`` / ``broadcast_history``
  in the repo's tool convention.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

# send_fn(channel, target, text) -> bool. Matches daemon.send_to / registry.send_to.
SendFn = Callable[[str, str, str], Awaitable[bool]]
# sleep(seconds) -> awaitable. Injectable so tests can use a fake clock.
SleepFn = Callable[[float], Awaitable[None]]


def _validate_targets(targets: Any) -> Optional[str]:
    if not isinstance(targets, list) or not targets:
        return "targets must be a non-empty list of {channel, target}"
    for i, t in enumerate(targets):
        if not isinstance(t, dict):
            return "targets[%d] must be a dict {channel, target}" % i
        if not t.get("channel") or not t.get("target"):
            return ("targets[%d] needs non-empty 'channel' and 'target'"
                    % i)
    return None


async def broadcast(send_fn: SendFn,
                    targets: List[Dict[str, str]],
                    text: str,
                    delay_sec: float = 1.0,
                    sleep: SleepFn = asyncio.sleep) -> Dict[str, Any]:
    """Send *text* to every target, pausing *delay_sec* between sends.

    Returns ``{"sent": n, "failed": m, "results": [...]}`` where each result
    is ``{"channel", "target", "ok": bool}`` plus ``"error"`` on failure.
    Never raises: a send_fn exception is recorded as that target's error.
    """
    err = _validate_targets(targets)
    if err:
        return {"error": err}
    if not (text or "").strip():
        return {"error": "text is empty: nothing to broadcast"}
    if delay_sec < 0:
        delay_sec = 0.0

    results: List[Dict[str, Any]] = []
    sent = 0
    for i, t in enumerate(targets):
        channel = str(t["channel"])
        target = str(t["target"])
        try:
            ok = await send_fn(channel, target, text)
            ok = bool(ok)
        except Exception as e:  # one bad target must not stop the rest
            results.append({"channel": channel, "target": target,
                            "ok": False, "error": "%s: %s"
                            % (type(e).__name__, e)})
            continue
        if ok:
            sent += 1
            results.append({"channel": channel, "target": target, "ok": True})
        else:
            results.append({"channel": channel, "target": target,
                            "ok": False, "error": "send_fn returned falsy"})
        if delay_sec and i < len(targets) - 1:
            try:
                await sleep(delay_sec)
            except Exception:
                pass  # a broken clock must not fail the broadcast
    return {"sent": sent, "failed": len(targets) - sent, "results": results}


class BroadcastStore:
    """SQLite history of broadcasts. Thread-safe."""

    def __init__(self, db_path: Any) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS broadcasts (
                       id TEXT PRIMARY KEY,
                       text TEXT NOT NULL,
                       targets TEXT NOT NULL,
                       sent_count INTEGER NOT NULL DEFAULT 0,
                       failed_count INTEGER NOT NULL DEFAULT 0,
                       created_by TEXT,
                       created_at TEXT NOT NULL
                   )""")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, text: str, targets: List[Dict[str, str]],
               sent_count: int, failed_count: int,
               created_by: Optional[str] = None,
               broadcast_id: Optional[str] = None) -> str:
        bid = broadcast_id or "bc_%s" % uuid.uuid4().hex[:12]
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO broadcasts (id, text, targets, sent_count,"
                " failed_count, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (bid, text, json.dumps(targets), int(sent_count),
                 int(failed_count), created_by,
                 time.strftime("%Y-%m-%dT%H:%M:%S")))
        return bid

    def get(self, broadcast_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM broadcasts WHERE id = ?",
                (broadcast_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def history(self, limit: int = 20) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM broadcasts ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["targets"] = json.loads(d["targets"])
        except (ValueError, TypeError):
            pass
        return d


def _resolve_root(env: Any) -> Optional[Path]:
    if env is None:
        return None
    root = getattr(env, "root_dir", None) or getattr(env, "root", None)
    if isinstance(root, str) and root:
        return Path(root)
    if isinstance(root, Path):
        return root
    return None


def _run_async(coro: Awaitable[Any]) -> Any:
    """Run a coroutine from sync code, inside or outside a running loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        return asyncio.run(coro)
    # Already inside a loop: run it on a helper thread with its own loop.
    box: Dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["result"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001 - propagate to caller
            box["error"] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=300)
    if t.is_alive():
        raise TimeoutError("broadcast timed out after 300s")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def build_tools(env: Any = None) -> Dict[str, Dict[str, Any]]:
    """broadcast_send / broadcast_history in the repo tool convention."""

    def _need_store() -> Any:
        root = _resolve_root(env)
        if root is None:
            return None, {"error": "environment has no root_dir: "
                                   "nowhere to keep broadcast history"}
        try:
            return BroadcastStore(root / "memories" / "broadcast.db"), None
        except OSError as e:
            return None, {"error": "could not open broadcast db: %s" % (e,)}

    def _send_fn() -> Any:
        daemon = getattr(env, "daemon", None) if env is not None else None
        fn = getattr(daemon, "send_to", None) if daemon else None
        if not callable(fn):
            return None, {"error": "environment has no daemon.send_to: "
                                   "cannot send messages"}
        return fn, None

    def broadcast_send(targets: List[Dict[str, str]], text: str,
                       dry_run: bool = False,
                       delay_sec: float = 1.0,
                       created_by: Optional[str] = None) -> Dict[str, Any]:
        err = _validate_targets(targets)
        if err:
            return {"error": err}
        if not (text or "").strip():
            return {"error": "text is empty: nothing to broadcast"}
        if dry_run:
            return {"dry_run": True,
                    "would_send": [{"channel": str(t["channel"]),
                                    "target": str(t["target"])}
                                   for t in targets],
                    "count": len(targets)}
        store, serr = _need_store()
        if serr:
            return serr
        send_fn, ferr = _send_fn()
        if ferr:
            return ferr
        try:
            report = _run_async(broadcast(send_fn, targets, text,
                                          delay_sec=delay_sec))
        except Exception as e:
            return {"error": "broadcast failed: %s: %s"
                    % (type(e).__name__, e)}
        if "error" in report:
            return report
        bid = store.record(text, targets, report["sent"], report["failed"],
                           created_by=created_by)
        report["broadcast_id"] = bid
        return report

    def broadcast_history(limit: int = 20) -> Dict[str, Any]:
        store, serr = _need_store()
        if serr:
            return serr
        return {"broadcasts": store.history(limit)}

    return {
        "broadcast_send": {
            "name": "broadcast_send",
            "func": broadcast_send,
            "description": ("Send one text message to many channel targets "
                            "(rate-limit friendly). dry_run previews without sending."),
            "schema": {
                "type": "object",
                "properties": {
                    "targets": {
                        "type": "array",
                        "description": "List of {channel, target}",
                        "items": {"type": "object"},
                    },
                    "text": {"type": "string"},
                    "dry_run": {"type": "boolean",
                                "description": "Preview only, do not send"},
                    "delay_sec": {"type": "number",
                                  "description": "Pause between sends (default 1.0)"},
                    "created_by": {"type": "string"},
                },
                "required": ["targets", "text"],
            },
        },
        "broadcast_history": {
            "name": "broadcast_history",
            "func": broadcast_history,
            "description": "List past broadcasts (newest first).",
            "schema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max rows (default 20)"},
                },
            },
        },
    }
