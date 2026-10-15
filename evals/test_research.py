"""Tests for agent/src/tools/research_tool.py — pure, no network, no model calls."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from tools.research_tool import build_tools, research_brief


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit("FAILED: " + name)


SOURCES = [
    {
        "url": "https://example.com/a",
        "title": "Pin perovskite đạt 26%",
        "excerpt": "Nhóm nghiên cứu công bố hiệu suất 26,1%. Đây là kỷ lục mới cho pin mặt trời perovskite trong phòng thí nghiệm.",
    },
    {
        "url": "https://example.com/b",
        "title": "Thương mại hóa 2027",
        "excerpt": "Công ty dự kiến sản xuất hàng loạt từ 2027! Giá thành giảm 30% so với silicon.",
    },
    {"url": "https://example.com/c", "title": "Thách thức độ bền", "excerpt": ""},
]


def test_basic_brief():
    r = research_brief("pin mặt trời perovskite", SOURCES)
    check("no error", "error" not in r)
    check("topic kept", r["topic"] == "pin mặt trời perovskite")
    check("summary cites [1]", "[1]" in r["summary"])
    check("summary cites [2]", "[2]" in r["summary"])
    check("summary cites [3]", "[3]" in r["summary"])
    check("summary quotes source text", "26,1%" in r["summary"])
    check("key_points enough", len(r["key_points"]) == 3)
    check("key_points cite [1]", r["key_points"][0].startswith("[1]"))
    check("key_points cite [2]", r["key_points"][1].startswith("[2]"))
    check("key_points cite [3]", r["key_points"][2].startswith("[3]"))
    check("sources order kept", [s["url"] for s in r["sources"]] == [s["url"] for s in SOURCES])
    check("sources refs", [s["ref"] for s in r["sources"]] == ["[1]", "[2]", "[3]"])


def test_empty_sources_error():
    check("empty list -> error", "error" in research_brief("x", []))
    check("non-list -> error", "error" in research_brief("x", "notalist"))
    check("none -> error", "error" in research_brief("x", None))


def test_empty_topic_error():
    check("blank topic -> error", "error" in research_brief("   ", SOURCES))


def test_bad_sources_skipped():
    r = research_brief("x", [{"title": "no url"}, "junk", None, SOURCES[0]])
    check("bad skipped, good kept", "error" not in r and len(r["sources"]) == 1)
    check("skipped counted", r.get("skipped") == 3)


def test_all_bad_sources_error():
    check("all bad -> error", "error" in research_brief("x", [{"a": 1}, None]))


def test_dedupe_by_url():
    r = research_brief("x", [SOURCES[0], dict(SOURCES[0])])
    check("duplicate url kept once", len(r["sources"]) == 1 and r.get("skipped") == 1)


def test_summary_caps_at_five():
    many = [
        {"url": f"https://e.com/{i}", "title": f"T{i}", "excerpt": f"Câu nội dung {i}. Thêm câu nữa."}
        for i in range(8)
    ]
    r = research_brief("x", many)
    check("summary has [5]", "[5]" in r["summary"])
    check("summary no [6]", "[6]" not in r["summary"])
    check("overflow note", "+3" in r["summary"])
    check("key_points cover all", len(r["key_points"]) == 8)


def test_build_tools_shape():
    t = build_tools()
    check("tool registered", "research_brief" in t)
    check("schema requires topic+sources",
          set(t["research_brief"]["schema"]["required"]) == {"topic", "sources"})
    r = t["research_brief"]["func"]("x", SOURCES)
    check("func callable via dict", "[1]" in r["summary"] and "[2]" in r["summary"])


def test_no_network_imports():
    import tools.research_tool as m
    src = Path(m.__file__).read_text(encoding="utf-8")
    for bad in ("urllib", "socket", "http.client", "requests", "subprocess"):
        check(f"no {bad} import", bad not in src)


if __name__ == "__main__":
    print("== research_tool ==")
    test_basic_brief()
    test_empty_sources_error()
    test_empty_topic_error()
    test_bad_sources_skipped()
    test_all_bad_sources_error()
    test_dedupe_by_url()
    test_summary_caps_at_five()
    test_build_tools_shape()
    test_no_network_imports()
    print("ALL RESEARCH TESTS PASSED")
