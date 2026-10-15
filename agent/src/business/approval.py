"""Multi-step approval flow cho Boom (doanh nghiệp).

Flow chuẩn: operator gửi yêu cầu -> admin duyệt -> callback chạy execute.

    flow = ApprovalFlow("approvals.db")
    res = flow.request_approval(
        "broadcast",
        requester="op_1",
        details={"channel": "telegram", "text": "Khuyến mãi tháng 10"},
        approvers=["admin_1", "admin_2"],
        timeout_minutes=30,
    )
    rid = res["request_id"]
    flow.on_approved(rid, lambda req: execute_broadcast(req["details"]))
    flow.approve(rid, "admin_1")   # -> "approved", callback chạy

Tích hợp Telegram: format_telegram_message() sinh text + inline keyboard
2 nút, dùng đúng callback_data "approve:<request_id>" / "deny:<request_id>"
như gateway/approval_telegram.py (cùng prefix, cùng label nút) nên hàm
parse_approval_callback() bên đó parse được ngay. handle_telegram_callback()
nhận webhook update và điều hướng vào approve()/reject() của flow này.

Pattern theo repo: SQLite + threading.Lock, stdlib only. Mỗi lần đổi trạng
thái ghi 1 audit event nếu import được module audit của repo (try/except,
thiếu audit vẫn chạy bình thường, không bao giờ crash vì audit).
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Tích hợp audit của repo (optional).
# Khi file này nằm trong repo Boom (agent/src trên sys.path) thì import được
# audit.store / audit.models và tự ghi audit. Chạy standalone thì bỏ qua.
# ---------------------------------------------------------------------------
try:
    from audit.models import AuditEvent as _AuditEvent  # type: ignore
    from audit.store import AuditStore as _AuditStore  # type: ignore
    _AUDIT_OK = True
except Exception:  # noqa: BLE001 - thiếu audit không phải lỗi
    _AuditEvent = None
    _AuditStore = None
    _AUDIT_OK = False

# ---------------------------------------------------------------------------
# Telegram: giữ đúng format với gateway/approval_telegram.py
# (APPROVE_PREFIX / DENY_PREFIX / label nút giống hệt bên đó).
# ---------------------------------------------------------------------------
APPROVE_PREFIX = "approve:"
DENY_PREFIX = "deny:"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    request_id       TEXT PRIMARY KEY,
    action           TEXT NOT NULL,
    requester        TEXT NOT NULL,
    details_json     TEXT NOT NULL DEFAULT '{}',
    approvers_json   TEXT NOT NULL DEFAULT '[]',
    required_approvals INTEGER NOT NULL DEFAULT 1,
    timeout_sec      REAL NOT NULL DEFAULT 1800,
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       REAL NOT NULL,
    decided_at       REAL,
    decided_by       TEXT NOT NULL DEFAULT '',
    reason           TEXT NOT NULL DEFAULT ''
);
-- Mỗi approver chỉ vote 1 lần cho 1 request (tránh đếm trùng khi
-- required_approvals > 1).
CREATE TABLE IF NOT EXISTS approval_votes (
    request_id TEXT NOT NULL,
    approver   TEXT NOT NULL,
    decision   TEXT NOT NULL,
    ts         REAL NOT NULL,
    PRIMARY KEY (request_id, approver)
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals (status);
"""


def _build_keyboard(request_id: str) -> Dict[str, Any]:
    """Inline keyboard 2 nút duyệt/từ chối.

    Giống hệt gateway/approval_telegram.build_keyboard() để tương thích.
    """
    return {"inline_keyboard": [[
        {"text": "✅ Đồng ý",
         "callback_data": f"{APPROVE_PREFIX}{request_id}"},
        {"text": "❌ Từ chối",
         "callback_data": f"{DENY_PREFIX}{request_id}"},
    ]]}


def _fmt_details(details: Dict[str, Any]) -> str:
    """Render dict details thành nhiều dòng dễ đọc trên Telegram."""
    if not details:
        return "(không có chi tiết)"
    lines = []
    for key, val in details.items():
        text = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        if len(text) > 300:
            text = text[:297] + "..."
        lines.append(f"• {key}: {text}")
    return "\n".join(lines)


