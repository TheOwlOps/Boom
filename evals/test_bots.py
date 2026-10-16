"""Tests for bots.BotManager. Stdlib only — run with: python3 test_bots.py"""

from __future__ import annotations

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent", "src", "business"))
from bots import BotManager

PASSED = 0


def check(cond, label):
    global PASSED
    assert cond, "FAILED: %s" % label
    PASSED += 1
    print("ok - %s" % label)


def fresh_db():
    fd, path = tempfile.mkstemp(prefix="bots_test_", suffix=".db")
    os.close(fd)
    os.unlink(path)  # let BotManager create it fresh
    return path


TOOLS = ["web_search", "shell_run", "file_read", "gmail_send", "memory_save"]

# ------------------------------------------------------------------ CREATE
db = fresh_db()
mgr = BotManager(db)

r = mgr.create_bot(name="Jarvis", description="Personal assistant",
                   system_prompt="You are Jarvis.",
                   avatar_emoji="🧠",
                   allowed_tools=["web_search", "file_read"],
                   denied_tools=["shell_run"],
                   channel_bindings=["telegram"])
check(r.get("ok") is True, "create_bot returns ok")
bot_a = r["bot"]
check(bot_a["name"] == "Jarvis", "create stores name")
check(bot_a["description"] == "Personal assistant", "create stores description")
check(bot_a["system_prompt"] == "You are Jarvis.", "create stores system_prompt")
check(bot_a["avatar_emoji"] == "🧠", "create stores avatar_emoji")
check(bot_a["allowed_tools"] == ["web_search", "file_read"],
      "create stores allowed_tools")
check(bot_a["denied_tools"] == ["shell_run"], "create stores denied_tools")
check(bot_a["channel_bindings"] == ["telegram"],
      "create stores channel_bindings")
check(bot_a["is_active"] is False, "new bot is not active by default")
check(bool(bot_a["id"]) and isinstance(bot_a["id"], str), "create assigns id")

r2 = mgr.create_bot(name="Plain")
bot_b = r2["bot"]
check(r2.get("ok") is True, "create with defaults ok")
check(bot_b["avatar_emoji"] == "🤖", "default avatar_emoji is robot")
check(bot_b["description"] == "" and bot_b["system_prompt"] == "",
      "defaults for description/system_prompt are empty")
check(bot_b["allowed_tools"] == [] and bot_b["denied_tools"] == []
      and bot_b["channel_bindings"] == [], "defaults for lists are empty")

check("error" in mgr.create_bot(name=""), "empty name rejected")
check("error" in mgr.create_bot(name="   "), "blank name rejected")
check("error" in mgr.create_bot(name=123), "non-string name rejected")
check("error" in mgr.create_bot(name="x", allowed_tools="web_search"),
      "non-list allowed_tools rejected")

# ------------------------------------------------------------------ LIST/GET
bots = mgr.list_bots()
check(len(bots) == 2, "list_bots returns both bots")
check({b["id"] for b in bots} == {bot_a["id"], bot_b["id"]},
      "list_bots ids match")

got = mgr.get_bot(bot_a["id"])
check(got is not None and got["name"] == "Jarvis", "get_bot returns the bot")
check(mgr.get_bot("does-not-exist") is None, "get_bot missing -> None")
check(mgr.get_bot("") is None, "get_bot blank id -> None")
check(mgr.get_bot(None) is None, "get_bot None -> None")

# ------------------------------------------------------------------ UPDATE
u = mgr.update_bot(bot_a["id"], description="Updated desc")
check(u.get("ok") is True, "update returns ok")
check(u["bot"]["description"] == "Updated desc",
      "partial update changes only description")
check(u["bot"]["name"] == "Jarvis", "partial update keeps name")
check(u["bot"]["allowed_tools"] == ["web_search", "file_read"],
      "partial update keeps allowed_tools")

u2 = mgr.update_bot(bot_a["id"], name="Jarvis 2",
                    allowed_tools=["memory_save", "memory_save"],
                    denied_tools=[], unknown_field="ignored")
check(u2.get("ok") is True, "update multiple fields ok")
check(u2["bot"]["name"] == "Jarvis 2", "name updated")
check(u2["bot"]["allowed_tools"] == ["memory_save"],
      "allowed_tools updated + deduplicated")
check("unknown_field" not in u2["bot"], "unknown field ignored, not stored")

