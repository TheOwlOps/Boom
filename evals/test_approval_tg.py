"""Test offline cho gateway/approval_telegram.py — khong can token that."""
import asyncio
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/boom-mods/upgrade1")

from gateway.approval_telegram import (  # noqa: E402
    ApprovalStore,
    build_approval_text,
    build_keyboard,
    handle_callback,
    is_expired,
    parse_approval_callback,
    send_approval_request,
)

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL: {name}")


class FakeAdapterMarkup:
    """Adapter co method send_message_markup chuyen dung."""

    def __init__(self):
        self.calls = []

    async def send_message_markup(self, target, text, reply_markup):
        self.calls.append({"target": target, "text": text,
                           "reply_markup": reply_markup})
        return {"ok": True, "message_id": 777}


class FakeResp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, body):
        self.body = body
        self.posts = []

    async def post(self, url, json=None):
        self.posts.append({"url": url, "json": json})
        return FakeResp(self.body)


class FakeTelegramAdapter:
    """Gia lap TelegramAdapter: co _api()/_http()."""

    def __init__(self, body):
        self._client = FakeClient(body)

    def _api(self):
        return "https://api.telegram.org/botTOKEN"

    def _http(self):
        return self._client


class FakeNoMarkup:
    pass


# --- 1. build_keyboard ---
kb = build_keyboard("req123")
rows = kb["inline_keyboard"]
check("keyboard 1 row 2 buttons", len(rows) == 1 and len(rows[0]) == 2)
btns = rows[0]
check("approve callback_data", btns[0]["callback_data"] == "approve:req123")
check("deny callback_data", btns[1]["callback_data"] == "deny:req123")
check("approve label", "Đồng ý" in btns[0]["text"])
check("deny label", "Từ chối" in btns[1]["text"])
check("approval text has desc", "xoa file" in build_approval_text("xoa file"))

# --- 2. send_approval_request via send_message_markup ---
fa = FakeAdapterMarkup()
r = asyncio.run(send_approval_request(fa, "5337", "gui mail cho sep", "req123"))
check("send ok", r.get("ok") is True)
check("send message_id", r.get("message_id") == "777")
check("one call", len(fa.calls) == 1)
call = fa.calls[0]
check("target passed", call["target"] == "5337")
check("text has desc", "gui mail cho sep" in call["text"])
rm = call["reply_markup"]
check("reply_markup inline_keyboard",
      "inline_keyboard" in rm and len(rm["inline_keyboard"][0]) == 2)
check("reply_markup approve data",
      rm["inline_keyboard"][0][0]["callback_data"] == "approve:req123")

# --- 3. send_approval_request via _api/_http fallback ---
fta = FakeTelegramAdapter({"ok": True, "result": {"message_id": 42}})
r = asyncio.run(send_approval_request(fta, "5337", "push code", "reqABC"))
check("fallback ok", r.get("ok") is True)
check("fallback message_id", r.get("message_id") == "42")
post = fta._client.posts[0]
check("fallback url sendMessage", post["url"].endswith("/sendMessage"))
check("fallback has reply_markup", "reply_markup" in post["json"])
check("fallback chat_id", post["json"]["chat_id"] == "5337")

# --- 4. send failures ---
fta_bad = FakeTelegramAdapter({"ok": False, "description": "Bad Request"})
r = asyncio.run(send_approval_request(fta_bad, "1", "x", "r1"))
check("fallback api error", r.get("ok") is False and "error" in r)
r = asyncio.run(send_approval_request(FakeNoMarkup(), "1", "x", "r1"))
check("no markup support -> error", r.get("ok") is False and "error" in r)
r = asyncio.run(send_approval_request(fa, "1", "x", "bad:id"))
check("request_id with colon rejected", r.get("ok") is False)
r = asyncio.run(send_approval_request(fa, "1", "x", ""))
check("empty request_id rejected", r.get("ok") is False)


class BoomAdapter:
    async def send_message_markup(self, target, text, reply_markup):
        raise RuntimeError("network down")


r = asyncio.run(send_approval_request(BoomAdapter(), "1", "x", "r9"))
check("exception -> honest error", r.get("ok") is False and "network down" in r["error"])