class ApprovalFlow:
    """Multi-step approval: request -> approve/reject/expire, có callback khi duyệt."""

    STATUSES = ("pending", "approved", "rejected", "expired")

    def __init__(
        self,
        db_path: str = ":memory:",
        audit_db: Optional[str] = None,
        audit_store: Any = None,
        required_approvals: int = 1,
    ) -> None:
        """db_path: file SQLite (":memory:" cho test).
        audit_db: đường dẫn file SQLite cho audit của repo (tự tạo AuditStore
            nếu import được module audit).
        audit_store: object có .log(event) — dùng cho test hoặc store tự chế.
        required_approvals: số approve tối thiểu mặc định cho mỗi request.
        """
        if not isinstance(required_approvals, int) or required_approvals < 1:
            raise ValueError("required_approvals must be an int >= 1")
        self._required_approvals = required_approvals
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._callbacks: Dict[str, List[Callable[[Dict[str, Any]], None]]] = {}
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

        # Ưu tiên audit_store được truyền vào; nếu không có thì thử tự dựng
        # từ module audit của repo khi có audit_db.
        self._audit_store = audit_store
        if self._audit_store is None and audit_db and _AUDIT_OK:
            try:
                self._audit_store = _AuditStore(audit_db)
            except Exception:  # noqa: BLE001 - audit lỗi thì bỏ qua
                self._audit_store = None

    # -- audit ----------------------------------------------------------
    def _audit(self, actor: str, action: str, target: str,
               details: Optional[Dict[str, Any]] = None,
               result: str = "ok") -> None:
        """Ghi 1 audit event. Không bao giờ raise (audit lỗi thì bỏ qua)."""
        store = self._audit_store
        if store is None:
            return
        try:
            payload = details or {}
            if _AuditEvent is not None:
                event = _AuditEvent(actor=actor, action=action, target=target,
                                    channel="", details=payload, result=result)
            else:
                # Duck-typing: AuditStore.log chỉ đọc các attribute này.
                event = SimpleNamespace(id="", ts="", actor=actor, action=action,
                                        target=target, channel="",
                                        details=payload, result=result)
            store.log(event)
        except Exception:  # noqa: BLE001 - audit không được làm crash flow
            pass

    # -- helpers nội bộ (gọi trong lock) --------------------------------
    def _row_to_dict(self, row: tuple) -> Dict[str, Any]:
        return {
            "request_id": row[0],
            "action": row[1],
            "requester": row[2],
            "details": json.loads(row[3] or "{}"),
            "approvers": json.loads(row[4] or "[]"),
            "required_approvals": row[5],
            "timeout_sec": row[6],
            "status": row[7],
            "created_at": row[8],
            "decided_at": row[9],
            "decided_by": row[10],
            "reason": row[11],
        }

    def _get_locked(self, request_id: str) -> Optional[Dict[str, Any]]:
        row = self._db.execute(
            "SELECT * FROM approvals WHERE request_id = ?", (request_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    @staticmethod
    def _is_expired(rec: Dict[str, Any], now: float) -> bool:
        return (now - rec["created_at"]) > rec["timeout_sec"]

    def _transition_locked(self, request_id: str, status: str,
                           decided_by: str, reason: str, now: float) -> None:
        self._db.execute(
            "UPDATE approvals SET status=?, decided_at=?, decided_by=?, reason=?"
            " WHERE request_id=?",
            (status, now, decided_by, reason or "", request_id))
        self._db.commit()

    @staticmethod
    def _clean_approvers(approvers: Any) -> List[str]:
        """Chuẩn hoá danh sách approver: bỏ trùng, bỏ rỗng, giữ thứ tự."""
        if not isinstance(approvers, (list, tuple)):
            return []
        seen = set()
        out = []
        for item in approvers:
            uid = str(item).strip() if item is not None else ""
            if uid and uid not in seen:
                seen.add(uid)
                out.append(uid)
        return out

    # -- API chính ------------------------------------------------------
    def request_approval(
        self,
        action: str,
        requester: str,
        details: Optional[Dict[str, Any]] = None,
        approvers: Optional[List[str]] = None,
        timeout_minutes: float = 30,
        required_approvals: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Tạo yêu cầu phê duyệt mới, status ban đầu "pending".

        Trả {"ok": True, "request_id", "status"} hoặc {"error": ...}.
        request_id dạng apr_<ms>_<hex>, không chứa ":" để tương thích
        callback_data của approval_telegram.py.
        """
        if not isinstance(action, str) or not action.strip():
            return {"error": "action must be a non-empty string"}
        if not isinstance(requester, str) or not requester.strip():
            return {"error": "requester must be a non-empty string"}
        clean_approvers = self._clean_approvers(approvers)
        if not clean_approvers:
            return {"error": "approvers must be a non-empty list of user ids"}
        try:
            timeout_sec = float(timeout_minutes) * 60.0
        except (TypeError, ValueError):
            return {"error": "timeout_minutes must be a number"}
        if timeout_sec <= 0:
            return {"error": "timeout_minutes must be > 0"}
        need = self._required_approvals if required_approvals is None else required_approvals
        if not isinstance(need, int) or isinstance(need, bool) or need < 1:
            return {"error": "required_approvals must be an int >= 1"}
        if need > len(clean_approvers):
            return {"error": f"required_approvals ({need}) > approvers ({len(clean_approvers)})"}
        details = details or {}
        if not isinstance(details, dict):
            return {"error": "details must be a dict"}
        try:
            details_json = json.dumps(details, ensure_ascii=False)
        except (TypeError, ValueError):
            return {"error": "details must be JSON-serializable"}

        # request_id không chứa ":" để parse callback Telegram không bị nhầm.
        request_id = f"apr_{int(time.time() * 1000)}_{secrets.token_hex(4)}"
        now = time.time()
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO approvals (request_id, action, requester,"
                    " details_json, approvers_json, required_approvals,"
                    " timeout_sec, status, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (request_id, action.strip(), requester.strip(), details_json,
                     json.dumps(clean_approvers, ensure_ascii=False),
                     need, timeout_sec, "pending", now),
                )
                self._db.commit()
            except sqlite3.IntegrityError:
                return {"error": f"request_id collision, please retry: {request_id}"}
        self._audit(requester.strip(), "approval.request", request_id,
                    {"action": action.strip(), "approvers": clean_approvers,
                     "required_approvals": need, "timeout_minutes": timeout_minutes})
        return {"ok": True, "request_id": request_id, "status": "pending"}

    def approve(self, request_id: str, approver: str) -> Dict[str, Any]:
        """1 approver trong danh sách duyệt. Đủ required_approvals -> "approved".

        Trả {"ok", "request_id", "status", "approvals", "required"} hoặc {"error"}.
        Khi chuyển sang "approved", các callback đã đăng ký qua on_approved()
        được gọi (ngoài lock, lỗi callback không làm sập flow).
        """
        now = time.time()
        audits: List[tuple] = []
        fire: List[Callable] = []
        with self._lock:
            rec = self._get_locked(request_id)
            if rec is None:
                result: Dict[str, Any] = {"error": f"unknown request_id: {request_id}"}
            elif rec["status"] == "pending" and self._is_expired(rec, now):
                self._transition_locked(request_id, "expired", "system", "timeout", now)
                audits.append(("system", "approval.expire", request_id,
                               {"action": rec["action"]}, "ok"))
                result = {"error": f"request expired: {request_id}", "status": "expired"}
            elif rec["status"] != "pending":
                result = {"error": f"already {rec['status']}: {request_id}",
                          "status": rec["status"]}
            elif approver not in rec["approvers"]:
                audits.append((str(approver), "approval.approve", request_id,
                               {"reason": "approver not in approvers list"}, "error"))
                result = {"error": f"approver not authorized: {approver}",
                          "status": "pending"}
            else:
                # Mỗi approver chỉ vote 1 lần (INSERT OR IGNORE theo PK).
                self._db.execute(
                    "INSERT OR IGNORE INTO approval_votes"
                    " (request_id, approver, decision, ts) VALUES (?,?,?,?)",
                    (request_id, approver, "approve", now))
                self._db.commit()
                voters = [r[0] for r in self._db.execute(
                    "SELECT approver FROM approval_votes"
                    " WHERE request_id=? AND decision='approve'", (request_id,))]
                n, need = len(voters), rec["required_approvals"]
                if n >= need:
                    decided_by = ",".join(sorted(voters))
                    self._transition_locked(request_id, "approved", decided_by, "", now)
                    audits.append((approver, "approval.approve", request_id,
                                   {"action": rec["action"], "approvals": n,
                                    "required": need}, "ok"))
                    fire = self._callbacks.pop(request_id, [])
                    result = {"ok": True, "request_id": request_id,
                              "status": "approved", "approvals": n, "required": need}
                else:
                    audits.append((approver, "approval.approve", request_id,
                                   {"action": rec["action"], "approvals": n,
                                    "required": need}, "ok"))
                    result = {"ok": True, "request_id": request_id,
                              "status": "pending", "approvals": n, "required": need}
        for entry in audits:
            self._audit(*entry)
        if result.get("status") == "approved":
            record = self.get_status(request_id)  # bản mới nhất sau commit
            for cb in fire:
                try:
                    cb(record)
                except Exception:  # noqa: BLE001 - callback lỗi không sập flow
                    self._audit("system", "approval.callback_error", request_id,
                                {"error": "on_approved callback raised"}, "error")
        return result

    def reject(self, request_id: str, approver: str, reason: str = "") -> Dict[str, Any]:
        """Approver trong danh sách từ chối -> status "rejected" ngay."""
        now = time.time()
        audits: List[tuple] = []
        with self._lock:
            rec = self._get_locked(request_id)
            if rec is None:
                result = {"error": f"unknown request_id: {request_id}"}
            elif rec["status"] == "pending" and self._is_expired(rec, now):
                self._transition_locked(request_id, "expired", "system", "timeout", now)
                audits.append(("system", "approval.expire", request_id,
                               {"action": rec["action"]}, "ok"))
                result = {"error": f"request expired: {request_id}", "status": "expired"}
            elif rec["status"] != "pending":
                result = {"error": f"already {rec['status']}: {request_id}",
                          "status": rec["status"]}
            elif approver not in rec["approvers"]:
                audits.append((str(approver), "approval.reject", request_id,
                               {"reason": "approver not in approvers list"}, "error"))
                result = {"error": f"approver not authorized: {approver}",
                          "status": "pending"}
            else:
                self._db.execute(
                    "INSERT OR IGNORE INTO approval_votes"
                    " (request_id, approver, decision, ts) VALUES (?,?,?,?)",
                    (request_id, approver, "reject", now))
                self._transition_locked(request_id, "rejected", approver, reason, now)
                audits.append((approver, "approval.reject", request_id,
                               {"action": rec["action"], "reason": reason}, "ok"))
                # Request bị từ chối: huỷ các callback chờ duyệt.
                self._callbacks.pop(request_id, None)
                result = {"ok": True, "request_id": request_id, "status": "rejected"}
        for entry in audits:
            self._audit(*entry)
        return result

    def get_status(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Trả record đầy đủ; tự đánh dấu "expired" nếu đã quá timeout.

        None khi request_id không tồn tại.
        """
        now = time.time()
        expired_audit = None
        with self._lock:
            rec = self._get_locked(request_id)
            if rec is None:
                return None
            if rec["status"] == "pending" and self._is_expired(rec, now):
                self._transition_locked(request_id, "expired", "system", "timeout", now)
                rec = self._get_locked(request_id)
                expired_audit = ("system", "approval.expire", request_id,
                                 {"action": rec["action"]}, "ok")
        if expired_audit:
            self._audit(*expired_audit)
        return rec

    def list_pending(self) -> List[Dict[str, Any]]:
        """Liệt kê các request đang pending (quét hết hạn trước)."""
        self.expire_old()
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM approvals WHERE status='pending' ORDER BY created_at"
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def expire_old(self, now: Optional[float] = None) -> int:
        """Quét request quá timeout -> "expired". Trả số request đã hết hạn.

        Gọi định kỳ từ cron/scheduler; get_status()/list_pending() cũng tự quét.
        """
        now = time.time() if now is None else now
        expired: List[Dict[str, Any]] = []
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM approvals WHERE status='pending'").fetchall()
            for row in rows:
                rec = self._row_to_dict(row)
                if self._is_expired(rec, now):
                    self._transition_locked(rec["request_id"], "expired",
                                            "system", "timeout", now)
                    expired.append(rec)
        for rec in expired:
            self._audit("system", "approval.expire", rec["request_id"],
                        {"action": rec["action"]}, "ok")
        return len(expired)

    def on_approved(self, request_id: str,
                    callback: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        """Đăng ký callback chạy khi request được duyệt (để nối vào execution).

        Nếu request đã "approved" thì callback chạy ngay. Callback nhận 1 arg
        là record đầy đủ từ get_status().
        """
        if not callable(callback):
            return {"error": "callback must be callable"}
        immediate = None
        with self._lock:
            rec = self._get_locked(request_id)
            if rec is None:
                return {"error": f"unknown request_id: {request_id}"}
            if rec["status"] == "approved":
                immediate = rec
            elif rec["status"] != "pending":
                return {"error": f"request already {rec['status']}: {request_id}"}
            else:
                self._callbacks.setdefault(request_id, []).append(callback)
        if immediate is not None:
            try:
                callback(immediate)
            except Exception:  # noqa: BLE001
                self._audit("system", "approval.callback_error", request_id,
                            {"error": "on_approved callback raised"}, "error")
        return {"ok": True, "request_id": request_id}

    # -- Telegram -------------------------------------------------------
    def format_telegram_message(self, request: Any) -> Dict[str, Any]:
        """Sinh text + inline keyboard gửi qua Telegram.

        request: request_id (str) hoặc record dict từ get_status().
        Trả {"text", "reply_markup"} — reply_markup tương thích 100% với
        gateway/approval_telegram.build_keyboard() (parse_approval_callback()
        bên đó parse được callback_data).
        """
        rec = self.get_status(request) if isinstance(request, str) else dict(request)
        if rec is None:
            return {"error": f"unknown request_id: {request}"}
        need, total = rec["required_approvals"], len(rec["approvers"])
        text = (
            "🔐 Yêu cầu phê duyệt\n\n"
            f"• Hành động: {rec['action']}\n"
            f"• Người yêu cầu: {rec['requester']}\n"
            f"• Người duyệt: {', '.join(rec['approvers'])} (cần {need}/{total})\n"
            f"• Hết hạn sau: {rec['timeout_sec'] / 60:g} phút\n"
            f"• Mã: {rec['request_id']}\n\n"
            f"Chi tiết:\n{_fmt_details(rec['details'])}\n\n"
            "Bấm nút bên dưới:"
        )
        return {"text": text, "reply_markup": _build_keyboard(rec["request_id"])}

    def handle_telegram_callback(self, update: Dict[str, Any]) -> Dict[str, Any]:
        """Điều hướng webhook callback_query Telegram vào flow này.

        update: dict webhook Telegram. Approver lấy từ callback_query.from.id.
        Trả {"ok", "request_id", "decision", "status"} hoặc {"error"}.
        """
        if not isinstance(update, dict):
            return {"error": "update must be a dict"}
        cq = update.get("callback_query")
        if not isinstance(cq, dict):
            return {"error": "no callback_query in update"}
        data = cq.get("data", "")
        if not isinstance(data, str) or not data:
            return {"error": "empty callback_data"}
        if data.startswith(APPROVE_PREFIX):
            request_id, decision = data[len(APPROVE_PREFIX):], "approve"
        elif data.startswith(DENY_PREFIX):
            request_id, decision = data[len(DENY_PREFIX):], "deny"
        else:
            return {"error": f"unknown callback data: {data[:32]}"}
        if not request_id:
            return {"error": "missing request_id in callback_data"}
        frm = cq.get("from") or {}
        approver = str(frm.get("id", "")).strip()
        if not approver:
            return {"error": "cannot identify approver from callback_query.from.id"}
        if decision == "approve":
            res = self.approve(request_id, approver)
        else:
            res = self.reject(request_id, approver, reason="rejected via telegram")
        if "error" in res:
            return res
        return {"ok": True, "request_id": request_id,
                "decision": decision, "status": res["status"]}

    def close(self) -> None:
        """Đóng kết nối SQLite."""
        with self._lock:
            self._db.close()
