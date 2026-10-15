"""Offline tests for gateway/broadcast.py — no network, no tokens."""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gateway"))

from broadcast import (  # noqa: E402
    BroadcastStore,
    broadcast,
    build_tools,
)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok:", name)
    else:
        FAIL += 1
        print("  FAIL:", name, "-", detail)


def run(coro):
    return asyncio.run(coro)


# ---------- core broadcast ----------

async def fake_send_ok(channel, target, text):
    return True


async def fake_send_mixed(channel, target, text):
    if target == "bad":
        return False
    return True


async def fake_send_raise(channel, target, text):
    if target == "boom":
        raise RuntimeError("connection lost")
    return True


def test_mixed_results():
    targets = [
        {"channel": "telegram", "target": "111"},
        {"channel": "telegram", "target": "bad"},
        {"channel": "discord", "target": "222"},
    ]
    r = run(broadcast(fake_send_mixed, targets, "hello", delay_sec=0))
    check("mixed: sent==2", r["sent"] == 2, r)
    check("mixed: failed==1", r["failed"] == 1, r)
    check("mixed: 3 results", len(r["results"]) == 3, r)
    bad = [x for x in r["results"] if x["target"] == "bad"][0]
    check("mixed: bad has error", bad["ok"] is False and "error" in bad, bad)


def test_exception_does_not_stop_rest():
    targets = [
        {"channel": "telegram", "target": "boom"},
        {"channel": "telegram", "target": "after"},
    ]
    r = run(broadcast(fake_send_raise, targets, "hi", delay_sec=0))
    check("raise: sent==1", r["sent"] == 1, r)
    check("raise: failed==1", r["failed"] == 1, r)
    first = r["results"][0]
    check("raise: error recorded", first["ok"] is False and "RuntimeError" in first["error"], first)
    check("raise: second still sent", r["results"][1]["ok"] is True, r["results"])


def test_delay_respected_with_fake_clock():
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    targets = [{"channel": "t", "target": str(i)} for i in range(3)]
    r = run(broadcast(fake_send_ok, targets, "x", delay_sec=2.5, sleep=fake_sleep))
    check("delay: 2 pauses for 3 targets", sleeps == [2.5, 2.5], sleeps)
    check("delay: all sent", r["sent"] == 3, r)


def test_no_delay_after_last():
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    run(broadcast(fake_send_ok, [{"channel": "t", "target": "1"}], "x",
                  delay_sec=5, sleep=fake_sleep))
    check("delay: no pause for single target", sleeps == [], sleeps)


def test_validation():
    r = run(broadcast(fake_send_ok, [], "x"))
    check("empty targets -> error", "error" in r, r)
    r = run(broadcast(fake_send_ok, [{"channel": "t"}], "x"))
    check("missing target -> error", "error" in r, r)
    r = run(broadcast(fake_send_ok, [{"channel": "t", "target": "1"}], "   "))
    check("empty text -> error", "error" in r, r)
    r = run(broadcast(fake_send_ok, "notalist", "x"))
    check("non-list targets -> error", "error" in r, r)


# ---------- store ----------

def test_store():
    with tempfile.TemporaryDirectory() as d:
        s = BroadcastStore(Path(d) / "b.db")
        targets = [{"channel": "telegram", "target": "111"}]
        bid = s.record("hello", targets, 1, 0, created_by="tester")
        check("store: record returns id", bid.startswith("bc_"), bid)
        got = s.get(bid)
        check("store: get round-trip", got and got["text"] == "hello"
              and got["targets"] == targets and got["sent_count"] == 1, got)
        check("store: missing -> None", s.get("nope") is None)
        s.record("second", targets, 0, 1)
        h = s.history(limit=10)
        check("store: history has 2", len(h) == 2, h)
        check("store: newest first", h[0]["text"] == "second", h[0])
        check("store: limit respected", len(s.history(limit=1)) == 1)


# ---------- build_tools ----------

class FakeDaemon:
    def __init__(self, fail_targets=()):
        self.calls = []
        self.fail_targets = set(fail_targets)

    async def send_to(self, channel, target, text):
        self.calls.append((channel, target, text))
        return target not in self.fail_targets


class FakeEnv:
    def __init__(self, root_dir, daemon=None):
        self.root_dir = root_dir
        self.daemon = daemon


def test_tools_send_and_history():
    with tempfile.TemporaryDirectory() as d:
        daemon = FakeDaemon(fail_targets={"bad"})
        tools = build_tools(FakeEnv(d, daemon))
        check("tools: 2 tools", set(tools) == {"broadcast_send", "broadcast_history"}, set(tools))
        send = tools["broadcast_send"]["func"]
        r = send([{"channel": "telegram", "target": "111"},
                  {"channel": "telegram", "target": "bad"}],
                 "announcement", delay_sec=0, created_by="tester")
        check("tool send: sent==1 failed==1", r["sent"] == 1 and r["failed"] == 1, r)
        check("tool send: broadcast_id", "broadcast_id" in r, r)
        check("tool send: daemon got 2 calls", len(daemon.calls) == 2, daemon.calls)
        hist = tools["broadcast_history"]["func"](limit=5)
        check("tool history: 1 row", len(hist["broadcasts"]) == 1, hist)
        check("tool history: created_by", hist["broadcasts"][0]["created_by"] == "tester",
              hist["broadcasts"][0])


def test_tools_dry_run():
    with tempfile.TemporaryDirectory() as d:
        daemon = FakeDaemon()
        tools = build_tools(FakeEnv(d, daemon))
        r = tools["broadcast_send"]["func"](
            [{"channel": "telegram", "target": "111"},
             {"channel": "discord", "target": "222"}],
            "preview", dry_run=True)
        check("dry_run: flagged", r.get("dry_run") is True, r)
        check("dry_run: count==2", r.get("count") == 2, r)
        check("dry_run: daemon not called", daemon.calls == [], daemon.calls)
        check("dry_run: not in history",
              tools["broadcast_history"]["func"]()["broadcasts"] == [])


def test_tools_no_daemon():
    with tempfile.TemporaryDirectory() as d:
        tools = build_tools(FakeEnv(d, None))
        r = tools["broadcast_send"]["func"](
            [{"channel": "telegram", "target": "111"}], "x", delay_sec=0)
        check("no daemon -> error", "error" in r and "send_to" in r["error"], r)


def test_tools_no_root():
    tools = build_tools(None)
    r = tools["broadcast_send"]["func"](
        [{"channel": "telegram", "target": "111"}], "x", dry_run=False)
    # dry_run=False needs a store -> error about root_dir
    check("no root -> error", "error" in r, r)


def test_run_async_inside_loop():
    # _run_async must also work when called from inside a running loop.
    from broadcast import _run_async

    async def outer():
        async def inner():
            return 42
        return _run_async(inner())

    check("_run_async inside loop", run(outer()) == 42)


if __name__ == "__main__":
    print("== broadcast core ==")
    test_mixed_results()
    test_exception_does_not_stop_rest()
    test_delay_respected_with_fake_clock()
    test_no_delay_after_last()
    test_validation()
    print("== store ==")
    test_store()
    print("== tools ==")
    test_tools_send_and_history()
    test_tools_dry_run()
    test_tools_no_daemon()
    test_tools_no_root()
    test_run_async_inside_loop()
    print("=" * 40)
    print(f"PASS: {PASS}  FAIL: {FAIL}")
    sys.exit(1 if FAIL else 0)