# --- 5. parse_approval_callback ---
p = parse_approval_callback("approve:req123")
check("parse approve", p == {"decision": "approve", "request_id": "req123"})
p = parse_approval_callback("deny:reqXYZ")
check("parse deny", p == {"decision": "deny", "request_id": "reqXYZ"})
check("parse unknown prefix", "error" in parse_approval_callback("maybe:req1"))
check("parse empty", "error" in parse_approval_callback(""))
check("parse non-str", "error" in parse_approval_callback(None))
check("parse approve no id", "error" in parse_approval_callback("approve:"))
# request_id co the chua dau khac, chi tach prefix 1 lan
p = parse_approval_callback("approve:a-b_c.9")
check("parse weird id", p["request_id"] == "a-b_c.9")

# --- 6. ApprovalStore ---
store = ApprovalStore()
r = store.create("req1", "5337", "gui mail", message_id="10")
check("store create", r.get("ok") is True)
rec = store.get("req1")
check("store get", rec is not None and rec["status"] == "pending"
      and rec["chat_id"] == "5337" and rec["message_id"] == "10")
check("store get missing", store.get("nope") is None)
r = store.create("req1", "5337", "dup", message_id="11")
check("store duplicate rejected", "error" in r)
r = store.create("", "5337", "empty")
check("store empty id rejected", "error" in r)
r = store.resolve("req1", "approved")
check("store resolve approved", r.get("ok") is True and r["status"] == "approved")
check("store status updated", store.get("req1")["status"] == "approved")
r = store.resolve("req1", "denied")
check("store double resolve rejected", "error" in r)
r = store.resolve("missing", "approved")
check("store resolve missing", "error" in r)
r = store.resolve("req1", "maybe")
check("store bad decision", "error" in r)
store.create("req2", "5337", "xoa file")
store.resolve("req2", "denied")
check("store denied", store.get("req2")["status"] == "denied")

# --- 7. is_expired ---
check("not expired fresh", is_expired(1000.0, now=1000.0) is False)
check("expired old", is_expired(1000.0 - 601, now=1000.0) is True)
check("boundary 600 not expired", is_expired(1000.0 - 600, now=1000.0) is False)
check("custom ttl", is_expired(1000.0 - 61, ttl_sec=60, now=1000.0) is True)

# --- 8. handle_callback ---
store2 = ApprovalStore()
store2.create("cb1", "5337", "push code len main", message_id="20")
update = {"update_id": 1,
          "callback_query": {"id": "cq1", "from": {"id": 5337},
                             "message": {"message_id": 20, "chat": {"id": 5337}},
                             "data": "approve:cb1"}}
r = handle_callback(update, store2)
check("handle approve", r == {"request_id": "cb1", "decision": "approve"})
check("store approved after callback", store2.get("cb1")["status"] == "approved")

store2.create("cb2", "5337", "xoa db", message_id="21")
update2 = {"update_id": 2,
           "callback_query": {"id": "cq2", "from": {"id": 5337},
                              "message": {"message_id": 21},
                              "data": "deny:cb2"}}
r = handle_callback(update2, store2)
check("handle deny", r == {"request_id": "cb2", "decision": "deny"})
check("store denied after callback", store2.get("cb2")["status"] == "denied")

check("handle no callback_query", "error" in handle_callback({"update_id": 3}, store2))
check("handle bad data", "error" in handle_callback(
    {"callback_query": {"data": "hello"}}, store2))
check("handle unknown rid", "error" in handle_callback(
    {"callback_query": {"data": "approve:ghost"}}, store2))
check("handle non-dict", "error" in handle_callback("nope", store2))

# double click -> already resolved
r = handle_callback(update, store2)
check("handle double click rejected", "error" in r and "already resolved" in r["error"])

# expired -> auto deny + error
store2.create("cb3", "5337", "viec cu", created_at=time.time() - 700)
r = handle_callback({"callback_query": {"data": "approve:cb3"}}, store2)
check("handle expired", "error" in r and "expired" in r["error"])
check("expired auto denied", store2.get("cb3")["status"] == "denied")

store.close()
store2.close()

print(f"\nPASS: {PASS}  FAIL: {FAIL}")
sys.exit(1 if FAIL else 0)
