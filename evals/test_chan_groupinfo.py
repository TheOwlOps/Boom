"""Offline tests for channel group-info + member-info actions.

get_group_info(channel, target) -> {id, name, member_count, type}
get_member_info(channel, target, user_id) -> {id, name, role, ...}

Every platform hits its real documented endpoint (asserted on URL/method
through a fake httpx transport). Refusals (404/forbidden/{"ok": false})
are reported as honest {"error": ...} — never fabricated. Platforms with no
bot-accessible group API (zalo, messenger, whatsapp) honestly report
not-supported via the base class.

Run: python3 evals/test_chan_groupinfo.py (httpx importable; transport faked).
"""

import asyncio
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Staged modules live in <staging>/gateway/ (repo copies + new actions).
sys.path.insert(0, str(HERE.parent))  # chan-actions/ -> `gateway` package

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
# (method, url-substring) -> (status, json) or "raise"
ROUTES = {}


class FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


def _route_request(method, url, kwargs):
    REQUESTS.append({"method": method, "url": url, "kwargs": kwargs})
    # longest substring first: "getChatMemberCount" must win over "getChat"
    for (m, sub), answer in sorted(ROUTES.items(),
                                   key=lambda kv: -len(kv[0][1])):
        if m == method and sub in url:
            if answer == "raise":
                raise RuntimeError("boom")
            return answer
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

    async def get(self, url, **kwargs):
        return await self._req("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self._req("POST", url, **kwargs)

    async def put(self, url, **kwargs):
        return await self._req("PUT", url, **kwargs)

    async def patch(self, url, **kwargs):
        return await self._req("PATCH", url, **kwargs)

    async def delete(self, url, **kwargs):
        return await self._req("DELETE", url, **kwargs)


# ------------------------------------------------------------------ module loading

_stub("tools")
_stub("tools.ws_client", WebSocket=object)
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

import httpx
httpx.AsyncClient = FakeAsyncClient  # noqa: E402

import gateway.channel as channel_mod  # noqa: E402
import gateway.telegram as telegram_mod  # noqa: E402
import gateway.slack as slack_mod  # noqa: E402
import gateway.line as line_mod  # noqa: E402
import gateway.feishu as feishu_mod  # noqa: E402
import gateway.zalo as zalo_mod  # noqa: E402
import gateway.messenger as messenger_mod  # noqa: E402
import gateway.whatsapp as whatsapp_mod  # noqa: E402
import gateway.discord as discord_mod  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def last_req(sub):
    for r in reversed(REQUESTS):
        if sub in r["url"]:
            return r
    return None


# ------------------------------------------------------------------ 1. base honesty

def test_base_honest():
    base = channel_mod.ChannelAdapter()
    check("base get_group_info honest",
          run(base.get_group_info("x"))["error"].startswith("base:"))
    check("base get_member_info honest",
          run(base.get_member_info("x", "u"))["error"].startswith("base:"))
    reg = channel_mod.ChannelRegistry()
    check("registry unknown channel group",
          run(reg.get_group_info("nope", "x")) == {"error": "unknown channel 'nope'"})
    check("registry unknown channel member",
          run(reg.get_member_info("nope", "x", "u")) == {"error": "unknown channel 'nope'"})


# ------------------------------------------------------------------ 2. telegram

def test_telegram_group():
    ROUTES.clear()
    ROUTES[("POST", "getChat")] = (200, {"ok": True, "result": {
        "id": -100123, "title": "Nhom Test", "type": "supergroup"}})
    ROUTES[("POST", "getChatMemberCount")] = (200, {"ok": True, "result": 42})
    a = telegram_mod.TelegramAdapter(token="t")
    out = run(a.get_group_info("-100123"))
    check("tg group parsed", out == {"id": "-100123", "name": "Nhom Test",
                                     "type": "supergroup", "member_count": 42}, out)
    r = last_req("getChat")
    check("tg group endpoint", r is not None and r["method"] == "POST"
          and r["kwargs"]["json"] == {"chat_id": "-100123"}, r)


def test_telegram_group_no_count():
    # private chat: getChatMemberCount fails -> member_count None, rest intact
    ROUTES.clear()
    ROUTES[("POST", "getChat")] = (200, {"ok": True, "result": {
        "id": 99, "first_name": "Anh", "type": "private"}})
    ROUTES[("POST", "getChatMemberCount")] = (400, {"ok": False})
    a = telegram_mod.TelegramAdapter(token="t")
    out = run(a.get_group_info("99"))
    check("tg group count best-effort",
          out["member_count"] is None and out["name"] == "Anh"
          and out["type"] == "private", out)


def test_telegram_group_refused():
    ROUTES.clear()
    ROUTES[("POST", "getChat")] = (403, {"ok": False, "description": "Forbidden"})
    a = telegram_mod.TelegramAdapter(token="t")
    out = run(a.get_group_info("-100123"))
    check("tg group refused honest", "error" in out and "403" in out["error"], out)


def test_telegram_member():
    ROUTES.clear()
    ROUTES[("POST", "getChatMember")] = (200, {"ok": True, "result": {
        "status": "administrator",
        "user": {"id": 777, "first_name": "Son", "username": "sonx",
                 "is_bot": False}}})
    a = telegram_mod.TelegramAdapter(token="t")
    out = run(a.get_member_info("-100123", "777"))
    check("tg member parsed", out == {"id": "777", "name": "sonx",
                                      "role": "administrator", "is_bot": False}, out)
    r = last_req("getChatMember")
    check("tg member endpoint", r["kwargs"]["json"] == {"chat_id": "-100123",
                                                        "user_id": "777"}, r)


def test_telegram_member_stranger():
    ROUTES.clear()
    ROUTES[("POST", "getChatMember")] = (400, {"ok": False,
                                              "description": "Bad Request: user not found"})
    a = telegram_mod.TelegramAdapter(token="t")
    out = run(a.get_member_info("-100123", "999"))
    check("tg stranger honest", "error" in out and "not visible" in out["error"], out)


# ------------------------------------------------------------------ 3. discord

def test_discord_group():
    ROUTES.clear()
    ROUTES[("GET", "/guilds/")] = (200, {"id": "111", "name": "Boom Server",
                                         "approximate_member_count": 150})
    a = discord_mod.DiscordChannel(daemon=None, token="tok")
    out = run(a.get_group_info("111"))
    check("discord group parsed", out == {"id": "111", "name": "Boom Server",
                                          "member_count": 150, "type": "server"}, out)
    r = last_req("/guilds/111")
    check("discord group endpoint", r is not None and "with_counts=true" in r["url"], r)


def test_discord_group_forbidden():
    ROUTES.clear()
    ROUTES[("GET", "/guilds/")] = (403, {"message": "Missing Access"})
    a = discord_mod.DiscordChannel(daemon=None, token="tok")
    out = run(a.get_group_info("111"))
    check("discord group forbidden honest",
          "error" in out and "not in guild" in out["error"], out)


def test_discord_member():
    ROUTES.clear()
    ROUTES[("GET", "/members/")] = (200, {
        "nick": "Mod Son", "roles": ["1", "2"], "joined_at": "2024-01-02T03:04:05Z",
        "user": {"id": "777", "username": "sonx", "bot": False}})
    a = discord_mod.DiscordChannel(daemon=None, token="tok")
    out = run(a.get_member_info("111", "777"))
    check("discord member parsed", out == {"id": "777", "name": "Mod Son",
                                           "role": ["1", "2"],
                                           "joined_at": "2024-01-02T03:04:05Z",
                                           "is_bot": False}, out)


def test_discord_member_404():
    ROUTES.clear()
    ROUTES[("GET", "/members/")] = (404, {"message": "Unknown Member"})
    a = discord_mod.DiscordChannel(daemon=None, token="tok")
    out = run(a.get_member_info("111", "999"))
    check("discord stranger 404 honest",
          "error" in out and "not visible" in out["error"], out)


# ------------------------------------------------------------------ 4. slack

def test_slack_group():
    ROUTES.clear()
    ROUTES[("POST", "conversations.info")] = (200, {"ok": True, "channel": {
        "id": "C123", "name": "general", "num_members": 30,
        "is_channel": True}})
    a = slack_mod.SlackAdapter(bot_token="x")
    out = run(a.get_group_info("C123"))
    check("slack group parsed", out == {"id": "C123", "name": "general",
                                        "member_count": 30, "type": "channel"}, out)


def test_slack_group_refused():
    ROUTES.clear()
    ROUTES[("POST", "conversations.info")] = (200, {"ok": False,
                                                    "error": "channel_not_found"})
    a = slack_mod.SlackAdapter(bot_token="x")
    out = run(a.get_group_info("C999"))
    check("slack group ok:false honest",
          "error" in out and "channel_not_found" in out["error"], out)


def test_slack_member():
    ROUTES.clear()
    ROUTES[("POST", "users.info")] = (200, {"ok": True, "user": {
        "id": "U777", "name": "son", "is_admin": True, "is_bot": False,
        "tz": "Asia/Saigon",
        "profile": {"display_name": "Sơn", "real_name": "Son X"}}})
    a = slack_mod.SlackAdapter(bot_token="x")
    out = run(a.get_member_info("C123", "U777"))
    check("slack member parsed", out == {"id": "U777", "name": "Sơn",
                                         "role": "admin", "is_bot": False,
                                         "timezone": "Asia/Saigon"}, out)


def test_slack_member_unknown():
    ROUTES.clear()
    ROUTES[("POST", "users.info")] = (200, {"ok": False, "error": "user_not_found"})
    a = slack_mod.SlackAdapter(bot_token="x")
    out = run(a.get_member_info("C123", "U999"))
    check("slack stranger honest",
          "error" in out and "user_not_found" in out["error"], out)


# ------------------------------------------------------------------ 5. line

def test_line_group():
    ROUTES.clear()
    ROUTES[("GET", "/summary")] = (200, {"groupId": "G1", "groupName": "Nhom LINE"})
    ROUTES[("GET", "/members/count")] = (200, {"count": 12})
    a = line_mod.LineAdapter(channel_token="t")
    out = run(a.get_group_info("G1"))
    check("line group parsed", out == {"id": "G1", "name": "Nhom LINE",
                                       "member_count": 12, "type": "group"}, out)


def test_line_group_refused():
    ROUTES.clear()
    ROUTES[("GET", "/summary")] = (404, {})
    a = line_mod.LineAdapter(channel_token="t")
    out = run(a.get_group_info("G9"))
    check("line group 404 honest", "error" in out and "404" in out["error"], out)


def test_line_member():
    ROUTES.clear()
    ROUTES[("GET", "/member/")] = (200, {"userId": "U1", "displayName": "Son"})
    a = line_mod.LineAdapter(channel_token="t")
    out = run(a.get_member_info("G1", "U1"))
    check("line member parsed", out == {"id": "U1", "name": "Son"}, out)
    r = last_req("/bot/group/G1/member/U1")
    check("line member endpoint", r is not None and r["method"] == "GET", r)


def test_line_member_404():
    ROUTES.clear()
    ROUTES[("GET", "/member/")] = (404, {})
    a = line_mod.LineAdapter(channel_token="t")
    out = run(a.get_member_info("G1", "UX"))
    check("line stranger 404 honest",
          "error" in out and "not visible" in out["error"], out)


# ------------------------------------------------------------------ 6. feishu

def test_feishu_group():
    ROUTES.clear()
    ROUTES[("POST", "tenant_access_token")] = (200, {"tenant_access_token": "ft",
                                                     "expire": 7200})
    ROUTES[("GET", "/chats/")] = (200, {"code": 0, "msg": "success", "data": {
        "chat_id": "oc_1", "name": "Nhom Feishu", "chat_type": "group"}})
    a = feishu_mod.FeishuAdapter(app_id="a", app_secret="s")
    out = run(a.get_group_info("oc_1"))
    check("feishu group parsed", out == {"id": "oc_1", "name": "Nhom Feishu",
                                         "member_count": None, "type": "group"}, out)
    r = last_req("/im/v1/chats/oc_1")
    check("feishu group endpoint", r is not None and r["method"] == "GET", r)


def test_feishu_group_refused():
    ROUTES.clear()
    ROUTES[("POST", "tenant_access_token")] = (200, {"tenant_access_token": "ft",
                                                     "expire": 7200})
    ROUTES[("GET", "/chats/")] = (200, {"code": 23003, "msg": "chat not found"})
    a = feishu_mod.FeishuAdapter(app_id="a", app_secret="s")
    out = run(a.get_group_info("oc_x"))
    check("feishu refused honest",
          "error" in out and "chat not found" in out["error"], out)


def test_feishu_member_unsupported():
    a = feishu_mod.FeishuAdapter(app_id="a", app_secret="s")
    out = run(a.get_member_info("oc_1", "u1"))
    check("feishu member not supported honest",
          "error" in out and "not supported" in out["error"], out)


# ------------------------------------------------------------------ 7. unsupported platforms

def test_unsupported_platforms():
    for mod, name in ((zalo_mod, "zalo"), (messenger_mod, "messenger"),
                      (whatsapp_mod, "whatsapp")):
        cls = next(c for n, c in vars(mod).items()
                   if isinstance(c, type) and issubclass(c, channel_mod.ChannelAdapter)
                   and c is not channel_mod.ChannelAdapter)
        a = cls()
        g = run(a.get_group_info("x"))
        m = run(a.get_member_info("x", "u"))
        check(f"{name} group honest",
              "error" in g and "not supported" in g["error"], g)
        check(f"{name} member honest",
              "error" in m and "not supported" in m["error"], m)


# ------------------------------------------------------------------ 8. registry + daemon dispatch

def test_registry_dispatch():
    ROUTES.clear()
    ROUTES[("POST", "getChat")] = (200, {"ok": True, "result": {
        "id": -5, "title": "G", "type": "group"}})
    ROUTES[("POST", "getChatMemberCount")] = (200, {"ok": True, "result": 3})
    reg = channel_mod.ChannelRegistry()
    reg.register(telegram_mod.TelegramAdapter(token="t"))
    out = run(reg.get_group_info("telegram", "-5"))
    check("registry group dispatch", out["name"] == "G", out)
    out = run(reg.get_member_info("zalo", "-5", "u"))
    check("registry unknown member channel",
          out == {"error": "unknown channel 'zalo'"}, out)


def main():
    test_base_honest()
    test_telegram_group()
    test_telegram_group_no_count()
    test_telegram_group_refused()
    test_telegram_member()
    test_telegram_member_stranger()
    test_discord_group()
    test_discord_group_forbidden()
    test_discord_member()
    test_discord_member_404()
    test_slack_group()
    test_slack_group_refused()
    test_slack_member()
    test_slack_member_unknown()
    test_line_group()
    test_line_group_refused()
    test_line_member()
    test_line_member_404()
    test_feishu_group()
    test_feishu_group_refused()
    test_feishu_member_unsupported()
    test_unsupported_platforms()
    test_registry_dispatch()
    print(f"\nALL GROUPINFO TESTS PASSED ({PASS} checks)")


if __name__ == "__main__":
    main()
