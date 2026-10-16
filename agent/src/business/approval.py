"""Multi-step approval flow for Boom (business).

Standard flow: operator sends a request -> admin approves -> callback executes.

    flow = ApprovalFlow("approvals.db")
    res = flow.request_approval(
        "broadcast",
        requester="op_1",
        details={"channel": "telegram", "text": "October promo"},
        approvers=["admin_1", "admin_2"],
        timeout_minutes=30,
    )
    rid = res["request_id"]
    flow.on_approved(rid, lambda req: execute_broadcast(req["details"]))
    flow.approve(rid, "admin_1")   # -> "approved", callback runs

Telegram integration: format_telegram_message() produces text + an inline
keyboard with 2 buttons, using the same callback_data "approve:<request_id>" /
"deny:<request_id>" scheme as gateway/approval_telegram.py (same prefix), so
its parse_approval_callback() can parse it directly. handle_telegram_callback()
takes a webhook update and routes it into this flow's approve()/reject().

Repo pattern: SQLite + threading.Lock, stdlib only. Every state change writes
1 audit event when the repo's audit module is importable (try/except —
missing audit runs fine, never crashes because of audit).
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
# Repo audit integration (optional).
# When this file sits inside the Boom repo (agent/src on sys.path), audit.store
# / audit.models import fine and audit is recorded automatically. Standalone
# runs skip it.
# ---------------------------------------------------------------------------
try:
    from audit.models import AuditEvent as _AuditEvent  # type: ignore
    from audit.store import AuditStore as _AuditStore  # type: ignore
    _AUDIT_OK = True
except Exception:  # noqa: BLE001 - missing audit is not an error
    _AuditEvent = None
    _AuditStore = None
    _AUDIT_OK = False

# ---------------------------------------------------------------------------
# Telegram: keep the same scheme as gateway/approval_telegram.py
# (APPROVE_PREFIX / DENY_PREFIX identical over there).
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
-- Each approver votes only once per request (no double counting when
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
    """Inline keyboard with 2 approve/reject buttons.

    Mirrors gateway/approval_telegram.build_keyboard() for compatibility.
    """
    return {"inline_keyboard": [[
        {"text": "✅ Approve",
         "callback_data": f"{APPROVE_PREFIX}{request_id}"},
        {"text": "❌ Reject",
         "callback_data": f"{DENY_PREFIX}{request_id}"},
    ]]}


def _fmt_details(details: Dict[str, Any]) -> str:
    """Render a details dict into readable multi-line Telegram text."""
    if not details:
        return "(no details)"
    lines = []
    for key, val in details.items():
        text = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        if len(text) > 300:
            text = text[:297] + "..."
        lines.append(f"• {key}: {text}")
    return "\n".join(lines)


class ApprovalFlow:
    """Multi-step approval: request -> approve/reject/expire, with an on-approve callback."""

    STATUSES = ("pending", "approved", "rejected", "expired")

    def __init__(
        self,
        db_path: str = ":memory:",
        audit_db: Optional[str] = None,
        audit_store: Any = None,
        required_approvals: int = 1,
    ) -> None:
        """db_path: SQLite file (":memory:" for tests).
        audit_db: SQLite file path for the repo's audit (builds an AuditStore
            when the repo's audit module imports fine).
        audit_store: object with .log(event) — for tests or custom stores.
        required_approvals: default minimum approvals per request.
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

        # Prefer a passed-in audit_store; otherwise try building one from the
        # repo's audit module when audit_db is given.
        self._audit_store = audit_store
        if self._audit_store is None and audit_db and _AUDIT_OK:
            try:
                self._audit_store = _AuditStore(audit_db)
            except Exception:  # noqa: BLE001 - audit failure is ignored
                self._audit_store = None

    # -- audit ----------------------------------------------------------
    def _audit(self, actor: str, action: str, target: str,
               details: Optional[Dict[str, Any]] = None,
               result: str = "ok") -> None:
        """Write 1 audit event. Never raises (audit errors are ignored)."""
        store = self._audit_store
        if store is None:
            return
        try:
            payload = details or {}
            if _AuditEvent is not None:
                event = _AuditEvent(actor=actor, action=action, target=target,
                                    channel="", details=payload, result=result)
            else:
                # Duck-typing: AuditStore.log only reads these attributes.
                event = SimpleNamespace(id="", ts="", actor=actor, action=action,
                                        target=target, channel="",
                                        details=payload, result=result)
            store.log(event)
        except Exception:  # noqa: BLE001 - audit must never crash the flow
            pass

    # -- internal helpers (called under lock) ---------------------------
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
        """Normalize the approver list: dedupe, drop empties, keep order."""
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

    # -- main API -------------------------------------------------------
    def request_approval(
        self,
        action: str,
        requester: str,
        details: Optional[Dict[str, Any]] = None,
        approvers: Optional[List[str]] = None,
        timeout_minutes: float = 30,
        required_approvals: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create a new approval request, initial status "pending".

        Returns {"ok": True, "request_id", "status"} or {"error": ...}.
        request_id looks like apr_<ms>_<hex> and contains no ":" so
        approval_telegram.py's callback_data parsing never confuses it.
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

        # request_id contains no ":" so Telegram callback parsing stays safe.
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
        """One approver in the list approves. Enough approvals -> "approved".

        Returns {"ok", "request_id", "status", "approvals", "required"} or {"error"}.
        On transition to "approved", callbacks registered via on_approved()
        fire (outside the lock; a failing callback can't crash the flow).
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
                # Each approver votes once (INSERT OR IGNORE on the PK).
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
            record = self.get_status(request_id)  # freshest version post-commit
            for cb in fire:
                try:
                    cb(record)
                except Exception:  # noqa: BLE001 - a callback error can't crash the flow
                    self._audit("system", "approval.callback_error", request_id,
                                {"error": "on_approved callback raised"}, "error")
        return result

    def reject(self, request_id: str, approver: str, reason: str = "") -> Dict[str, Any]:
        """An approver in the list rejects -> status "rejected" immediately."""
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
                # Rejected request: drop pending-approval callbacks.
                self._callbacks.pop(request_id, None)
                result = {"ok": True, "request_id": request_id, "status": "rejected"}
        for entry in audits:
            self._audit(*entry)
        return result

    def get_status(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Returns the full record; auto-marks "expired" past the timeout.

        None when the request_id doesn't exist.
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
        """Lists pending requests (sweeps expired ones first)."""
        self.expire_old()
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM approvals WHERE status='pending' ORDER BY created_at"
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def expire_old(self, now: Optional[float] = None) -> int:
        """Sweeps timed-out requests -> "expired". Returns the number expired.

        Called periodically from cron/scheduler; get_status()/list_pending()
        sweep automatically too.
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
        """Registers a callback to run when the request is approved (chains into execution).

        If the request is already "approved", the callback runs immediately.
        The callback receives 1 arg: the full record from get_status().
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
        """Builds the text + inline keyboard for sending over Telegram.

        request: a request_id (str) or a record dict from get_status().
        Returns {"text", "reply_markup"} — reply_markup is 100% compatible with
        gateway/approval_telegram.build_keyboard() (its parse_approval_callback()
        parses the callback_data).
        """
        rec = self.get_status(request) if isinstance(request, str) else dict(request)
        if rec is None:
            return {"error": f"unknown request_id: {request}"}
        need, total = rec["required_approvals"], len(rec["approvers"])
        text = (
            "🔐 Approval request\n\n"
            f"• Action: {rec['action']}\n"
            f"• Requester: {rec['requester']}\n"
            f"• Approvers: {', '.join(rec['approvers'])} (needs {need}/{total})\n"
            f"• Expires in: {rec['timeout_sec'] / 60:g} minutes\n"
            f"• Code: {rec['request_id']}\n\n"
            f"Details:\n{_fmt_details(rec['details'])}\n\n"
            "Tap a button below:"
        )
        return {"text": text, "reply_markup": _build_keyboard(rec["request_id"])}

    def handle_telegram_callback(self, update: Dict[str, Any]) -> Dict[str, Any]:
        """Routes a Telegram webhook callback_query into this flow.

        update: a Telegram webhook dict. The approver comes from
        callback_query.from.id.
        Returns {"ok", "request_id", "decision", "status"} or {"error"}.
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
        """Closes the SQLite connection."""
        with self._lock:
            self._db.close()
