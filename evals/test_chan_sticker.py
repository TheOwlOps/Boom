"""Offline tests for channel send_sticker action.

Covers: registry dispatch (unknown channel -> honest error), each platform's
real endpoint + payload (asserted through a fake httpx transport), malformed
input -> honest error, platforms without a sticker API -> honest
{"error": ...}, and daemon.send_sticker delegating to the registry.

Run: python3 evals/test_chan_sticker.py (needs httpx importable; the transport
is faked, no network, no tokens).
"""

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # repo root -> `gateway` package

PASS = 0


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        raise AssertionError(f"FAIL {name} {extra}")


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# ------------------------------------------------------------------ fake httpx

REQUESTS = []


class FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


def _route_request(method, url, kwargs):
    REQUESTS.append({"method": method, "url": url, "kwargs": kwargs})
    if "sendSticker" in url:
        return 200, {"ok": True, "result": {"sticker": {"file_id": "abc"}}}
    if "openapi.zalo.me" in url:
        return 200, {"error": 0, "message": "Success", "data": {}}
    if "api.line.me" in url:
        return 200, {}
    if "graph.facebook.com" in url:
        return 200, {"messages": [{"id": "wamid.123"}]}
    if "discord.com" in url:
        if method == "POST":
            return 200, {"id": "1"}
        return 200, {}
    return 200, {}


class FakeAsyncClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def _req(self, method, url, **kwargs):
        status, data = _route_request(method, url, kwargs)
        return FakeResponse(status, data)

    async def post(self, url, **kwargs):
        return await self._req("POST", url, **kwargs)

    async def get(self, url, **kwargs):
        return await self._req("GET", url, **kwargs)

    async def put(self, url, **kwargs):
        return await self._req("PUT", url, **kwargs)

    async def patch(self, url, **kwargs):
        return await self._req("PATCH", url, **kwargs)

    async def delete(self, url, **kwargs):
        return await self._req("DELETE", url, **kwargs)


# ------------------------------------------------------------------ imports

# gateway.discord pulls tools.ws_client at import; stub it — the sticker path
# never touches the websocket.
_stub("tools", __path_=[])
_stub("tools.ws_client", WebSocket=object)

from gateway.channel import ChannelRegistry, ChannelAdapter  # noqa: E402
from gateway.telegram import TelegramAdapter  # noqa: E402
from gateway.discord import DiscordChannel  # noqa: E402
from gateway.line import LineAdapter  # noqa: E402
from gateway.zalo import ZaloAdapter  # noqa: E402
from gateway.whatsapp import WhatsAppAdapter  # noqa: E402
from gateway.slack import SlackAdapter  # noqa: E402
from gateway.feishu import FeishuAdapter  # noqa: E402
from gateway.messenger import MessengerAdapter  # noqa: E402


def last():
    return REQUESTS[-1]


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ------------------------------------------------------------------ tests

