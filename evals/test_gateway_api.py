"""Live-HTTP tests for gateway/api.py with fake seams.

Runs a real ThreadingHTTPServer on an ephemeral port; the injected fake
brain/daemon touch no network and no LLM. stdlib only.

Usage:  python3 evals/test_gateway_api.py   (from gateway-api/ dir)
"""

import asyncio
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import api as boom_api  # noqa: E402

PASSED = 0
FAILED = 0
FAILURES = []


def check(name, cond, extra=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        FAILURES.append(f"{name} {extra}".strip())


# ----------------------------------------------------------------- fakes


class FakeBrainResult:
    def __init__(self, strategy="react", text="fake reply", errors=None,
                 node_outputs=None):
        self.strategy = strategy
        self.text = text
        self.errors = errors or {}
        self.node_outputs = node_outputs or {}
        self.agent_outputs = {}


class FakeBrain:
    """Mimics Brain.run's never-raise contract; delay keeps the turn lock
    held so a concurrent request gets 429."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls = []
        self.started = threading.Event()

    async def run(self, prompt, strategy="auto", session_id=None, **kw):
        self.started.set()
        self.calls.append((prompt, strategy, session_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        used = strategy if strategy != "auto" else "react"
        return FakeBrainResult(strategy=used, text="fake reply to: " + prompt)


class FakeLoop:
    def __init__(self):
        self.loaded = []

    def load_session(self, session_id):
        self.loaded.append(session_id)


class FakeDaemon:
    """Only telegram is deliverable, like a fresh env with just one token."""

    def can_deliver(self, channel):
        return channel == "telegram"


class QuietHandler(boom_api._Handler):
    def log_message(self, fmt, *args):  # silence request chatter in tests
        pass


# --------------------------------------------------------------- http util


BASE = None


def http(method, path, body=None, headers=None):
    url = BASE + path
    data = None
    h = dict(headers or {})
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def parse_sse(raw):
    events = []
    for block in raw.decode("utf-8").strip().split("\n\n"):
        ev, data = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        events.append((ev, data))
    return events


def start_server(api_obj):
    server = boom_api.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    server.api = api_obj
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ----------------------------------------------------------------- tests


def test_health():
    status, headers, raw = http("GET", "/api/health")
    d = json.loads(raw)
    check("health/200", status == 200, f"got {status}")
    check("health/ct", headers.get("Content-Type") == "application/json",
          str(headers.get("Content-Type")))
    check("health/shape",
          d == {"status": "ok", "version": "0.1.0", "model": "fake-model"}, str(d))


def test_chat_json(brain, loop):
    status, _, raw = http("POST", "/api/chat",
                          {"prompt": "hello there", "strategy": "react"})
    d = json.loads(raw)
    check("chat/200", status == 200, f"got {status} {d}")
    check("chat/reply", d["reply"] == "fake reply to: hello there", str(d))
    check("chat/strategy", d["strategy"] == "react", str(d))
    check("chat/errors", d["errors"] == {}, str(d))
    check("chat/session_id", isinstance(d["session_id"], str) and d["session_id"],
          str(d))
    check("chat/session-passed",
          brain.calls and brain.calls[-1][2] == d["session_id"], str(brain.calls))
    check("chat/loop-load", d["session_id"] in loop.loaded, str(loop.loaded))


def test_chat_defaults():
    status, _, raw = http("POST", "/api/chat", {"prompt": "auto route me"})
    d = json.loads(raw)
    check("chat-default/200", status == 200, f"got {status}")
    check("chat-default/auto->react", d["strategy"] == "react", str(d))


def test_chat_400s():
    for body, name in [
        ({"prompt": ""}, "empty"),
        ({"prompt": "   "}, "blank"),
        ({"prompt": 123}, "non-string"),
        ({}, "missing"),
    ]:
        status, _, raw = http("POST", "/api/chat", body)
        check(f"chat400/{name}",
              status == 400 and json.loads(raw) == {"error": "prompt is required"},
              f"got {status} {raw!r}")
    status, _, raw = http("POST", "/api/chat", {"prompt": "x", "strategy": "bogus"})
    check("chat400/strategy", status == 400 and "error" in json.loads(raw),
          f"got {status} {raw!r}")
    status, _, raw = http("POST", "/api/chat", b"{not json")
    check("chat400/malformed", status == 400 and "error" in json.loads(raw),
          f"got {status} {raw!r}")


def test_chat_sse():
    import http.client
    from urllib.parse import urlparse
    u = urlparse(BASE)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=15)
    body = json.dumps({"prompt": "stream me"}).encode()
    conn.request("POST", "/api/chat", body=body,
                 headers={"Content-Type": "application/json",
                          "Accept": "text/event-stream"})
    resp = conn.getresponse()
    check("sse/200", resp.status == 200, f"got {resp.status}")
    check("sse/ct", resp.getheader("Content-Type") == "text/event-stream",
          str(resp.getheader("Content-Type")))
    # Read line by line until all three events' data lines arrived; the
    # server keeps the keep-alive connection open, so read() would hang.
    raw = b""
    deadline = time.time() + 15
    try:
        while raw.count(b"data: ") < 3:
            line = resp.fp.readline()
            if line:
                raw += line
            if time.time() > deadline:
                raise TimeoutError("sse events never arrived")
    finally:
        conn.close()
    events = parse_sse(raw)
    kinds = [e for e, _ in events]
    check("sse/order", kinds == ["status", "message", "done"], str(kinds))
    by = dict(events)
    check("sse/status", by["status"] == {"phase": "thinking"}, str(by["status"]))
    check("sse/message", by["message"] == {"text": "fake reply to: stream me"},
          str(by["message"]))
    check("sse/done",
          by["done"]["strategy"] == "react"
          and isinstance(by["done"]["session_id"], str), str(by["done"]))


def test_chat_429():
    brain = FakeBrain(delay=2.0)
    server2 = start_server(boom_api.BoomAPI(brain=brain, model="m"))
    global BASE
    old = BASE
    BASE = f"http://127.0.0.1:{server2.server_port}"
    try:
        first = {}
        def first_req():
            s, _, raw = http("POST", "/api/chat", {"prompt": "slow turn"})
            first["status"] = s
            first["body"] = json.loads(raw)

        t = threading.Thread(target=first_req)
        t.start()
        check("429/wait", brain.started.wait(timeout=10), "fake brain never ran")
        time.sleep(0.2)  # the turn lock is definitely held now
        s2, _, raw2 = http("POST", "/api/chat", {"prompt": "second turn"})
        check("429/status",
              s2 == 429 and json.loads(raw2) == {"error": "agent busy"},
              f"got {s2} {raw2!r}")
        t.join(timeout=15)
        check("429/first-ok", first.get("status") == 200, str(first))
    finally:
        BASE = old
        server2.shutdown()
        server2.server_close()


def test_sessions():
    status, _, raw = http("GET", "/api/sessions")
    check("sessions", status == 200 and json.loads(raw) == {"sessions": []},
          f"got {status} {raw!r}")


def test_channels():
    status, _, raw = http("GET", "/api/channels/status")
    d = json.loads(raw)
    expected = {"telegram", "discord", "slack", "whatsapp",
                "line", "feishu", "zalo", "messenger"}
    got = {c["name"] for c in d["channels"]}
    check("channels/200", status == 200, f"got {status}")
    check("channels/names", got == expected, str(got))
    check("channels/shape",
          all(set(c) == {"name", "connected", "detail"} for c in d["channels"]),
          str(d["channels"]))
    tg = next(c for c in d["channels"] if c["name"] == "telegram")
    check("channels/tg",
          tg["connected"] is True and tg["detail"] is None, str(tg))
    slack = next(c for c in d["channels"] if c["name"] == "slack")
    check("channels/slack-off",
          slack["connected"] is False
          and slack["detail"] == "BOOM_SLACK_BOT_TOKEN not set", str(slack))


def test_dag():
    status, _, raw = http("GET", "/api/dag/status")
    check("dag", status == 200 and json.loads(raw) == {"nodes": []},
          f"got {status} {raw!r}")


def test_cors():
    status, headers, _ = http("GET", "/api/health",
                              headers={"Origin": "http://localhost:5173"})
    check("cors/vite",
          headers.get("Access-Control-Allow-Origin") == "http://localhost:5173",
          str(headers.get("Access-Control-Allow-Origin")))
    status, headers, _ = http("GET", "/api/health",
                              headers={"Origin": "http://localhost:1420"})
    check("cors/tauri",
          headers.get("Access-Control-Allow-Origin") == "http://localhost:1420",
          str(headers.get("Access-Control-Allow-Origin")))
    status, headers, _ = http("GET", "/api/health",
                              headers={"Origin": "http://evil.example.com"})
    check("cors/evil", "Access-Control-Allow-Origin" not in headers,
          str(headers.get("Access-Control-Allow-Origin")))
    status, headers, _ = http("OPTIONS", "/api/chat",
                              headers={"Origin": "http://localhost:5173"})
    check("cors/preflight", status == 204
          and headers.get("Access-Control-Allow-Origin") == "http://localhost:5173"
          and "POST" in headers.get("Access-Control-Allow-Methods", ""),
          f"got {status}")


def main():
    global BASE
    brain = FakeBrain()
    loop = FakeLoop()
    daemon = FakeDaemon()
    api_obj = boom_api.BoomAPI(brain=brain, loop=loop, daemon=daemon,
                               model="fake-model")
    server = start_server(api_obj)
    BASE = f"http://127.0.0.1:{server.server_port}"
    try:
        test_health()
        test_chat_json(brain, loop)
        test_chat_defaults()
        test_chat_400s()
        test_chat_sse()
        test_sessions()
        test_channels()
        test_dag()
        test_cors()
        test_chat_429()
    finally:
        server.shutdown()
        server.server_close()
    print(f"{PASSED} passed, {FAILED} failed")
    for f in FAILURES:
        print("FAIL:", f)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
