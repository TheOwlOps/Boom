"""Offline tests for the people ledger: store semantics, tools, persistence.

Nothing here touches the network: the store is pointed at a temp dir and the
clock is fixed, so every run is deterministic.
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "agent" / "src"
sys.path.insert(0, str(SRC))

from people.store import PEOPLE_FILE, PeopleStore, _people_path
from people.tools import build_tools

T0 = datetime(2026, 10, 15, 10, 0, 0)


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


def make_store(tmpdir):
    return PeopleStore(Path(tmpdir) / "memories" / PEOPLE_FILE, clock=lambda: T0)


def test_upsert_add_note_get_search(tmpdir):
    store = make_store(tmpdir)
    p = store.upsert("Son", relation="chủ nhân", note="thích cà phê")
    check("upsert returns person", p["name"] == "Son" and p["relation"] == "chủ nhân")
    check("first note stored", p["notes"] == ["thích cà phê"])

    p = store.add_note("SON", "làm việc ở Hà Nội")
    check("add_note appends (case-insensitive)", p["notes"] == ["thích cà phê", "làm việc ở Hà Nội"])

    got = store.get("son")
    check("get case-insensitive", got is not None and got["name"] == "Son")
    check("relation kept", got["relation"] == "chủ nhân")

    check("search by note", [x["name"] for x in store.search("cà phê")] == ["Son"])
    check("search by name fragment", [x["name"] for x in store.search("SON")] == ["Son"])
    check("search no hit", store.search("không tồn tại") == [])


def test_upsert_updates_existing(tmpdir):
    store = make_store(tmpdir)
    store.upsert("Lan", relation="bạn", note="ghi chú 1")
    p = store.upsert("lan", relation="đồng nghiệp", note="ghi chú 2")
    check("relation replaced", p["relation"] == "đồng nghiệp")
    check("notes accumulated", p["notes"] == ["ghi chú 1", "ghi chú 2"])
    check("one record only", len(store.list()) == 1)
    check("display name keeps first form", store.list()[0]["name"] == "Lan")


def test_persist_across_reinit(tmpdir):
    store = make_store(tmpdir)
    store.upsert("Son", relation="chủ nhân", note="ghi chú A")
    store.add_note("son", "ghi chú B")
    fresh = make_store(tmpdir)
    got = fresh.get("SON")
    check("persisted", got is not None and got["notes"] == ["ghi chú A", "ghi chú B"])
    check("ledger file written", (Path(tmpdir) / "memories" / PEOPLE_FILE).exists())


def test_corrupt_file_does_not_crash(tmpdir):
    p = Path(tmpdir) / "memories" / PEOPLE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json", encoding="utf-8")
    store = make_store(tmpdir)
    check("corrupt reads as empty", store.list() == [])
    check("get on corrupt is None", store.get("ai đó") is None)
    store.upsert("Minh", relation="bạn")
    check("writable after corrupt", store.get("minh")["relation"] == "bạn")


def test_validation(tmpdir):
    store = make_store(tmpdir)
    expect_value_error("upsert blank name", lambda: store.upsert("   "))
    expect_value_error("upsert empty name", lambda: store.upsert(""))
    expect_value_error("add_note blank name", lambda: store.add_note("  ", "x"))
    expect_value_error("add_note unknown person", lambda: store.add_note("Người Lạ", "x"))
    expect_value_error("add_note blank note", lambda: store.upsert("Ok", note="  "))
    expect_value_error("get blank name", lambda: store.get(""))


def test_tools(tmpdir):
    tools = build_tools(Path(tmpdir))
    check("five tools", sorted(tools) == ["person_add", "person_get", "person_list",
                                          "person_note", "person_search"])

    res = tools["person_add"]["func"]("Hà", relation="gia đình", note="em gái")
    check("person_add ok", res["person"]["name"] == "Hà")

    res = tools["person_note"]["func"]("hà", "sinh năm 2000")
    check("person_note ok", res["person"]["notes"] == ["em gái", "sinh năm 2000"])

    res = tools["person_get"]["func"]("HÀ")
    check("person_get case-insensitive", res["person"]["relation"] == "gia đình")

    res = tools["person_list"]["func"]()
    check("person_list", res["total"] == 1)

    res = tools["person_search"]["func"]("2000")
    check("person_search", res["total"] == 1)

    res = tools["person_note"]["func"]("Người Lạ", "x")
    check("person_note unknown -> error", "error" in res)
    res = tools["person_add"]["func"]("  ")
    check("person_add blank -> error", "error" in res)
    res = tools["person_get"]["func"]("Không Ai")
    check("person_get unknown -> error", "error" in res)


def test_no_root_dir(tmpdir):
    tools = build_tools(None)
    res = tools["person_list"]["func"]()
    check("no root_dir -> error", "error" in res)


if __name__ == "__main__":
    print("== people module ==")
    print("ledger file:", PEOPLE_FILE, "| default path:", _people_path("/root"))
    for fn in (test_upsert_add_note_get_search, test_upsert_updates_existing,
               test_persist_across_reinit, test_corrupt_file_does_not_crash,
               test_validation, test_tools, test_no_root_dir):
        with tempfile.TemporaryDirectory() as tmp:
            fn(tmp)
    print("ALL PEOPLE TESTS PASSED")
