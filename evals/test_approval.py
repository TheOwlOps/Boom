"""Test cho agent/src/business/approval.py — ApprovalFlow.

Chạy:  python3 test_approval.py
       python3 -m unittest test_approval -v
Stdlib only. Test timeout dùng timeout_minutes rất nhỏ + sleep thật.
"""

import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from approval import ApprovalFlow, APPROVE_PREFIX, DENY_PREFIX

# Thử nạp module approval_telegram của repo để kiểm tra tương thích
# callback_data thật (không bắt buộc: skip nếu không có).
_TG = None
try:
    sys.path.insert(0, "/tmp/boom_git/gateway")
    import approval_telegram as _TG
except Exception:  # noqa: BLE001
    _TG = None


class FakeAuditStore:
    """Audit store giả (duck-typing theo audit.store.AuditStore)."""

    def __init__(self):
        self.events = []

    def log(self, event):
        self.events.append({
            "actor": event.actor,
            "action": event.action,
            "target": event.target,
            "result": event.result,
            "details": dict(event.details or {}),
        })
        return "fake-id"

    def actions(self):
        return [e["action"] for e in self.events]


def make_flow(**kwargs):
    kwargs.setdefault("audit_store", FakeAuditStore())
    flow = ApprovalFlow(":memory:", **kwargs)
    return flow, flow._audit_store


class TestRequestApproval(unittest.TestCase):
    def test_request_ok_pending(self):
        flow, _ = make_flow()
        res = flow.request_approval("broadcast", "op_1",
                                    {"channel": "telegram", "text": "hello"},
                                    ["admin_1", "admin_2"])
        self.assertIn("ok", res)
        rid = res["request_id"]
        # Format apr_<ms>_<hex>, KHÔNG chứa ":" (tương thích callback Telegram).
        self.assertRegex(rid, r"^apr_\d+_[0-9a-f]{8}$")
        self.assertNotIn(":", rid)
        self.assertEqual(res["status"], "pending")
        st = flow.get_status(rid)
        self.assertEqual(st["status"], "pending")
        self.assertEqual(st["action"], "broadcast")
        self.assertEqual(st["requester"], "op_1")
        self.assertEqual(st["details"], {"channel": "telegram", "text": "hello"})
        self.assertEqual(st["approvers"], ["admin_1", "admin_2"])
        self.assertIsNone(st["decided_at"])
        flow.close()

    def test_request_validation(self):
        flow, _ = make_flow()
        self.assertIn("error", flow.request_approval("", "op", {}, ["a"]))
        self.assertIn("error", flow.request_approval("x", "", {}, ["a"]))
        self.assertIn("error", flow.request_approval("x", "op", {}, []))
        self.assertIn("error", flow.request_approval("x", "op", {}, None))
        self.assertIn("error", flow.request_approval("x", "op", {}, ["a"],
                                                     timeout_minutes=0))
        self.assertIn("error", flow.request_approval("x", "op", {}, ["a"],
                                                     timeout_minutes=-5))
        self.assertIn("error", flow.request_approval("x", "op", {}, ["a"],
                                                     required_approvals=2))
        self.assertIn("error", flow.request_approval("x", "op", "not-a-dict",
                                                     ["a"]))
        flow.close()

    def test_request_dedupes_approvers(self):
        flow, _ = make_flow()
        res = flow.request_approval("x", "op", {}, ["a", "b", "a", " "])
        st = flow.get_status(res["request_id"])
        self.assertEqual(st["approvers"], ["a", "b"])
        flow.close()


