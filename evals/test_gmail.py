"""Tests for agent/src/connectors/gmail.py — no network, no real credentials.

httpx.MockTransport serves realistic Gmail REST API fixtures; every assertion
checks real parsing behavior (base64url bodies, header extraction, send payload).
"""

import base64
import json
import os
import sys
from pathlib import Path

# Import root is agent/src (repo convention).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

import httpx

from connectors.gmail import CONNECTOR, GmailConnector, build_tools


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit("FAILED: " + name)


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


TOKEN = "test-token-123"
MSG_1 = "18d2f3a4b5c6d7e8"
MSG_2 = "18d2f3a4b5c6d7e9"
THREAD_1 = "18d2f3a4b5c6d7e8"

PLAIN_BODY = "Chào anh,\n\nĐây là báo cáo tuần 42.\n\nTrân trọng,\nA"
HTML_BODY = (
    "<html><body><p>Chào <b>anh</b>,</p>"
    "<p>Đây là <i>báo cáo</i> tuần 42.</p>"
    "<script>evil()</script></body></html>"
)
HTML_ONLY_BODY = "<div>Hello <b>world</b> &amp; mọi người</div>"

HEADERS_1 = [
    {"name": "From", "value": "Nguyễn Văn A <nguyenvana@example.com>"},
    {"name": "To", "value": "son@example.com"},
    {"name": "Subject", "value": "Báo cáo tuần 42"},
    {"name": "Date", "value": "Tue, 14 Oct 2026 09:15:00 +0700"},
]

LIST_FIXTURE = {
    "messages": [
        {"id": MSG_1, "threadId": THREAD_1},
        {"id": MSG_2, "threadId": THREAD_1},
    ],
    "nextPageToken": "CAEQ",
    "resultSizeEstimate": 2,
}

META_FIXTURE = {
    MSG_1: {
        "id": MSG_1, "threadId": THREAD_1, "labelIds": ["INBOX", "UNREAD"],
        "snippet": "Chào anh, đây là báo cáo tuần 42...",
        "payload": {
            "partId": "", "mimeType": "multipart/alternative",
            "headers": HEADERS_1, "body": {"size": 0}, "parts": [],
        },
    },
    MSG_2: {
        "id": MSG_2, "threadId": THREAD_1, "labelIds": ["INBOX"],
        "snippet": "Hello world snippet",
        "payload": {
            "partId": "", "mimeType": "text/html",
            "headers": [
                {"name": "From", "value": "b@example.com"},
                {"name": "Subject", "value": "Hello"},
                {"name": "Date", "value": "Wed, 15 Oct 2026 08:00:00 +0700"},
            ],
            "body": {"size": 0}, "parts": [],
        },
    },
}

FULL_FIXTURE = {
    MSG_1: {
        "id": MSG_1, "threadId": THREAD_1, "labelIds": ["INBOX"],
        "snippet": "Chào anh, đây là báo cáo tuần 42...",
        "payload": {
            "partId": "", "mimeType": "multipart/alternative",
            "headers": HEADERS_1, "body": {"size": 0},
            "parts": [
                {"partId": "0", "mimeType": "text/plain",
                 "headers": [{"name": "Content-Type", "value": "text/plain; charset=UTF-8"}],
                 "body": {"size": len(PLAIN_BODY), "data": b64url(PLAIN_BODY)}},
                {"partId": "1", "mimeType": "text/html",
                 "headers": [{"name": "Content-Type", "value": "text/html; charset=UTF-8"}],
                 "body": {"size": len(HTML_BODY), "data": b64url(HTML_BODY)}},
            ],
        },
    },
    MSG_2: {
        "id": MSG_2, "threadId": THREAD_1, "labelIds": ["INBOX"],
        "snippet": "Hello world snippet",
        "payload": {
            "partId": "", "mimeType": "text/html",
            "headers": [
                {"name": "From", "value": "b@example.com"},
                {"name": "Subject", "value": "Hello"},
                {"name": "Date", "value": "Wed, 15 Oct 2026 08:00:00 +0700"},
            ],
            "body": {"size": 42, "data": b64url(HTML_ONLY_BODY)},
        },
    },
}

SEND_FIXTURE = {
    "id": "18e0a1b2c3d4e5f6",
    "threadId": "18e0a1b2c3d4e5f6",
    "labelIds": ["UNREAD", "SENT", "INBOX"],
}

captured_sends = []