e = mgr.update_bot("does-not-exist", name="x")
check("error" in e, "update missing bot -> error")
check("error" in mgr.update_bot(bot_a["id"], name=""),
      "update with empty name -> error")
check("error" in mgr.update_bot(bot_a["id"]),
      "update with no fields -> error")
check("error" in mgr.update_bot(bot_a["id"], allowed_tools="nope"),
      "update with bad list -> error")
check("error" in mgr.update_bot(bot_a["id"], is_active=1),
      "is_active not updatable -> error")

# ------------------------------------------------------------------ ACTIVE
check(mgr.get_active_bot() is None, "no active bot initially")
check(mgr.set_active_bot(bot_a["id"]) is True, "set_active_bot ok")
act = mgr.get_active_bot()
check(act is not None and act["id"] == bot_a["id"],
      "get_active_bot returns the active bot")
check(mgr.list_bots()[0]["id"] == bot_a["id"],
      "active bot sorts first in list_bots")

check(mgr.set_active_bot(bot_b["id"]) is True, "switch active bot ok")
check(mgr.get_active_bot()["id"] == bot_b["id"],
      "active bot switched")
check(mgr.get_bot(bot_a["id"])["is_active"] is False,
      "previous bot no longer active")
check(mgr.set_active_bot("does-not-exist") is False,
      "set_active_bot missing -> False")
check(mgr.get_active_bot()["id"] == bot_b["id"],
      "failed set_active keeps current active bot")

# ------------------------------------------------------------------ EFFECTIVE TOOLS
eff = mgr.get_effective_tools(bot_a["id"], TOOLS)
check(eff == ["memory_save"],
      "allowed ∩ all minus denied = [memory_save]")

r3 = mgr.create_bot(name="AllowDeny", allowed_tools=["web_search", "shell_run",
                                                     "ghost_tool"],
                    denied_tools=["shell_run"])
eff2 = mgr.get_effective_tools(r3["bot"]["id"], TOOLS)
check(eff2 == ["web_search"],
      "allowed∩all minus denied; unknown tool names dropped")

r4 = mgr.create_bot(name="Open", denied_tools=["gmail_send"])
eff3 = mgr.get_effective_tools(r4["bot"]["id"], TOOLS)
check(eff3 == ["web_search", "shell_run", "file_read", "memory_save"],
      "empty allowed = all tools, denied removed")
check(mgr.get_effective_tools("does-not-exist", TOOLS) == TOOLS,
      "unknown bot -> fail-open with all tools")

# ------------------------------------------------------------------ DELETE
check(mgr.delete_bot("does-not-exist") is False,
      "delete missing -> False")
check(mgr.delete_bot("") is False, "delete blank id -> False")
check(mgr.delete_bot(bot_a["id"]) is True, "delete existing -> True")
check(mgr.get_bot(bot_a["id"]) is None, "deleted bot is gone")
check(len(mgr.list_bots()) == 3, "list shrinks after delete")

# deleting the active bot leaves no active bot
check(mgr.get_active_bot() is not None, "active bot still set before delete")
check(mgr.delete_bot(bot_b["id"]) is True, "delete active bot -> True")
check(mgr.get_active_bot() is None,
      "no active bot after active bot deleted")

# ------------------------------------------------------------------ PERSISTENCE
r5 = mgr.create_bot(name="Persisted", system_prompt="stay")
mgr.set_active_bot(r5["bot"]["id"])
mgr2 = BotManager(db)  # reopen the same file
restored = mgr2.get_bot(r5["bot"]["id"])
check(restored is not None and restored["system_prompt"] == "stay",
      "reopen: bot persisted")
check(mgr2.get_active_bot()["id"] == r5["bot"]["id"],
      "reopen: active bot persisted")
check(len(mgr2.list_bots()) == len(mgr.list_bots()),
      "reopen: same bot count")

# ------------------------------------------------------------------ THREAD SAFETY
errors = []


def worker(n):
    try:
        for i in range(20):
            rr = mgr.create_bot(name="t%d-%d" % (n, i))
            assert rr.get("ok") is True
            mgr.list_bots()
    except Exception as e:  # pragma: no cover
        errors.append(e)


threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check(not errors, "concurrent create/list from 4 threads -> no errors")

os.unlink(db)
print("\n%d assertions PASSED" % PASSED)