class TestApproveReject(unittest.TestCase):
    def test_full_approve_flow(self):
        flow, audit = make_flow()
        fired = []
        rid = flow.request_approval("broadcast", "op_1", {"text": "hi"},
                                    ["admin_1"])["request_id"]
        flow.on_approved(rid, lambda rec: fired.append(rec))
        res = flow.approve(rid, "admin_1")
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "approved")
        st = flow.get_status(rid)
        self.assertEqual(st["status"], "approved")
        self.assertEqual(st["decided_by"], "admin_1")
        self.assertIsNotNone(st["decided_at"])
        # Callback chạy đúng 1 lần với record đã approved.
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["request_id"], rid)
        self.assertEqual(fired[0]["status"], "approved")
        # Audit ghi đủ 2 event.
        self.assertEqual(audit.actions(), ["approval.request", "approval.approve"])
        flow.close()

    def test_approve_unauthorized(self):
        flow, audit = make_flow()
        rid = flow.request_approval("x", "op", {}, ["admin_1"])["request_id"]
        res = flow.approve(rid, "stranger")
        self.assertIn("error", res)
        self.assertEqual(flow.get_status(rid)["status"], "pending")
        # Audit vẫn ghi attempt bị từ chối (result=error).
        denied = [e for e in audit.events if e["result"] == "error"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["action"], "approval.approve")
        flow.close()

    def test_reject_flow(self):
        flow, audit = make_flow()
        rid = flow.request_approval("x", "op", {}, ["admin_1"])["request_id"]
        res = flow.reject(rid, "admin_1", reason="không phù hợp")
        self.assertTrue(res["ok"])
        st = flow.get_status(rid)
        self.assertEqual(st["status"], "rejected")
        self.assertEqual(st["decided_by"], "admin_1")
        self.assertEqual(st["reason"], "không phù hợp")
        self.assertEqual(audit.actions(), ["approval.request", "approval.reject"])
        flow.close()

    def test_reject_unauthorized(self):
        flow, _ = make_flow()
        rid = flow.request_approval("x", "op", {}, ["admin_1"])["request_id"]
        self.assertIn("error", flow.reject(rid, "stranger"))
        self.assertEqual(flow.get_status(rid)["status"], "pending")
        flow.close()

    def test_no_double_decision(self):
        flow, _ = make_flow()
        rid = flow.request_approval("x", "op", {}, ["admin_1"])["request_id"]
        flow.approve(rid, "admin_1")
        self.assertIn("error", flow.approve(rid, "admin_1"))
        self.assertIn("error", flow.reject(rid, "admin_1"))
        rid2 = flow.request_approval("x", "op", {}, ["admin_1"])["request_id"]
        flow.reject(rid2, "admin_1")
        self.assertIn("error", flow.approve(rid2, "admin_1"))
        flow.close()

    def test_multi_approvals_required(self):
        flow, _ = make_flow()
        rid = flow.request_approval("x", "op", {}, ["a1", "a2", "a3"],
                                    required_approvals=2)["request_id"]
        r1 = flow.approve(rid, "a1")
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["status"], "pending")  # chưa đủ
        self.assertEqual(r1["approvals"], 1)
        # Cùng người vote lại không được đếm 2 lần.
        r1b = flow.approve(rid, "a1")
        self.assertEqual(r1b["approvals"], 1)
        self.assertEqual(r1b["status"], "pending")
        r2 = flow.approve(rid, "a2")
        self.assertEqual(r2["status"], "approved")
        self.assertEqual(r2["approvals"], 2)
        st = flow.get_status(rid)
        self.assertIn("a1", st["decided_by"])
        self.assertIn("a2", st["decided_by"])
        flow.close()

    def test_approve_unknown_request(self):
        flow, _ = make_flow()
        self.assertIn("error", flow.approve("apr_nope", "admin_1"))
        self.assertIn("error", flow.reject("apr_nope", "admin_1"))
        self.assertIsNone(flow.get_status("apr_nope"))
        flow.close()


class TestCallbacks(unittest.TestCase):
    def test_on_approved_after_decision_fires_immediately(self):
        flow, _ = make_flow()
        rid = flow.request_approval("x", "op", {}, ["a"])["request_id"]
        flow.approve(rid, "a")
        fired = []
        res = flow.on_approved(rid, lambda rec: fired.append(rec))
        self.assertTrue(res["ok"])
        self.assertEqual(len(fired), 1)  # chạy ngay vì đã approved
        flow.close()

    def test_on_approved_rejected_request_errors(self):
        flow, _ = make_flow()
        rid = flow.request_approval("x", "op", {}, ["a"])["request_id"]
        flow.reject(rid, "a")
        self.assertIn("error", flow.on_approved(rid, lambda rec: None))
        self.assertIn("error", flow.on_approved("apr_nope", lambda rec: None))
        self.assertIn("error", flow.on_approved(rid, "not-callable"))
        flow.close()

    def test_callback_exception_does_not_break_flow(self):
        flow, audit = make_flow()
        rid = flow.request_approval("x", "op", {}, ["a"])["request_id"]

        def bad_cb(rec):
            raise RuntimeError("boom")

        flow.on_approved(rid, bad_cb)
        res = flow.approve(rid, "a")  # không được raise
        self.assertEqual(res["status"], "approved")
        self.assertIn("approval.callback_error", audit.actions())
        flow.close()