def make_handler(list_fixture=None):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "gmail.googleapis.com", request.url.host
        assert request.headers.get("authorization") == f"Bearer {TOKEN}", \
            f"bad auth header: {request.headers.get('authorization')}"
        path = request.url.path
        if path == "/gmail/v1/users/me/messages" and request.method == "GET":
            assert request.url.params.get("q") == "is:unread"
            assert request.url.params.get("maxResults") == "10"
            return httpx.Response(200, json=list_fixture if list_fixture is not None else LIST_FIXTURE)
        if path.startswith("/gmail/v1/users/me/messages/") and request.method == "GET":
            mid = path.rsplit("/", 1)[-1]
            fmt = request.url.params.get("format")
            if fmt == "metadata":
                return httpx.Response(200, json=META_FIXTURE[mid])
            return httpx.Response(200, json=FULL_FIXTURE[mid])
        if path == "/gmail/v1/users/me/messages/send" and request.method == "POST":
            captured_sends.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json=SEND_FIXTURE)
        return httpx.Response(404, json={"error": {"code": 404, "message": "not found"}})
    return handler


def make_connector(handler=None):
    transport = httpx.MockTransport(handler or make_handler())
    return GmailConnector(token=TOKEN, client=httpx.Client(transport=transport))


# ------------------------------------------------------------------ tests


def test_status_with_token():
    c = GmailConnector(token=TOKEN)
    st = c.status()
    check("status connected with token", st["connected"] is True)
    check("status detail mentions env", "BOOM_GMAIL_ACCESS_TOKEN" in st["detail"])


def test_status_without_token():
    saved = os.environ.pop("BOOM_GMAIL_ACCESS_TOKEN", None)
    try:
        c = GmailConnector()  # reads env lazily
        st = c.status()
        check("status disconnected without token", st["connected"] is False)
        check("status detail honest", "not set" in st["detail"])
    finally:
        if saved is not None:
            os.environ["BOOM_GMAIL_ACCESS_TOKEN"] = saved


def test_tools_error_without_token():
    saved = os.environ.pop("BOOM_GMAIL_ACCESS_TOKEN", None)
    try:
        c = GmailConnector()
        for name, kwargs in (("gmail_search", {"query": "x"}),
                             ("gmail_read", {"message_id": "abc"}),
                             ("gmail_send", {"to": "a@b.c", "subject": "s", "body": "b"})):
            res = getattr(c, name)(**kwargs)
            check(f"{name} errors without token",
                  isinstance(res, dict) and "BOOM_GMAIL_ACCESS_TOKEN" in res.get("error", ""))
    finally:
        if saved is not None:
            os.environ["BOOM_GMAIL_ACCESS_TOKEN"] = saved


def test_search_parses_list_and_metadata():
    c = make_connector()
    res = c.gmail_search("is:unread")
    check("search ok", "error" not in res)
    check("two hits", len(res["messages"]) == 2)
    first = res["messages"][0]
    check("hit id", first["id"] == MSG_1)
    check("hit snippet", first["snippet"].startswith("Chào anh"))
    check("hit from", first["from"] == "Nguyễn Văn A <nguyenvana@example.com>")
    check("hit subject", first["subject"] == "Báo cáo tuần 42")
    check("hit date", "14 Oct 2026" in first["date"])
    check("size estimate", res["resultSizeEstimate"] == 2)


def test_search_empty_result():
    c = make_connector(make_handler(list_fixture={"resultSizeEstimate": 0}))
    res = c.gmail_search("is:unread")
    check("empty search ok", res["messages"] == [] and res["resultSizeEstimate"] == 0)


def test_search_metadata_failure_kept_honest():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return httpx.Response(200, json={
                "messages": [{"id": MSG_1, "threadId": THREAD_1}],
                "resultSizeEstimate": 1})
        return httpx.Response(500, json={"error": {"code": 500, "message": "Backend Error"}})
    c = GmailConnector(token=TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler)))
    res = c.gmail_search("is:unread")
    check("failure kept with id", res["messages"][0]["id"] == MSG_1)
    check("failure noted honestly", "500" in res["messages"][0].get("error", ""))


def test_search_validation():
    c = make_connector()
    check("empty query errors", "error" in c.gmail_search("  "))
    check("bad max_results errors", "error" in c.gmail_search("x", max_results="many"))


def test_read_multipart_prefers_plain():
    c = make_connector()
    res = c.gmail_read(MSG_1)
    check("read ok", "error" not in res)
    check("from", res["from"] == "Nguyễn Văn A <nguyenvana@example.com>")
    check("subject", res["subject"] == "Báo cáo tuần 42")
    check("date", "14 Oct 2026" in res["date"])
    check("body is plain text", res["body"] == PLAIN_BODY)
    check("body_mime plain", res["body_mime"] == "text/plain")
    check("labels", res["labelIds"] == ["INBOX"])


def test_read_html_only_falls_back_to_stripped_text():
    c = make_connector()
    res = c.gmail_read(MSG_2)
    check("html fallback ok", "error" not in res)
    check("html stripped", res["body"] == "Hello world & mọi người")
    check("body_mime html", res["body_mime"] == "text/html")
    check("script dropped", "evil" not in res["body"])