with patch("httpx.AsyncClient", FakeAsyncClient):
    reg = ChannelRegistry()
    reg.register(TelegramAdapter(token="tg"))
    reg.register(DiscordChannel(daemon=None, token="dc"))
    reg.register(LineAdapter(channel_token="line"))
    reg.register(ZaloAdapter(access_token="zalo"))
    reg.register(WhatsAppAdapter(token="wa", phone_id="123"))
    reg.register(SlackAdapter())
    reg.register(FeishuAdapter())
    reg.register(MessengerAdapter(page_token="mg"))

    # 1. unknown channel -> honest error
    r = run(reg.send_sticker("nope", "t", "s"))
    check("registry unknown channel", r == {"error": "unknown channel 'nope'"}, r)

    # 2. telegram -> sendSticker with chat_id + sticker
    REQUESTS.clear()
    r = run(reg.send_sticker("telegram", "chat1", "CAACAgE"))
    check("telegram ok", r == {"ok": True}, r)
    q = last()
    check("telegram endpoint", q["method"] == "POST" and q["url"].endswith("/sendSticker"), q["url"])
    check("telegram payload", q["kwargs"]["json"] == {"chat_id": "chat1", "sticker": "CAACAgE"},
          q["kwargs"]["json"])

    # 3. discord -> POST /channels/{id}/messages with sticker_ids
    REQUESTS.clear()
    r = run(reg.send_sticker("discord", "chan9", "123456"))
    check("discord ok", r == {"ok": True}, r)
    q = last()
    check("discord endpoint", q["method"] == "POST" and q["url"].endswith("/channels/chan9/messages"), q["url"])
    check("discord payload", q["kwargs"]["json"] == {"sticker_ids": ["123456"]}, q["kwargs"]["json"])
    check("discord auth", q["kwargs"]["headers"] == {"Authorization": "Bot dc"}, q["kwargs"].get("headers"))

    # 4. line -> type sticker with packageId/stickerId
    REQUESTS.clear()
    r = run(reg.send_sticker("line", "user1", "446:1988"))
    check("line ok", r == {"ok": True}, r)
    q = last()
    check("line endpoint", q["url"].endswith("/v2/bot/message/push"), q["url"])
    msgs = q["kwargs"]["json"]["messages"]
    check("line payload", msgs == [{"type": "sticker", "packageId": "446", "stickerId": "1988"}], msgs)

    # 4b. line malformed -> honest error, no HTTP
    REQUESTS.clear()
    r = run(reg.send_sticker("line", "user1", "not-a-pair"))
    check("line malformed error", "error" in r and "packageId:stickerId" in r["error"], r)
    check("line malformed no http", REQUESTS == [], REQUESTS)

    # 5. zalo -> stickerid in message
    REQUESTS.clear()
    r = run(reg.send_sticker("zalo", "user7", "sticker_42"))
    check("zalo ok", r == {"ok": True}, r)
    q = last()
    check("zalo endpoint", "openapi.zalo.me/v3.0/oa/message/cs" in q["url"], q["url"])
    check("zalo payload", q["kwargs"]["json"]["message"] == {"stickerid": "sticker_42"},
          q["kwargs"]["json"])

    # 6. whatsapp media id -> type sticker with id
    REQUESTS.clear()
    r = run(reg.send_sticker("whatsapp", "84901", "media_99"))
    check("whatsapp ok", r == {"ok": True}, r)
    q = last()
    payload = q["kwargs"]["json"]
    check("whatsapp type", payload["type"] == "sticker", payload)
    check("whatsapp id", payload["sticker"] == {"id": "media_99"}, payload)

    # 6b. whatsapp URL -> link
    REQUESTS.clear()
    r = run(reg.send_sticker("whatsapp", "84901", "https://x.test/s.webp"))
    check("whatsapp url ok", r == {"ok": True}, r)
    payload = last()["kwargs"]["json"]
    check("whatsapp link", payload["sticker"] == {"link": "https://x.test/s.webp"}, payload)

    # 6c. whatsapp empty -> honest error
    r = run(reg.send_sticker("whatsapp", "84901", "  "))
    check("whatsapp empty error", r == {"error": "whatsapp sticker is empty"}, r)

    # 7. platforms without a sticker API -> honest "not supported"
    for ch in ("slack", "feishu", "messenger"):
        REQUESTS.clear()
        r = run(reg.send_sticker(ch, "t", "s"))
        check(f"{ch} not supported", "error" in r and "not supported" in r["error"], r)
        check(f"{ch} no http", REQUESTS == [], REQUESTS)

    # 8. base adapter default
    r = run(ChannelAdapter().send_sticker("t", "s"))
    check("base honest error", r == {"error": "base: stickers not supported by this platform"}, r)

    # 9. telegram refusal surfaces honestly (simulate 400)
    def _refuse_400(method, url, kwargs):
        REQUESTS.append({"method": method, "url": url, "kwargs": kwargs})
        return 400, {"ok": False, "description": "Bad Request: wrong file identifier"}
    with patch("httpx.AsyncClient", FakeAsyncClient):
        orig = _route_request
        try:
            globals()["_route_request"] = _refuse_400
            r = run(reg.send_sticker("telegram", "chat1", "bogus"))
            check("telegram refusal honest",
                  "error" in r and "400" in r["error"] and "wrong file identifier" in r["error"], r)
        finally:
            globals()["_route_request"] = orig

# 10. daemon.send_sticker delegates to the registry
_stub("agent", __path_=[])
_stub("agent.src", __path_=[])
_stub("agent.src.security", __path_=[])
_stub("agent.src.security.system_one_gate", SystemOneGate=object)
_stub("gateway.delivery_ledger", DeliveryLedger=object)
_stub("gateway.dispatcher", TurnDispatcher=object, new_job=object)
_stub("gateway.discord", DiscordChannel=object)
_stub("gateway.ingress", IngressServer=object)
import gateway.daemon as daemon_mod  # noqa: E402


class FakeRegistry:
    def __init__(self):
        self.calls = []

    async def send_sticker(self, channel, target, sticker):
        self.calls.append((channel, target, sticker))
        return {"ok": True, "via": "registry"}


d = daemon_mod.GatewayDaemon.__new__(daemon_mod.GatewayDaemon)
d.registry = FakeRegistry()
r = asyncio.get_event_loop().run_until_complete(d.send_sticker("telegram", "c", "s"))
check("daemon delegates", r == {"ok": True, "via": "registry"}, r)
check("daemon args", d.registry.calls == [("telegram", "c", "s")], d.registry.calls)

print(f"\nALL STICKER TESTS PASSED ({PASS} checks)")