class TestExpiry(unittest.TestCase):
    def test_expire_via_get_status(self):
        flow, audit = make_flow()
        rid = flow.request_approval("x", "op", {}, ["a"],
                                    timeout_minutes=0.02)["request_id"]  # ~1.2s
        time.sleep(1.6)
        st = flow.get_status(rid)  # tự đánh dấu expired
        self.assertEqual(st["status"], "expired")
        self.assertEqual(st["decided_by"], "system")
        self.assertEqual(st["reason"], "timeout")
        self.assertIn("approval.expire", audit.actions())
        # Đã expired thì không approve được nữa.
        self.assertIn("error", flow.approve(rid, "a"))
        flow.close()

    def test_expire_old_sweep(self):
        flow, _ = make_flow()
        short = flow.request_approval("x", "op", {}, ["a"],
                                      timeout_minutes=0.02)["request_id"]
        long = flow.request_approval("y", "op", {}, ["a"],
                                     timeout_minutes=60)["request_id"]
        time.sleep(1.6)
        n = flow.expire_old()
        self.assertEqual(n, 1)
        self.assertEqual(flow.get_status(short)["status"], "expired")
        self.assertEqual(flow.get_status(long)["status"], "pending")
        # Quét lại không đếm trùng.
        self.assertEqual(flow.expire_old(), 0)
        flow.close()

    def test_list_pending_only_pending(self):
        flow, _ = make_flow()
        r1 = flow.request_approval("a", "op", {}, ["x"])["request_id"]
        r2 = flow.request_approval("b", "op", {}, ["x"])["request_id"]
        r3 = flow.request_approval("c", "op", {}, ["x"],
                                   timeout_minutes=0.02)["request_id"]
        flow.approve(r1, "x")
        time.sleep(1.6)
        pending = flow.list_pending()
        ids = [p["request_id"] for p in pending]
        self.assertEqual(ids, [r2])  # r1 approved, r3 expired
        flow.close()


