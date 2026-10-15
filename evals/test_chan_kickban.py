"""Offline tests for kick_member / ban_member channel actions.

No network, no real tokens: httpx.AsyncClient is monkeypatched with a
recording fake. Run: python3 evals/test_chan_kickban.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from gateway.channel import ChannelRegistry, build_default_registry  # noqa: E402
from gateway.telegram import TelegramAdapter  # noqa: E402
from gateway.discord import DiscordChannel  # noqa: E402
from gateway.slack import SlackAdapter  # noqa: E402
from gateway.line import LineAdapter  # noqa: E402
from gateway.zalo import ZaloAdapter  # noqa: E402
from gateway.whatsapp import WhatsAppAdapter  # noqa: E402

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


class FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeClient:
    """Drop-in for httpx.AsyncClient: records calls, replays scripted responses."""
    instances = []

    def __init__(self, *a, **k):
        self.calls = []
        self.script = []  # list of (status, body)
        FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw.get("json")))
        if self.script:
            status, body = self.script.pop(0)
        else:
            status, body = 200, {"ok": True}
        return FakeResp(status, body)

    async def post(self, url, **kw):
        return self._next("POST", url, **kw)

    async def put(self, url, **kw):
        return self._next("PUT", url, **kw)

    async def delete(self, url, **kw):
        return self._next("DELETE", url, **kw)

    async def get(self, url, **kw):
        return self._next("GET", url, **kw)


httpx.AsyncClient = FakeClient


def run(coro):
    return asyncio.run(coro)


def last_client():
    return FakeClient.instances[-1]


# ---------------------------------------------------------------- telegram kick
FakeClient.instances.clear()
tg = TelegramAdapter(token="TESTTOKEN")
res = run(tg.kick_member("-100123", "999"))
c = last_client()
check("telegram kick ok", res == {"ok": True}, res)
check("telegram kick: banChatMember first",
      c.calls[0][0] == "POST" and c.calls[0][1].endswith("/banChatMember")
      and c.calls[0][2] == {"chat_id": "-100123", "user_id": "999"}, c.calls[0])
check("telegram kick: unbanChatMember second (rejoin allowed)",
      c.calls[1][0] == "POST" and c.calls[1][1].endswith("/unbanChatMember")
      and c.calls[1][2] == {"chat_id": "-100123", "user_id": "999"}, c.calls[1])

# ---------------------------------------------------------------- telegram ban
FakeClient.instances.clear()
res = run(tg.ban_member("-100123", "999"))
c = last_client()
check("telegram ban ok", res == {"ok": True}, res)
check("telegram ban: single banChatMember call, no unban",
      len(c.calls) == 1 and c.calls[0][1].endswith("/banChatMember"), c.calls)

# ------------------------------------------------------- telegram 403 -> permission
FakeClient.instances.clear()
tg2 = TelegramAdapter(token="TESTTOKEN")
client_holder = {}
orig_init = FakeClient.__init__


def patched_init(self, *a, **k):
    orig_init(self, *a, **k)
    self.script = [(200, {"ok": False, "error_code": 403,
                          "description": "Forbidden: bot is not an administrator"})]


FakeClient.__init__ = patched_init
res = run(tg2.kick_member("-100123", "999"))
FakeClient.__init__ = orig_init
check("telegram 403 -> bot lacks admin permission",
      res == {"error": "bot lacks admin permission"}, res)

# ---------------------------------------------------------------- discord kick
FakeClient.instances.clear()
dc = DiscordChannel(daemon=None, token="DCTOKEN")
res = run(dc.kick_member("111", "222"))
c = last_client()
check("discord kick ok", res == {"ok": True}, res)
check("discord kick: DELETE /guilds/{guild}/members/{user}",
      c.calls[0][0] == "DELETE"
      and c.calls[0][1] == "https://discord.com/api/v10/guilds/111/members/222",
      c.calls[0])

# ---------------------------------------------------------------- discord ban
FakeClient.instances.clear()
res = run(dc.ban_member("111", "222"))
c = last_client()
check("discord ban ok", res == {"ok": True}, res)
check("discord ban: PUT /guilds/{guild}/bans/{user}",
      c.calls[0][0] == "PUT"
      and c.calls[0][1] == "https://discord.com/api/v10/guilds/111/bans/222",
      c.calls[0])

# -------------------------------------------------------- discord 403 -> permission
FakeClient.instances.clear()


def patched_init_403(self, *a, **k):
    orig_init(self, *a, **k)
    self.script = [(403, {"message": "Missing Permissions", "code": 50013})]


FakeClient.__init__ = patched_init_403
res = run(dc.kick_member("111", "222"))
res2 = run(dc.ban_member("111", "222"))
FakeClient.__init__ = orig_init
check("discord kick 403 -> bot lacks admin permission",
      res == {"error": "bot lacks admin permission"}, res)
check("discord ban 403 -> bot lacks admin permission",
      res2 == {"error": "bot lacks admin permission"}, res2)

# ---------------------------------------------------------------- slack kick
FakeClient.instances.clear()
sl = SlackAdapter(bot_token="xoxb-test")
res = run(sl.kick_member("C123", "U456"))
c = last_client()
check("slack kick ok", res == {"ok": True}, res)
check("slack kick: conversations.kick payload",
      c.calls[0][1].endswith("/conversations.kick")
      and c.calls[0][2] == {"channel": "C123", "user": "U456"}, c.calls[0])

# ---------------------------------------------------------- slack not_admin
FakeClient.instances.clear()


def patched_init_slack(self, *a, **k):
    orig_init(self, *a, **k)
    self.script = [(200, {"ok": False, "error": "not_admin"})]


FakeClient.__init__ = patched_init_slack
res = run(sl.kick_member("C123", "U456"))
FakeClient.__init__ = orig_init
check("slack not_admin -> bot lacks admin permission",
      res == {"error": "bot lacks admin permission"}, res)

# ---------------------------------------------------------- slack ban: no API
res = run(sl.ban_member("C123", "U456"))
check("slack ban -> honest not-supported error",
      res == {"error": "slack: banning members not supported by this platform"}, res)

# ---------------------------------------------------------- unsupported platforms
for adapter, label in [(LineAdapter(), "line"), (ZaloAdapter(), "zalo"),
                       (WhatsAppAdapter(), "whatsapp")]:
    rk = run(adapter.kick_member("t", "u"))
    rb = run(adapter.ban_member("t", "u"))
    check(f"{label} kick -> honest error",
          rk == {"error": f"{label}: kicking members not supported by this platform"}, rk)
    check(f"{label} ban -> honest error",
          rb == {"error": f"{label}: banning members not supported by this platform"}, rb)

# ---------------------------------------------------------- registry dispatch
FakeClient.instances.clear()
reg = ChannelRegistry()
reg.register(TelegramAdapter(token="T"))
reg.register(LineAdapter())
res = run(reg.kick_member("telegram", "-1001", "42"))
check("registry kick_member dispatches to telegram", res == {"ok": True}, res)
res = run(reg.ban_member("telegram", "-1001", "42"))
check("registry ban_member dispatches to telegram", res == {"ok": True}, res)
res = run(reg.kick_member("line", "t", "u"))
check("registry kick_member line -> honest error", "not supported" in res.get("error", ""), res)
res = run(reg.kick_member("nope", "t", "u"))
check("registry kick_member unknown channel",
      res == {"error": "unknown channel 'nope'"}, res)
res = run(reg.ban_member("nope", "t", "u"))
check("registry ban_member unknown channel",
      res == {"error": "unknown channel 'nope'"}, res)

# ---------------------------------------------------------- old methods intact
FakeClient.instances.clear()
res = run(tg.react("-1001", "7", "❤️"))
check("telegram react still works", res == {"ok": True}, res)
res = run(dc.delete_message("99", "88"))
check("discord delete still works", res == {"ok": True}, res)
res = run(sl.delete_message("C1", "123.456"))
check("slack delete still works", res == {"ok": True}, res)

# ---------------------------------------------------------------- summary
failed = [n for n, ok, _ in CHECKS if not ok]
print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
print("ALL KICK/BAN TESTS PASSED")
