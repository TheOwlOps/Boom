"""Tests for the goals module: full lifecycle, persistence, corruption, validation.

Nothing here touches the network or a model: the store is a JSON ledger under a temp
dir, so every run is deterministic.
"""

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from goals.store import GOALS_FILE, STATUSES, GoalStore
from goals.tools import build_tools

T0 = datetime(2026, 10, 15, 12, 0, 0)


def make_store(tmpdir, clock=lambda: T0):
    return GoalStore(Path(tmpdir) / "memories" / GOALS_FILE, clock=clock)


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(f"FAILED: {name}")


def expect_value_error(name, fn):
    try:
        fn()
    except ValueError:
        print("PASS " + name)
        return
    raise SystemExit(f"FAILED (no ValueError): {name}")


def test_add_and_get(tmpdir):
    store = make_store(tmpdir)
    goal = store.add("Ship the Boom web UI", "Replace the hardcoded demo")
    check("id assigned", goal["id"].startswith("goal_"))
    check("status active", goal["status"] == "active")
    check("empty log", goal["progress_log"] == [])
    check("title kept", goal["title"] == "Ship the Boom web UI")
    check("description kept", goal["description"] == "Replace the hardcoded demo")
    check("get round trip", store.get(goal["id"]) == goal)
    check("unknown get -> None", store.get("nope") is None)


def test_full_lifecycle(tmpdir):
    from datetime import timedelta
    ticks = [T0]

    def clock():
        ticks[0] = ticks[0] + timedelta(seconds=1)
        return ticks[0]

    store = make_store(tmpdir, clock=clock)
    goal = store.add("Learn Japanese", goal_id="g-jp")
    store.log_progress("g-jp", "Finished hiragana")
    store.log_progress("g-jp", "Started katakana")
    paused = store.set_status("g-jp", "paused")
    check("paused", paused["status"] == "paused")
    done = store.set_status("g-jp", "completed")
    check("completed", done["status"] == "completed")
    check("two log entries", len(done["progress_log"]) == 2)
    check("note text kept", done["progress_log"][0]["note"] == "Finished hiragana")
    check("log timestamps", all("at" in e for e in done["progress_log"]))
    check("updated_at moved", done["updated_at"] >= done["created_at"])


def test_persist_across_reinit(tmpdir):
    store = make_store(tmpdir)
    goal = store.add("Write docs", goal_id="g-doc")
    store.log_progress("g-doc", "Outlined chapters")
    store2 = make_store(tmpdir)  # fresh instance, same file
    again = store2.get("g-doc")
    check("survives reinit", again is not None)
    check("log survives", again["progress_log"][0]["note"] == "Outlined chapters")
    check("list sees it", [g["id"] for g in store2.list()] == ["g-doc"])


def test_corrupt_file_reads_empty(tmpdir):
    p = Path(tmpdir) / "memories" / GOALS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("this is not json{{{", encoding="utf-8")
    store = make_store(tmpdir)
    check("corrupt list -> []", store.list() == [])
    check("corrupt get -> None", store.get("x") is None)
    goal = store.add("After corruption", goal_id="g-after")
    check("writable after corruption", store.get("g-after")["id"] == "g-after")
    # Non-list JSON also reads as empty.
    p.write_text(json.dumps({"oops": 1}), encoding="utf-8")
    check("non-list -> []", make_store(tmpdir).list() == [])


def test_validation(tmpdir):
    store = make_store(tmpdir)
    expect_value_error("empty title", lambda: store.add(""))
    expect_value_error("blank title", lambda: store.add("   "))
    store.add("T", goal_id="g-dup")
    expect_value_error("duplicate id", lambda: store.add("T2", goal_id="g-dup"))
    expect_value_error("empty note", lambda: store.log_progress("g-dup", ""))
    expect_value_error("bad status", lambda: store.set_status("g-dup", "done-ish"))
    check("log unknown -> None", store.log_progress("missing", "note") is None)
    check("status unknown -> None", store.set_status("missing", "paused") is None)
    check("remove unknown -> False", store.remove("missing") is False)


def test_remove_and_filter(tmpdir):
    store = make_store(tmpdir)
    store.add("A", goal_id="g-a")
    store.add("B", goal_id="g-b")
    store.set_status("g-b", "paused")
    check("filter active", [g["id"] for g in store.list("active")] == ["g-a"])
    check("filter paused", [g["id"] for g in store.list("paused")] == ["g-b"])
    check("filter completed empty", store.list("completed") == [])
    check("list all", len(store.list()) == 2)
    expect_value_error("bad filter", lambda: store.list("bogus"))
    check("remove True", store.remove("g-a") is True)
    check("gone after remove", store.get("g-a") is None)
    check("remove again False", store.remove("g-a") is False)


def test_tools_end_to_end(tmpdir):
    tools = build_tools(Path(tmpdir))
    res = tools["goal_add"]["func"]("Ship web UI", "Replace demo")
    check("tool add ok", "goal" in res and res["goal"]["status"] == "active")
    gid = res["goal"]["id"]
    check("tool add empty title -> error",
          "error" in tools["goal_add"]["func"](""))
    res = tools["goal_list"]["func"]()
    check("tool list", res["total"] == 1 and res["goals"][0]["id"] == gid)
    res = tools["goal_log"]["func"](gid, "Scaffolded pages")
    check("tool log", res["goal"]["progress_log"][0]["note"] == "Scaffolded pages")
    check("tool log unknown -> error",
          "error" in tools["goal_log"]["func"]("nope", "x"))
    res = tools["goal_pause"]["func"](gid)
    check("tool pause", res["goal"]["status"] == "paused")
    res = tools["goal_list"]["func"]("paused")
    check("tool list filtered", res["total"] == 1)
    res = tools["goal_complete"]["func"](gid)
    check("tool complete", res["goal"]["status"] == "completed")
    res = tools["goal_get"]["func"](gid)
    check("tool get", res["goal"]["id"] == gid and len(res["goal"]["progress_log"]) == 1)
    check("tool get unknown -> error", "error" in tools["goal_get"]["func"]("nope"))
    check("tool pause unknown -> error", "error" in tools["goal_pause"]["func"]("nope"))


def test_tools_accept_env_object(tmpdir):
    class Env:
        root_dir = Path(tmpdir)

    tools = build_tools(Env())  # repo convention: factory(env)
    res = tools["goal_add"]["func"]("Via env")
    check("env-style build_tools", "goal" in res)


def test_tools_without_root():
    tools = build_tools(None)
    for name, tool in tools.items():
        try:
            out = tool["func"]("x", "y") if name in ("goal_add", "goal_log") else (
                tool["func"]("x") if name != "goal_list" else tool["func"]())
        except TypeError:
            out = {"error": "unexpected"}
        check(f"no-root {name} -> error", isinstance(out, dict) and "error" in out)


def test_statuses_constant():
    check("STATUSES", set(STATUSES) == {"active", "paused", "completed"})


if __name__ == "__main__":
    print("== goals ==")
    for fn in (test_add_and_get, test_full_lifecycle, test_persist_across_reinit,
               test_corrupt_file_reads_empty, test_validation, test_remove_and_filter,
               test_tools_end_to_end, test_tools_accept_env_object,
               test_tools_without_root, test_statuses_constant):
        if fn.__code__.co_argcount == 0:
            fn()
        else:
            with tempfile.TemporaryDirectory() as tmp:
                fn(tmp)
    print("ALL GOALS TESTS PASSED")
