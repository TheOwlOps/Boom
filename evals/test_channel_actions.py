"""Offline tests for the unified channel adapter layer + message actions.

1. Interface: every adapter is a ChannelAdapter, registry fans out, unsupported
   actions honestly answer {"error": ...}.
2. Actions: react/edit/delete hit the real documented endpoint of each platform
   (asserted on URL/method/payload through a fake httpx transport).
3. Differential old-vs-new: the pre-refactor daemon.py/ingress.py (loaded from
   /tmp/survey_*.py) vs the new adapter-based code — send_to, can_deliver,
   _verify, _handshake and _normalize must behave identically.
4. New channels: Zalo OA send, Messenger send + webhook verify + signature.

Run: python3 evals/test_channel_actions.py (needs httpx importable; the transport
is faked, no network).
"""

import asyncio
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import time
import types
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # channels/ -> `gateway` package

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

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _route_request(method, url, kwargs):
    REQUESTS.append({"method": method, "url": url, "kwargs": kwargs})
    if "tenant_access_token" in url:
        return 200, {"tenant_access_token": "fake-tok", "expire": 7200}
    if "setMessageReaction" in url or "editMessageText" in url or "deleteMessage" in url:
        return 200, {"ok": True}
    if "sendMessage" in url:
        return 200, {"ok": True, "result": {"message_id": 1}}
    if "reactions.add" in url or "chat.update" in url or "chat.delete" in url:
        return 200, {"ok": True}
    if "chat.postMessage" in url:
        return 200, {"ok": True, "ts": "123.456"}
    if "openapi.zalo.me" in url:
        return 200, {"error": 0, "message": "Success", "data": {}}
    if "graph.facebook.com" in url:
        if "me/messages" in url:
            return 200, {"recipient_id": "1", "message_id": "mid.123"}
        return 200, {"messages": [{"id": "wamid.123"}]}
    if "api.line.me" in url:
        return 200, {}
    if "open.feishu.cn" in url:
        return 200, {"code": 0, "msg": "success", "data": {}}
    if "discord.com" in url:
        if method == "PUT":
            return 204, {}
        if method == "PATCH":
            return 200, {"id": "1"}
        if method == "DELETE":
            return 204, {}
        return 200, {"id": "1"}
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


# ------------------------------------------------------------------ module loading

# tools.ws_client is imported by gateway.discord; stub it for offline tests.
_stub("tools")
_stub("tools.ws_client", WebSocket=object)
# agent.* and the untouched gateway siblings are not part of this change.
_stub("agent")
_stub("agent.src")
_stub("agent.src.security")
_stub("agent.src.security.system_one_gate", SystemOneGate=object)
# gateway/discord.py does `from agent.src.tools.ws_client import WebSocket`
# (repo-root import style, like daemon.py); stub it too, otherwise the
# stubbed non-package `agent.src` module breaks that import.
_stub("agent.src.tools")
_stub("agent.src.tools.ws_client", WebSocket=object)
_stub("gateway.delivery_ledger", DeliveryLedger=object)
_stub("gateway.dispatcher", TurnDispatcher=object, new_job=lambda *a, **k: None)

import gateway.channel as channel_mod
import gateway.telegram as telegram_mod
import gateway.slack as slack_mod
import gateway.whatsapp as whatsapp_mod
import gateway.line as line_mod
import gateway.feishu as feishu_mod
import gateway.zalo as zalo_mod
import gateway.messenger as messenger_mod
import gateway.discord as discord_mod
import gateway.daemon as daemon_mod
import gateway.ingress as ingress_mod