def test_read_validation():
    c = make_connector()
    check("empty id errors", "error" in c.gmail_read("  "))


def test_send_builds_rfc2822_and_confirms():
    captured_sends.clear()
    c = make_connector()
    res = c.gmail_send("ban@example.com", "Xin chào", "Nội dung thư\ndòng 2.")
    check("send ok", "error" not in res)
    check("sent True", res["sent"] is True)
    check("send id", res["id"] == SEND_FIXTURE["id"])
    check("one request captured", len(captured_sends) == 1)
    raw = captured_sends[0]["raw"]
    raw_bytes = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    decoded = raw_bytes.decode("utf-8", errors="replace")
    check("raw has To", "To: ban@example.com" in decoded)
    # Non-ASCII Subject is RFC2047-encoded by EmailMessage; parse it back.
    from email import message_from_bytes
    from email.header import decode_header, make_header
    parsed = message_from_bytes(raw_bytes)
    check("raw has Subject",
          str(make_header(decode_header(parsed["Subject"]))) == "Xin chào")
    check("raw has body", "Nội dung thư" in decoded and "dòng 2." in decoded)


def test_send_validation():
    c = make_connector()
    check("missing to errors", "error" in c.gmail_send("", "s", "b"))
    check("missing subject errors", "error" in c.gmail_send("a@b.c", "", "b"))
    check("missing body errors", "error" in c.gmail_send("a@b.c", "s", "  "))


def test_api_error_is_honest():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={
            "error": {"code": 401, "message": "Invalid Credentials",
                      "errors": [{"message": "Invalid Credentials", "domain": "global",
                                  "reason": "authError"}]}})
    c = GmailConnector(token="bad-token",
                       client=httpx.Client(transport=httpx.MockTransport(handler)))
    res = c.gmail_search("is:unread")
    check("401 becomes error dict", "error" in res)
    check("status in message", "401" in res["error"])
    check("api message kept", "Invalid Credentials" in res["error"])
    check("no fake success", res.get("sent") is not True and "messages" not in res)


def test_network_error_is_honest():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    c = GmailConnector(token=TOKEN,
                       client=httpx.Client(transport=httpx.MockTransport(handler)))
    res = c.gmail_read(MSG_1)
    check("network failure becomes error dict", "error" in res)
    check("no exception escapes", isinstance(res, dict))


def test_build_tools_shape_and_prefix():
    c = make_connector()
    tools = c.build_tools()
    check("three tools", set(tools) == {"gmail_search", "gmail_read", "gmail_send"})
    for name, meta in tools.items():
        check(f"{name} prefixed", name.startswith("gmail_"))
        for key in ("name", "func", "description", "schema"):
            check(f"{name} has {key}", key in meta)
        check(f"{name} schema object", meta["schema"]["type"] == "object")
    check("search requires query", tools["gmail_search"]["schema"]["required"] == ["query"])
    check("send requires all", tools["gmail_send"]["schema"]["required"] == ["to", "subject", "body"])
    # module-level factory (environment.py convention) works too
    mod_tools = build_tools(env=None)
    check("module factory same tools", set(mod_tools) == set(tools))


def test_module_connector_instance_for_registry():
    check("CONNECTOR exists", CONNECTOR is not None)
    check("CONNECTOR name", CONNECTOR.name == "gmail")
    check("CONNECTOR is Connector",
          type(CONNECTOR).__mro__[1].__name__ == "Connector")
    # End-to-end with the real framework registry (worker 1).
    from connectors.registry import load_default_registry
    os.environ["BOOM_GMAIL_ACCESS_TOKEN"] = TOKEN
    try:
        registry = load_default_registry(("connectors.gmail",))
        check("registry picks up gmail", registry.names() == ["gmail"])
        check("registry tools merged",
              set(registry.build_tools()) == {"gmail_search", "gmail_read", "gmail_send"})
        check("registry status honest",
              registry.status_all()["gmail"]["connected"] is True)
    finally:
        os.environ.pop("BOOM_GMAIL_ACCESS_TOKEN", None)


if __name__ == "__main__":
    # Deterministic order; each test that needs env isolation handles it itself.
    for fn in (test_status_with_token, test_status_without_token,
               test_tools_error_without_token, test_search_parses_list_and_metadata,
               test_search_empty_result, test_search_metadata_failure_kept_honest,
               test_search_validation, test_read_multipart_prefers_plain,
               test_read_html_only_falls_back_to_stripped_text, test_read_validation,
               test_send_builds_rfc2822_and_confirms, test_send_validation,
               test_api_error_is_honest, test_network_error_is_honest,
               test_build_tools_shape_and_prefix, test_module_connector_instance_for_registry):
        fn()
    print("ALL GMAIL TESTS PASSED")
