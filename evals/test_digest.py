"""Tests for the digest module: briefing markdown and the digest_preview tool."""

import json
import sys
from datetime import date
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from digest import build_briefing, build_tools, empty_briefing

DAY = date(2026, 10, 15)  # a Thursday


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"FAILED: {name}" + (f": {detail}" if detail else ""))


def test_full_sections_format():
    out = build_briefing(
        {
            "goals": ["Hoàn thành báo cáo tuần", "Chạy 5km"],
            "people": ["Nhớ chúc mừng sinh nhật Lan"],
            "reminders": ["9h họp team", "18h đón con"],
            "notes": ["Ý tưởng app mới"],
        },
        on_date=DAY,
    )
    check("date header", out.startswith("📰 Bản tin — Thứ Năm, 15/10/2026"), out.split("\n")[0])
    for title in ("🎯 Mục tiêu", "👥 Người cần nhớ", "⏰ Nhắc việc", "📝 Ghi chú"):
        check(f"section header {title}", f"### {title}" in out, out)
    check("bullets", "- Hoàn thành báo cáo tuần" in out and "- 9h họp team" in out, out)
    # canonical order
    positions = [out.index(t) for t in ("🎯 Mục tiêu", "👥 Người cần nhớ", "⏰ Nhắc việc", "📝 Ghi chú")]
    check("canonical order", positions == sorted(positions), str(positions))


def test_missing_sections_skipped():
    out = build_briefing({"goals": ["Một việc"], "notes": []}, on_date=DAY)
    check("kept section present", "### 🎯 Mục tiêu" in out, out)
    check("empty notes header absent", "### 📝 Ghi chú" not in out, out)
    check("missing people header absent", "👥 Người cần nhớ" not in out, out)
    check("missing reminders header absent", "⏰ Nhắc việc" not in out, out)
    check("no blank bullet lines", "\n- \n" not in out and not out.endswith("- "), out)


def test_empty_mapping_gives_default():
    check("empty dict", build_briefing({}) == "Hôm nay không có gì mới.")
    check("none", build_briefing(None) == "Hôm nay không có gì mới.")
    check("all empty lists", build_briefing({"goals": [], "notes": []}) == "Hôm nay không có gì mới.")
    check("whitespace only", build_briefing({"goals": ["   ", ""]}) == "Hôm nay không có gì mới.")
    check("empty_briefing()", empty_briefing() == "Hôm nay không có gì mới.")


def test_items_cleaned():
    out = build_briefing({"reminders": ["  9h họp  ", "", "   "]}, on_date=DAY)
    check("stripped", "- 9h họp" in out, out)
    check("blank items dropped", out.count("- ") == 1, out)


def test_unknown_key_rendered_under_own_name():
    out = build_briefing({"weather": ["Nắng, 32°C"]}, on_date=DAY)
    check("unknown key kept", "### weather" in out and "- Nắng, 32°C" in out, out)


def test_preview_tool_good_json():
    tools = build_tools()
    check("tool registered", "digest_preview" in tools)
    res = tools["digest_preview"]["func"](json.dumps({"goals": ["Xong việc A"]}))
    check("preview key", "preview" in res and "error" not in res, str(res))
    check("preview markdown", "### 🎯 Mục tiêu" in res["preview"] and "- Xong việc A" in res["preview"])


def test_preview_tool_bad_json():
    tools = build_tools()
    res = tools["digest_preview"]["func"]("{not json")
    check("bad json -> error", "error" in res, str(res))
    res = tools["digest_preview"]["func"](json.dumps(["not", "a", "dict"]))
    check("non-object -> error", "error" in res, str(res))
    res = tools["digest_preview"]["func"](json.dumps({}))
    check("empty object -> default message", res.get("preview") == "Hôm nay không có gì mới.", str(res))


if __name__ == "__main__":
    print("== digest ==")
    test_full_sections_format()
    test_missing_sections_skipped()
    test_empty_mapping_gives_default()
    test_items_cleaned()
    test_unknown_key_rendered_under_own_name()
    test_preview_tool_good_json()
    test_preview_tool_bad_json()
    print("ALL DIGEST TESTS PASSED")