def _load_old(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # shadow the new gateway.discord / gateway.ingress while the old code imports
    saved = {}
    for key in ("gateway.discord", "gateway.ingress"):
        saved[key] = sys.modules.get(key)
        _stub(key, **({"DiscordChannel": object, "IngressServer": object}
                      if key == "gateway.discord" else {"IngressServer": object}))
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        for key, val in saved.items():
            if val is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = val
    return mod


OLD_DAEMON = "/tmp/survey_daemon.py"
OLD_INGRESS = "/tmp/survey_ingress.py"
old_daemon = _load_old(OLD_DAEMON, "old_daemon") if Path(OLD_DAEMON).exists() else None
old_ingress = _load_old(OLD_INGRESS, "old_ingress") if Path(OLD_INGRESS).exists() else None


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ 1. interface

def test_interface():
    reg = channel_mod.build_default_registry()
    check("registry names",
          reg.names() == ["feishu", "line", "messenger", "slack", "telegram",
                          "whatsapp", "zalo"],
          reg.names())
    for name in reg.names():
        a = reg.get(name)
        check(f"{name} is ChannelAdapter", isinstance(a, channel_mod.ChannelAdapter))
    check("unknown channel send_to False", run(reg.send_to("tiktok", "t", "x")) is False)
    check("unknown channel can_deliver False", reg.can_deliver("tiktok") is False)

    base = channel_mod.ChannelAdapter()
    check("base react not supported",
          "not supported" in run(base.react("t", "m", "x"))["error"])
    check("base edit not supported", "error" in run(base.edit_message("t", "m", "x")))
    check("base delete not supported", "error" in run(base.delete_message("t", "m")))
    check("unknown channel react error",
          run(reg.react("tiktok", "t", "m", "x"))["error"].startswith("unknown channel"))

    # platforms without the action stay honest
    with patch("httpx.AsyncClient", FakeAsyncClient):
        for ch in ("line", "zalo", "messenger"):
            r = run(reg.react(ch, "t", "m", "❤️"))
            check(f"{ch} react not supported", "not supported" in r["error"], r)
            r = run(reg.edit_message(ch, "t", "m", "new"))
            check(f"{ch} edit not supported", "error" in r, r)
            r = run(reg.delete_message(ch, "t", "m"))
            check(f"{ch} delete not supported", "error" in r, r)
        for ch in ("whatsapp",):
            r = run(reg.edit_message(ch, "t", "m", "new"))
            check(f"{ch} edit not supported", "error" in r, r)
            r = run(reg.delete_message(ch, "t", "m"))
            check(f"{ch} delete not supported", "error" in r, r)


# ------------------------------------------------------------------ 2. actions hit real endpoints

def test_actions():
    reg = channel_mod.build_default_registry()
    # discord joins the registry via the daemon in production; register it here
    reg.register(discord_mod.DiscordChannel(None, "dc-tok"))
    with patch("httpx.AsyncClient", FakeAsyncClient):
        # telegram: setMessageReaction / editMessageText / deleteMessage
        REQUESTS.clear()
        r = run(reg.react("telegram", "chat1", "42", "❤️"))
        check("telegram react ok", r == {"ok": True}, r)
        last = REQUESTS[-1]
        check("telegram react url", last["url"].endswith("/setMessageReaction"), last["url"])
        check("telegram react payload",
              last["kwargs"]["json"] == {"chat_id": "chat1", "message_id": 42,
                                         "reaction": [{"type": "emoji", "emoji": "❤️"}]},
              last["kwargs"]["json"])
        r = run(reg.edit_message("telegram", "chat1", "42", "new text"))
        check("telegram edit ok", r == {"ok": True}, r)
        check("telegram edit url", REQUESTS[-1]["url"].endswith("/editMessageText"))
        r = run(reg.delete_message("telegram", "chat1", "42"))
        check("telegram delete ok", r == {"ok": True}, r)
        check("telegram delete url", REQUESTS[-1]["url"].endswith("/deleteMessage"))

        # discord: PUT reactions / PATCH / DELETE
        r = run(reg.react("discord", "chan9", "msg7", "❤️"))
        check("discord react ok", r == {"ok": True}, r)
        last = REQUESTS[-1]
        check("discord react method+url",
              last["method"] == "PUT" and
              last["url"].endswith(f"/channels/chan9/messages/msg7/reactions/{quote('❤️', safe='')}/@me"),
              last["url"])
        r = run(reg.edit_message("discord", "chan9", "msg7", "edited"))
        check("discord edit ok", r == {"ok": True}, r)
        last = REQUESTS[-1]
        check("discord edit method+payload",
              last["method"] == "PATCH" and last["kwargs"]["json"] == {"content": "edited"},
              (last["method"], last["kwargs"].get("json")))
        r = run(reg.delete_message("discord", "chan9", "msg7"))
        check("discord delete ok", r == {"ok": True}, r)
        check("discord delete method", REQUESTS[-1]["method"] == "DELETE")

        # slack: reactions.add with emoji NAME mapping, chat.update, chat.delete
        r = run(reg.react("slack", "C1", "123.456", "❤️"))
        check("slack react ok", r == {"ok": True}, r)
        last = REQUESTS[-1]
        check("slack react endpoint", last["url"].endswith("/reactions.add"), last["url"])
        check("slack react name mapped",
              last["kwargs"]["json"] == {"channel": "C1", "timestamp": "123.456", "name": "heart"},
              last["kwargs"]["json"])
        r = run(reg.edit_message("slack", "C1", "123.456", "fixed"))
        check("slack edit ok", r == {"ok": True}, r)
        check("slack edit endpoint", REQUESTS[-1]["url"].endswith("/chat.update"))
        r = run(reg.delete_message("slack", "C1", "123.456"))
        check("slack delete ok", r == {"ok": True}, r)
        check("slack delete endpoint", REQUESTS[-1]["url"].endswith("/chat.delete"))
        # refused call -> honest error (Slack answers 200 {"ok": false})
        with patch("httpx.AsyncClient", _RefusingClient):
            # adapters cache their shared httpx client (_http() is lazy-once):
            # drop the caches so the patched refusing client is actually used.
            for _a in reg._adapters.values():
                _a.__dict__.pop("_client", None)
            r = run(reg.react("slack", "C1", "1", "heart"))
            check("slack refused -> error", r["error"].startswith("slack reactions.add refused"), r)

        # whatsapp: reaction message type
        r = run(reg.react("whatsapp", "84901", "wamid.1", "❤️"))
        check("whatsapp react ok", r == {"ok": True}, r)
        last = REQUESTS[-1]
        check("whatsapp react payload",
              last["kwargs"]["json"]["reaction"] == {"message_id": "wamid.1", "emoji": "❤️"}
              and last["kwargs"]["json"]["type"] == "reaction",
              last["kwargs"]["json"])

        # feishu: reactions / PUT / DELETE on im/v1/messages
        feishu_mod._FEISHU_TOKEN.update({"value": "", "expiry": 0.0})
        r = run(reg.react("feishu", "chat1", "msg1", "SMILE"))
        check("feishu react ok", r == {"ok": True}, r)
        last = REQUESTS[-1]
        check("feishu react url",
              last["url"].endswith("/open-apis/im/v1/messages/msg1/reactions"), last["url"])
        check("feishu react payload",
              last["kwargs"]["json"] == {"reaction_type": {"emoji_type": "SMILE"}},
              last["kwargs"]["json"])
        r = run(reg.edit_message("feishu", "chat1", "msg1", "new"))
        check("feishu edit ok", r == {"ok": True}, r)
        check("feishu edit method+url",
              REQUESTS[-1]["method"] == "PUT" and
              REQUESTS[-1]["url"].endswith("/open-apis/im/v1/messages/msg1"))
        r = run(reg.delete_message("feishu", "chat1", "msg1"))
        check("feishu delete ok", r == {"ok": True}, r)
        check("feishu delete method", REQUESTS[-1]["method"] == "DELETE")


class _RefusingClient(FakeAsyncClient):
    async def _req(self, method, url, **kwargs):
        REQUESTS.append({"method": method, "url": url, "kwargs": kwargs})
        return FakeResponse(200, {"ok": False, "error": "not_in_channel"})


# ------------------------------------------------------------------ 3. differential old vs new

ENV_KEYS = ["TELEGRAM_BOT_TOKEN", "BOOM_SLACK_BOT_TOKEN", "BOOM_WHATSAPP_TOKEN",
            "BOOM_WHATSAPP_PHONE_ID", "BOOM_LINE_CHANNEL_TOKEN",
            "BOOM_FEISHU_APP_ID", "BOOM_FEISHU_APP_SECRET",
            "BOOM_ZALO_OA_ACCESS_TOKEN", "BOOM_MESSENGER_PAGE_TOKEN"]

FULL_ENV = {
    "TELEGRAM_BOT_TOKEN": "tg-tok",
    "BOOM_SLACK_BOT_TOKEN": "xoxb-tok",
    "BOOM_WHATSAPP_TOKEN": "wa-tok",
    "BOOM_WHATSAPP_PHONE_ID": "12345",
    "BOOM_LINE_CHANNEL_TOKEN": "line-tok",
    "BOOM_FEISHU_APP_ID": "app-id",
    "BOOM_FEISHU_APP_SECRET": "app-sec",
}


def _set_env(mapping):
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ.update(mapping)
    return saved


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_differential_can_deliver():
    if old_daemon is None:
        print("SKIP differential (old daemon source not found)")
        return
    for with_env in (FULL_ENV, {}):
        saved = _set_env(with_env)
        try:
            old = old_daemon.GatewayDaemon(runner_fn=lambda *a: None,
                                           delivery_ledger=types.SimpleNamespace())
            new = daemon_mod.GatewayDaemon(runner_fn=lambda *a: None,
                                           delivery_ledger=types.SimpleNamespace())
            for ch in ("telegram", "discord", "slack", "whatsapp", "line", "feishu",
                       "zalo", "messenger", "tiktok"):
                o, n = old.can_deliver(ch), new.can_deliver(ch)
                # zalo/messenger are new channels: old code had no branch -> False
                if ch in ("zalo", "messenger") and with_env:
                    continue  # covered separately below
                check(f"can_deliver {ch} env={'full' if with_env else 'empty'}", o == n, (o, n))
        finally:
            _restore_env(saved)
    # new channels behave sanely
    saved = _set_env({**FULL_ENV, "BOOM_ZALO_OA_ACCESS_TOKEN": "z", "BOOM_MESSENGER_PAGE_TOKEN": "m"})
    try:
        new = daemon_mod.GatewayDaemon(runner_fn=lambda *a: None,
                                       delivery_ledger=types.SimpleNamespace())
        check("zalo can_deliver with token", new.can_deliver("zalo") is True)
        check("messenger can_deliver with token", new.can_deliver("messenger") is True)
    finally:
        _restore_env(saved)


def test_differential_send_to():
    if old_daemon is None:
        print("SKIP differential (old daemon source not found)")
        return
    saved = _set_env(FULL_ENV)
    try:
        with patch("httpx.AsyncClient", FakeAsyncClient):
            for channel, target in (("telegram", "chat1"), ("slack", "C1"),
                                    ("whatsapp", "84901"), ("line", "U1"),
                                    ("feishu", "oc_1")):
                old = old_daemon.GatewayDaemon(runner_fn=lambda *a: None,
                                               delivery_ledger=types.SimpleNamespace())
                new = daemon_mod.GatewayDaemon(runner_fn=lambda *a: None,
                                               delivery_ledger=types.SimpleNamespace())
                if channel == "feishu":
                    old_daemon._FEISHU_TOKEN.update({"value": "", "expiry": 0.0})
                    feishu_mod._FEISHU_TOKEN.update({"value": "", "expiry": 0.0})
                REQUESTS.clear()
                o_ret = run(old.send_to(channel, target, "hello"))
                o_req = REQUESTS[-1]
                REQUESTS.clear()
                n_ret = run(new.send_to(channel, target, "hello"))
                n_req = REQUESTS[-1]
                # feishu makes 2 requests (token + send); compare the send
                if channel == "feishu":
                    o_req = [r for r in [o_req] if "tenant_access_token" not in r["url"]][0]
                    n_req = [r for r in [n_req] if "tenant_access_token" not in r["url"]][0]
                check(f"send_to {channel} return", o_ret == n_ret, (o_ret, n_ret))
                check(f"send_to {channel} method", o_req["method"] == n_req["method"])
                check(f"send_to {channel} url", o_req["url"] == n_req["url"],
                      (o_req["url"], n_req["url"]))
                check(f"send_to {channel} json", o_req["kwargs"].get("json") ==
                      n_req["kwargs"].get("json"),
                      (o_req["kwargs"].get("json"), n_req["kwargs"].get("json")))
                check(f"send_to {channel} headers", o_req["kwargs"].get("headers") ==
                      n_req["kwargs"].get("headers"))
                check(f"send_to {channel} params", o_req["kwargs"].get("params") ==
                      n_req["kwargs"].get("params"))
            # discord: old path needs a live DiscordChannel; compare via stub vs real
            REQUESTS.clear()

            class OldDiscordStub:
                async def send_message(self, target, text):
                    REQUESTS.append({"stub": "old-discord", "target": target, "text": text})

            old = old_daemon.GatewayDaemon(runner_fn=lambda *a: None,
                                           delivery_ledger=types.SimpleNamespace())
            old.discord = OldDiscordStub()
            new = daemon_mod.GatewayDaemon(runner_fn=lambda *a: None,
                                           delivery_ledger=types.SimpleNamespace())
            new.registry.register(discord_mod.DiscordChannel(None, "dc-tok"))
            o_ret = run(old.send_to("discord", "chan1", "hi"))
            n_ret = run(new.send_to("discord", "chan1", "hi"))
            check("send_to discord return", o_ret is True and n_ret is True, (o_ret, n_ret))
            check("send_to discord url",
                  REQUESTS[-1]["url"] == "https://discord.com/api/v10/channels/chan1/messages",
                  REQUESTS[-1]["url"])
            # unknown channel
            old = old_daemon.GatewayDaemon(runner_fn=lambda *a: None,
                                           delivery_ledger=types.SimpleNamespace())
            new = daemon_mod.GatewayDaemon(runner_fn=lambda *a: None,
                                           delivery_ledger=types.SimpleNamespace())
            check("send_to unknown False", run(old.send_to("tiktok", "t", "x")) is False
                  and run(new.send_to("tiktok", "t", "x")) is False)
    finally:
        _restore_env(saved)


def _slack_sig(secret, stamp, body):
    return "v0=" + hmac.new(secret.encode(), b"v0:" + stamp.encode() + b":" + body,
                             hashlib.sha256).hexdigest()


def test_differential_ingress():
    if old_ingress is None:
        print("SKIP differential (old ingress source not found)")
        return
    secret, fkey, lsec, token = "ssec", "fkey", "lsec", "tok"
    old_srv = old_ingress.IngressServer(None, port=0, token=token, slack_secret=secret,
                                        feishu_key=fkey, line_secret=lsec)
    new_srv = ingress_mod.IngressServer(None, port=0, token=token, slack_secret=secret,
                                        feishu_key=fkey, line_secret=lsec)

    # _verify
    body = b'{"type":"event_callback"}'
    stamp = str(int(time.time()))
    hdr = {"x-slack-signature": _slack_sig(secret, stamp, body),
           "x-slack-request-timestamp": stamp}
    check("verify slack ok",
          old_srv._verify("slack", hdr, body) is None and new_srv._verify("slack", hdr, body) is None)
    bad = dict(hdr, **{"x-slack-signature": "v0=dead"})
    check("verify slack bad",
          old_srv._verify("slack", bad, body) == new_srv._verify("slack", bad, body) ==
          "bad Slack signature")
    check("verify unknown channel + token",
          old_srv._verify("discord", {"x-boom-token": token}, b"{}") is None
          and new_srv._verify("discord", {"x-boom-token": token}, b"{}") is None)
    check("verify unknown channel bad token",
          old_srv._verify("discord", {}, b"{}") == new_srv._verify("discord", {}, b"{}") ==
          "bad or missing X-Boom-Token")

    fbody = b'{"header":{}}'
    fstamp, fnonce = "1700000000", "abc"
    fsig = hashlib.sha256((fstamp + fnonce + fkey).encode() + fbody).hexdigest()
    fhdr = {"x-lark-signature": fsig, "x-lark-request-timestamp": fstamp,
            "x-lark-request-nonce": fnonce}
    check("verify feishu ok",
          old_srv._verify("feishu", fhdr, fbody) is None
          and new_srv._verify("feishu", fhdr, fbody) is None)
    check("verify feishu bad",
          old_srv._verify("feishu", dict(fhdr, **{"x-lark-signature": "x"}), fbody) ==
          new_srv._verify("feishu", dict(fhdr, **{"x-lark-signature": "x"}), fbody) ==
          "bad Feishu signature")

    lbody = b'{"events":[]}'
    lsig = base64.b64encode(hmac.new(lsec.encode(), lbody, hashlib.sha256).digest()).decode()
    check("verify line ok",
          old_srv._verify("line", {"x-line-signature": lsig}, lbody) is None
          and new_srv._verify("line", {"x-line-signature": lsig}, lbody) is None)

    # module-level _handshake / _normalize
    for ch, ev in (("slack", {"type": "url_verification", "challenge": "c1"}),
                   ("feishu", {"type": "url_verification", "challenge": "c2"}),
                   ("other", {"type": "url_verification"})):
        check(f"handshake {ch}", old_ingress._handshake(ch, ev) == ingress_mod._handshake(ch, ev))

    slack_ev = {"type": "event_callback",
                "event": {"type": "message", "user": "U9", "text": "hi",
                          "ts": "1.2", "channel": "C9"}}
    check("normalize slack",
          old_ingress._normalize("slack", slack_ev) == ingress_mod._normalize("slack", slack_ev))
    wa_ev = {"entry": [{"changes": [{"value": {
        "messages": [{"type": "text", "from": "84901", "id": "w1",
                      "text": {"body": "yo"}}],
        "contacts": [{"profile": {"name": "An"}}]}}]}]}
    check("normalize whatsapp",
          old_ingress._normalize("whatsapp", wa_ev) == ingress_mod._normalize("whatsapp", wa_ev))
    fs_ev = {"header": {"event_type": "im.message.receive_v1"},
             "event": {"sender": {"sender_id": {"open_id": "ou_1"}},
                       "message": {"message_id": "m1", "chat_id": "oc_1",
                                   "message_type": "text",
                                   "content": '{"text":"hi"}'}}}
    check("normalize feishu",
          old_ingress._normalize("feishu", fs_ev) == ingress_mod._normalize("feishu", fs_ev))
    line_ev = {"events": [{"type": "message", "message": {"type": "text", "text": "hi", "id": "m5"},
                           "source": {"userId": "U5"}}]}
    check("normalize line",
          old_ingress._normalize("line", line_ev) == ingress_mod._normalize("line", line_ev))
    canon = {"sender_id": "u1", "sender_name": "A", "text": "hi", "message_id": "m1"}
    check("normalize canonical passthrough",
          old_ingress._normalize("zalo", canon) == ingress_mod._normalize("zalo", canon) == canon)
    check("normalize slack ignore",
          old_ingress._normalize("slack", {"type": "app_mention"}) ==
          ingress_mod._normalize("slack", {"type": "app_mention"}))


# ------------------------------------------------------------------ 4. new channels

def test_new_channels():
    reg = channel_mod.build_default_registry()
    with patch("httpx.AsyncClient", FakeAsyncClient):
        saved = _set_env({"BOOM_ZALO_OA_ACCESS_TOKEN": "z", "BOOM_MESSENGER_PAGE_TOKEN": "p",
                          "BOOM_MESSENGER_APP_SECRET": "s", "BOOM_MESSENGER_VERIFY_TOKEN": "v"})
        try:
            # zalo send
            REQUESTS.clear()
            check("zalo send", run(reg.send_to("zalo", "user1", "xin chào")) is True)
            last = REQUESTS[-1]
            check("zalo url", last["url"] == "https://openapi.zalo.me/v3.0/oa/message/cs",
                  last["url"])
            check("zalo headers+payload",
                  last["kwargs"]["headers"] == {"access_token": "z"}
                  and last["kwargs"]["json"] == {"recipient": {"user_id": "user1"},
                                                 "message": {"text": "xin chào"}},
                  last["kwargs"])
            # messenger send
            REQUESTS.clear()
            check("messenger send", run(reg.send_to("messenger", "psid1", "hello")) is True)
            last = REQUESTS[-1]
            check("messenger url", last["url"] == "https://graph.facebook.com/v21.0/me/messages",
                  last["url"])
            check("messenger payload",
                  last["kwargs"]["json"]["recipient"] == {"id": "psid1"}
                  and last["kwargs"]["params"] == {"access_token": "p"},
                  last["kwargs"])
            # messenger webhook verify
            m = reg.get("messenger")
            check("messenger verify ok",
                  m.verify_webhook({"hub.mode": "subscribe", "hub.verify_token": "v",
                                    "hub.challenge": "CH"}) == "CH")
            check("messenger verify bad token",
                  m.verify_webhook({"hub.mode": "subscribe", "hub.verify_token": "wrong",
                                    "hub.challenge": "CH"}) is None)
            # messenger signature
            body = b'{"entry":[]}'
            sig = "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest()
            check("messenger signature ok",
                  m.verify_request({"x-hub-signature-256": sig}, body) is None)
            check("messenger signature bad",
                  m.verify_request({"x-hub-signature-256": "sha256=x"}, body) ==
                  "bad X-Hub-Signature-256")
            check("messenger has secret", m.has_verify_secret() is True)
            # messenger normalize
            ev = {"entry": [{"messaging": [
                {"sender": {"id": "psid1"}, "message": {"mid": "m1", "text": "hi"}}]}]}
            n = reg.normalize("messenger", ev)
            check("messenger normalize",
                  n == {"sender_id": "psid1", "sender_name": "psid1", "text": "hi",
                        "message_id": "m1", "target": "psid1"}, n)
            # telegram webhook verify + normalize
            t = reg.get("telegram")
            check("telegram verify no secret -> None",
                  telegram_mod.TelegramAdapter().verify_request({}, b"{}") is None)
            t2 = telegram_mod.TelegramAdapter(secret_token="sec")
            check("telegram verify ok",
                  t2.verify_request({"x-telegram-bot-api-secret-token": "sec"}, b"{}") is None)
            check("telegram verify bad",
                  t2.verify_request({"x-telegram-bot-api-secret-token": "x"}, b"{}") ==
                  "bad X-Telegram-Bot-Api-Secret-Token")
            check("telegram has secret", t2.has_verify_secret() is True)
            tev = {"update_id": 1, "message": {"message_id": 5, "text": "hi",
                   "from": {"id": 9, "username": "an"}, "chat": {"id": 9}}}
            n = reg.normalize("telegram", tev)
            check("telegram normalize",
                  n == {"sender_id": "9", "sender_name": "an", "text": "hi",
                        "message_id": "5", "target": "9"}, n)
            # daemon-level action helpers
            d = daemon_mod.GatewayDaemon(runner_fn=lambda *a: None,
                                        delivery_ledger=types.SimpleNamespace())
            r = run(d.react("telegram", "c", "1", "👍"))
            check("daemon.react", r == {"ok": True}, r)
            r = run(d.delete_message("line", "u", "1"))
            check("daemon.delete line -> error", "error" in r, r)
        finally:
            _restore_env(saved)


def main():
    test_interface()
    test_actions()
    test_differential_can_deliver()
    test_differential_send_to()
    test_differential_ingress()
    test_new_channels()
    print(f"\nALL CHANNEL TESTS PASSED ({PASS} checks)")


if __name__ == "__main__":
    main()