class TestTelegram(unittest.TestCase):
    def test_format_telegram_message(self):
        flow, _ = make_flow()
        rid = flow.request_approval("broadcast", "op_1",
                                    {"channel": "telegram", "text": "Sale 50%"},
                                    ["admin_1", "admin_2"])["request_id"]
        msg = flow.format_telegram_message(rid)
        self.assertIn("text", msg)
        self.assertIn("reply_markup", msg)
        self.assertIn("broadcast", msg["text"])
        self.assertIn("op_1", msg["text"])
        self.assertIn(rid, msg["text"])
        kb = msg["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(kb), 1)
        self.assertEqual(len(kb[0]), 2)
        btn_ok, btn_no = kb[0]
        self.assertEqual(btn_ok["callback_data"], f"{APPROVE_PREFIX}{rid}")
        self.assertEqual(btn_no["callback_data"], f"{DENY_PREFIX}{rid}")
        self.assertIn("Đồng ý", btn_ok["text"])
        # Chấp nhận cả record dict thay vì request_id.
        msg2 = flow.format_telegram_message(flow.get_status(rid))
        self.assertEqual(msg2["reply_markup"], msg["reply_markup"])
        # request_id lạ -> error.
        self.assertIn("error", flow.format_telegram_message("apr_nope"))
        flow.close()

    def test_keyboard_compatible_with_approval_telegram(self):
        # Verify round-trip với module approval_telegram.py của repo:
        # parse_approval_callback() phải parse được callback_data do
        # format_telegram_message() sinh ra.
        if _TG is None:
            self.skipTest("approval_telegram.py của repo không import được")
        flow, _ = make_flow()
        rid = flow.request_approval("x", "op", {}, ["a"])["request_id"]
        msg = flow.format_telegram_message(rid)
        kb = msg["reply_markup"]["inline_keyboard"][0]
        parsed_ok = _TG.parse_approval_callback(kb[0]["callback_data"])
        parsed_no = _TG.parse_approval_callback(kb[1]["callback_data"])
        self.assertEqual(parsed_ok, {"decision": "approve", "request_id": rid})
        self.assertEqual(parsed_no, {"decision": "deny", "request_id": rid})
        # Và keyboard do module này sinh phải deep-equal với build_keyboard gốc.
        self.assertEqual(msg["reply_markup"], _TG.build_keyboard(rid))
        flow.close()

    def test_handle_telegram_callback_approve(self):
        flow, _ = make_flow()
        rid = flow.request_approval("x", "op", {}, ["12345"])["request_id"]
        update = {"callback_query": {"data": f"approve:{rid}",
                                     "from": {"id": 12345}}}
        res = flow.handle_telegram_callback(update)
        self.assertTrue(res["ok"])
        self.assertEqual(res["decision"], "approve")
        self.assertEqual(res["status"], "approved")
        self.assertEqual(flow.get_status(rid)["decided_by"], "12345")
        flow.close()

    def test_handle_telegram_callback_deny(self):
        flow, _ = make_flow()
        rid = flow.request_approval("x", "op", {}, ["999"])["request_id"]
        update = {"callback_query": {"data": f"deny:{rid}",
                                     "from": {"id": 999}}}
        res = flow.handle_telegram_callback(update)
        self.assertTrue(res["ok"])
        self.assertEqual(res["decision"], "deny")
        self.assertEqual(res["status"], "rejected")
        flow.close()

    def test_handle_telegram_callback_errors(self):
        flow, _ = make_flow()
        rid = flow.request_approval("x", "op", {}, ["1"])["request_id"]
        # Người bấm nút không nằm trong approvers.
        bad_user = {"callback_query": {"data": f"approve:{rid}",
                                       "from": {"id": 777}}}
        self.assertIn("error", flow.handle_telegram_callback(bad_user))
        # data lạ / thiếu from / thiếu callback_query.
        self.assertIn("error", flow.handle_telegram_callback(
            {"callback_query": {"data": "weird", "from": {"id": 1}}}))
        self.assertIn("error", flow.handle_telegram_callback(
            {"callback_query": {"data": f"approve:{rid}"}}))
        self.assertIn("error", flow.handle_telegram_callback({"x": 1}))
        self.assertIn("error", flow.handle_telegram_callback("not-a-dict"))
        # request_id không tồn tại.
        self.assertIn("error", flow.handle_telegram_callback(
            {"callback_query": {"data": "approve:apr_nope",
                                "from": {"id": 1}}}))
        flow.close()


class TestAuditOptional(unittest.TestCase):
    def test_no_audit_store_no_crash(self):
        # Không truyền audit_store, không có module audit -> vẫn chạy full flow.
        flow = ApprovalFlow(":memory:")
        rid = flow.request_approval("x", "op", {}, ["a"])["request_id"]
        self.assertEqual(flow.approve(rid, "a")["status"], "approved")
        rid2 = flow.request_approval("y", "op", {}, ["a"],
                                     timeout_minutes=0.02)["request_id"]
        time.sleep(1.6)
        self.assertEqual(flow.get_status(rid2)["status"], "expired")
        flow.close()

    def test_audit_events_content(self):
        flow, audit = make_flow()
        rid = flow.request_approval("deploy", "op_9", {"env": "prod"},
                                    ["boss"])["request_id"]
        flow.reject(rid, "boss", reason="đợi thêm test")
        req_ev = audit.events[0]
        self.assertEqual(req_ev["actor"], "op_9")
        self.assertEqual(req_ev["action"], "approval.request")
        self.assertEqual(req_ev["target"], rid)
        rej_ev = audit.events[1]
        self.assertEqual(rej_ev["actor"], "boss")
        self.assertEqual(rej_ev["action"], "approval.reject")
        self.assertEqual(rej_ev["details"]["reason"], "đợi thêm test")
        flow.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
