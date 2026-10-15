"""Tests for agent/src/audit/. Offline, no credentials."""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent", "src"))

from audit.models import AuditEvent
from audit.store import AuditStore
from audit.middleware import audit_wrap
from audit.tools import build_tools

PASS = 0
FAIL = 0

def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL:", name)

# ---------- models ----------
e = AuditEvent(actor="u1", action="channel.send", target="c1", channel="telegram",
               details={"text": "hi"}, result="ok")
d = e.to_dict()
e2 = AuditEvent.from_dict(d)
check("models round-trip", e2.actor == "u1" and e2.action == "channel.send"
      and e2.details == {"text": "hi"} and e2.result == "ok")
e3 = AuditEvent.from_dict({"actor": "x", "details": '{"a": 1}'})
check("models details json str", e3.details == {"a": 1})

# ---------- store ----------
tmp = tempfile.mkdtemp()
os.makedirs(os.path.join(tmp, "memories"), exist_ok=True)
store = AuditStore(os.path.join(tmp, "memories", "audit.db"))

id1 = store.log(AuditEvent(actor="alice", action="channel.send", target="g1",
                           channel="telegram", details={"text": "hello"}))
id2 = store.log(AuditEvent(actor="bob", action="channel.kick", target="g1",
                           channel="telegram", result="ok"))
id3 = store.log(AuditEvent(actor="alice", action="trigger.create", target="t1",
                           channel="", result="error",
                           details={"error": "bad kind"}))
check("store log returns id", id1.startswith("aud_") and id1 != id2)
check("store count", store.count() == 3)

got = store.get(id1)
check("store get", got is not None and got.actor == "alice" and got.details == {"text": "hello"})
check("store get missing", store.get("aud_nope") is None)

check("query by actor", len(store.query(actor="alice")) == 2)
check("query by action", len(store.query(action="channel.kick")) == 1
      and store.query(action="channel.kick")[0].actor == "bob")
check("query by channel", len(store.query(channel="telegram")) == 2)
check("query all", len(store.query()) == 3)
check("query limit", len(store.query(limit=2)) == 2)

# time range: use ts of first event
ts1 = store.get(id1).ts
check("query since", len(store.query(since=ts1)) == 3)
check("query until far past", len(store.query(until="2000-01-01T00:00:00")) == 0)

# append-only: no delete/update API
check("no delete method", not hasattr(store, "delete"))
check("no update method", not hasattr(store, "update"))

def endswith_value_error(store, eid):
    try:
        store.log(AuditEvent(id=eid, actor="x", action="y"))
        return False
    except ValueError:
        return True

check("duplicate id raises", endswith_value_error(store, id1))

# events survive "restart" (new store object, same file)
store2 = AuditStore(os.path.join(tmp, "memories", "audit.db"))
check("persist across reopen", store2.count() == 3)

# ---------- middleware: sync ----------
s3 = AuditStore(os.path.join(tmp, "m.db"))

def add(a, b):
    return a + b

wrapped = audit_wrap(add, "test.add", lambda: s3, actor_getter=lambda: "tester",
                     target_arg="a")
check("middleware sync result", wrapped(2, 3) == 5)
evs = s3.query(action="test.add")
check("middleware sync logged", len(evs) == 1 and evs[0].result == "ok"
      and evs[0].actor == "tester" and evs[0].target == "2")

def boom():
    raise RuntimeError("kaboom")

wrapped_boom = audit_wrap(boom, "test.boom", lambda: s3)
try:
    wrapped_boom()
    check("middleware sync raise propagates", False)
except RuntimeError:
    check("middleware sync raise propagates", True)
err_evs = [x for x in s3.query(action="test.boom") if x.result == "error"]
check("middleware sync error logged", len(err_evs) == 1 and "kaboom" in str(err_evs[0].details))

# store failure must not break the wrapped function
def bad_store():
    raise OSError("disk gone")

wrapped_fragile = audit_wrap(add, "test.fragile", bad_store)
check("middleware survives store failure", wrapped_fragile(1, 2) == 3)

# ---------- middleware: async ----------
s4 = AuditStore(os.path.join(tmp, "ma.db"))

async def asend(channel, target, text):
    return {"ok": True}

awrapped = audit_wrap(asend, "channel.send", lambda: s4,
                      actor_getter=lambda: "bot",
                      target_arg="target", channel_arg="channel")

async def afail():
    raise ValueError("nope")
awrapped_fail = audit_wrap(afail, "channel.send", lambda: s4)

async def run_async_tests():
    out = await awrapped(channel="telegram", target="g9", text="hi")
    ok1 = out == {"ok": True}
    evs = s4.query(action="channel.send")
    ok2 = len(evs) == 1 and evs[0].result == "ok" and evs[0].channel == "telegram" \
        and evs[0].target == "g9"
    try:
        await awrapped_fail()
        ok3 = False
    except ValueError:
        ok3 = True
    ok4 = any(x.result == "error" for x in s4.query(action="channel.send"))
    return ok1, ok2, ok3, ok4

r = asyncio.run(run_async_tests())
check("middleware async result", r[0])
check("middleware async logged", r[1])
check("middleware async raise propagates", r[2])
check("middleware async error logged", r[3])

# ---------- tools ----------
tools = build_tools(tmp)
check("tools keys", set(tools.keys()) == {"audit_query", "audit_get"})

q = tools["audit_query"]["func"](actor="alice")
check("tool query", q.get("count") == 2 and len(q["events"]) == 2)
q2 = tools["audit_query"]["func"](action="channel.kick")
check("tool query action", q2.get("count") == 1)
g = tools["audit_get"]["func"](id1)
check("tool get", g.get("event", {}).get("id") == id1)
check("tool get missing", "error" in tools["audit_get"]["func"]("aud_nope"))
check("tool get empty", "error" in tools["audit_get"]["func"](""))
check("tool no root", "error" in build_tools(None)["audit_query"]["func"]())

print("=" * 40)
print(f"PASS: {PASS}  FAIL: {FAIL}")
sys.exit(1 if FAIL else 0)
