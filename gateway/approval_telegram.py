"""Approval qua nut bam Telegram cho Boom.

Flow:
  1. Boom can duyet -> send_approval_request() gui tin nhan co 2 nut inline
     "Dong y" / "Tu choi" (callback_data = approve:<request_id> / deny:<request_id>)
  2. User bam nut -> Telegram goi webhook -> handle_callback() parse update,
     resolve trong ApprovalStore -> daemon doc ket qua va tiep tuc.
  3. Qua 10 phut chua bam -> is_expired() True, daemon tu huy (coi nhu tu choi).

Module nay khong goi Telegram API truc tiep (tru send_approval_request qua
adapter duoc truyen vao), nen logic parse/store test offline duoc.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Dict, Optional

APPROVE_PREFIX = "approve:"
DENY_PREFIX = "deny:"
DEFAULT_TTL_SEC = 600  # 10 phut


def build_keyboard(request_id: str) -> Dict[str, Any]:
    """Inline keyboard 2 nut duyet/tu choi."""
    return {"inline_keyboard": [[
        {"text": "\u2705 Approve",
         "callback_data": f"{APPROVE_PREFIX}{request_id}"},
        {"text": "\u274c Reject",
         "callback_data": f"{DENY_PREFIX}{request_id}"},
    ]]}


def build_approval_text(action_desc: str) -> str:
    return (f"\U0001f510 Y\u00eau c\u1ea7u x\u00e1c nh\u1eadn\n\n"
            f"{action_desc}\n\nB\u1ea5m n\u00fat b\u00ean d\u01b0\u1edbi:")


async def send_approval_request(adapter: Any, chat_id: str, action_desc: str,
                                request_id: str) -> Dict[str, Any]:
    """Gui tin nhan duyet co nut bam. Tra {"ok", "message_id"} hoac {"ok": False, "error"}."""
    if not request_id or ":" in request_id:
        # request_id chua ":" de parse callback khong bi nham
        return {"ok": False, "error": "request_id must be non-empty and contain no ':'"}
    text = build_approval_text(action_desc)
    keyboard = build_keyboard(request_id)

    # Uu tien method chuyen dung cua adapter (neu co)
    send_fn = getattr(adapter, "send_message_markup", None)
    if callable(send_fn):
        try:
            result = await send_fn(chat_id, text, keyboard)
        except Exception as exc:  # noqa: BLE001 - bao loi trung thuc
            return {"ok": False, "error": f"send_message_markup raised: {exc}"}
        if isinstance(result, dict) and result.get("ok"):
            return {"ok": True, "message_id": str(result.get("message_id", ""))}
        return {"ok": False, "error": f"adapter refused: {result}"}

    # Fallback: TelegramAdapter co _api()/_http() -> goi thang sendMessage
    api = getattr(adapter, "_api", None)
    http = getattr(adapter, "_http", None)
    if callable(api) and callable(http):
        try:
            client = http()
            resp = await client.post(
                f"{api()}/sendMessage",
                json={"chat_id": chat_id, "text": text, "reply_markup": keyboard},
            )
            body = resp.json() if hasattr(resp, "json") else {}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"telegram sendMessage raised: {exc}"}
        if isinstance(body, dict) and body.get("ok"):
            mid = body.get("result", {}).get("message_id", "")
            return {"ok": True, "message_id": str(mid)}
        return {"ok": False, "error": f"telegram refused: {body}"}

    return {"ok": False,
            "error": "adapter does not support inline keyboard (no send_message_markup/_api)"}


def parse_approval_callback(callback_data: str) -> Dict[str, Any]:
    """Parse callback_data -> {"decision", "request_id"} hoac {"error"}."""
    if not isinstance(callback_data, str) or not callback_data:
        return {"error": "empty callback_data"}
    if callback_data.startswith(APPROVE_PREFIX):
        rid = callback_data[len(APPROVE_PREFIX):]
        if not rid:
            return {"error": "missing request_id after approve:"}
        return {"decision": "approve", "request_id": rid}
    if callback_data.startswith(DENY_PREFIX):
        rid = callback_data[len(DENY_PREFIX):]
        if not rid:
            return {"error": "missing request_id after deny:"}
        return {"decision": "deny", "request_id": rid}
    return {"error": f"unknown callback prefix: {callback_data[:32]}"}


def is_expired(created_at: float, ttl_sec: int = DEFAULT_TTL_SEC,
               now: Optional[float] = None) -> bool:
    """True neu approval qua han (mac dinh 10 phut)."""
    now = time.time() if now is None else now
    return (now - created_at) > ttl_sec


class ApprovalStore:
    """SQLite luu cac approval dang cho."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS approvals (
                       request_id TEXT PRIMARY KEY,
                       chat_id TEXT NOT NULL,
                       action_desc TEXT NOT NULL,
                       message_id TEXT NOT NULL DEFAULT '',
                       status TEXT NOT NULL DEFAULT 'pending',
                       created_at REAL NOT NULL)"""
            )
            self._db.commit()

    def create(self, request_id: str, chat_id: str, action_desc: str,
               message_id: str = "", created_at: Optional[float] = None) -> Dict[str, Any]:
        if not request_id:
            return {"error": "request_id required"}
        ts = time.time() if created_at is None else created_at
        try:
            with self._lock:
                self._db.execute(
                    "INSERT INTO approvals VALUES (?,?,?,?,?,?)",
                    (request_id, chat_id, action_desc, message_id, "pending", ts),
                )
                self._db.commit()
        except sqlite3.IntegrityError:
            return {"error": f"request_id already exists: {request_id}"}
        return {"ok": True, "request_id": request_id}

    def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute(
                "SELECT request_id, chat_id, action_desc, message_id, status, created_at"
                " FROM approvals WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            return None
        return {"request_id": row[0], "chat_id": row[1], "action_desc": row[2],
                "message_id": row[3], "status": row[4], "created_at": row[5]}

    def resolve(self, request_id: str, decision: str) -> Dict[str, Any]:
        """decision: 'approved' | 'denied'. Chi resolve duoc khi dang pending."""
        if decision not in ("approved", "denied"):
            return {"error": f"bad decision: {decision}"}
        with self._lock:
            cur = self._db.execute(
                "UPDATE approvals SET status=? WHERE request_id=? AND status='pending'",
                (decision, request_id))
            self._db.commit()
            if cur.rowcount == 0:
                return {"error": f"not found or already resolved: {request_id}"}
        return {"ok": True, "request_id": request_id, "status": decision}

    def close(self) -> None:
        with self._lock:
            self._db.close()


def handle_callback(update: Dict[str, Any], store: ApprovalStore) -> Dict[str, Any]:
    """Xu ly webhook update co callback_query. Khong goi network.

    Tra {"request_id", "decision": "approve"|"deny"} hoac {"error"}.
    """
    if not isinstance(update, dict):
        return {"error": "update must be a dict"}
    cq = update.get("callback_query")
    if not isinstance(cq, dict):
        return {"error": "no callback_query in update"}
    parsed = parse_approval_callback(cq.get("data", ""))
    if "error" in parsed:
        return parsed
    decision = parsed["decision"]
    rid = parsed["request_id"]
    rec = store.get(rid)
    if rec is None:
        return {"error": f"unknown request_id: {rid}"}
    if rec["status"] != "pending":
        return {"error": f"already resolved: {rid} ({rec['status']})"}
    if is_expired(rec["created_at"]):
        store.resolve(rid, "denied")
        return {"error": f"approval expired: {rid}"}
    store.resolve(rid, "approved" if decision == "approve" else "denied")
    return {"request_id": rid, "decision": decision}
