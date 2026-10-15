"""Test DeepSearch core bằng mock search_fn/fetch_fn (KHÔNG gọi internet thật)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deepsearch import DeepSearch  # noqa: E402

PASS = 0


def check(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1
    print(f"  PASS: {name}")


def make_search(pages):
    """pages: dict query_substring -> [result dicts]."""
    calls = []

    def search_fn(query):
        calls.append(query)
        for key, results in pages.items():
            if key in query:
                return results
        return []

    search_fn.calls = calls
    return search_fn


def make_fetch(texts):
    """texts: dict url -> text."""
    calls = []

    def fetch_fn(url):
        calls.append(url)
        return {"url": url, "title": f"Title {url}", "text": texts.get(url, "")}

    fetch_fn.calls = calls
    return fetch_fn


# ---------------------------------------------------------------------------
# Test 1: 1 hop -> 2 sources -> entities trích được
print("Test 1: 1 hop cơ bản")
s1 = make_search({
    "AI agent": [
        {"title": "A", "url": "https://ex.com/a", "snippet": ""},
        {"title": "B", "url": "https://ex.com/b", "snippet": ""},
    ],
})
f1 = make_fetch({
    "https://ex.com/a": (
        "OpenAI GPT-4 là mô hình ngôn ngữ mạnh. "
        "Ra mắt ngày 14/03/2023 với 175 tỷ tham số huấn luyện."
    ),
    "https://ex.com/b": "Anthropic Claude hỗ trợ lập trình rất tốt.",
})
ds = DeepSearch(search_fn=s1, fetch_fn=f1)
res = ds.research("AI agent là gì", max_hops=1)
check("trả về đúng query gốc", res["query"] == "AI agent là gì")
check("1 hop -> 2 sources", len(res["sources"]) == 2)
check("source có đủ keys url/title/text/hop",
      all(set(s) >= {"url", "title", "text", "hop"} for s in res["sources"]))
check("trích được cụm viết hoa 2+ từ", "OpenAI GPT-4" in res["entities"])
check("trích được ngày tháng", "14/03/2023" in res["entities"])
check("trích được số liệu có đơn vị", "175 tỷ" in res["entities"])
check("hops_used == 1", res["hops_used"] == 1)

# ---------------------------------------------------------------------------
# Test 2: multi-hop — entity từ hop 1 tạo query hop 2
print("Test 2: multi-hop")
s2 = make_search({
    "AI agent": [{"title": "A", "url": "https://ex.com/a", "snippet": ""}],
    "OpenAI GPT-4": [{"title": "C", "url": "https://ex.com/c", "snippet": ""}],
})
f2 = make_fetch({
    "https://ex.com/a": "OpenAI GPT-4 được phát triển bởi OpenAI.",
    "https://ex.com/c": "Microsoft Corporation đầu tư mạnh vào OpenAI.",
})
ds2 = DeepSearch(search_fn=s2, fetch_fn=f2)
res2 = ds2.research("AI agent là gì", max_hops=2)
check("hops_used == 2 (có hop 2)", res2["hops_used"] == 2)
check("hop 2 đã fetch source mới",
      any(s["url"] == "https://ex.com/c" and s["hop"] == 1 for s in res2["sources"]))
check("entity từ hop 2 xuất hiện", "Microsoft Corporation" in res2["entities"])
check("đã sinh follow-up query dạng 'là gì'",
      any("là gì" in q for q in s2.calls[1:]))

# ---------------------------------------------------------------------------
# Test 3: dedup — cùng URL không fetch 2 lần
print("Test 3: dedup URL")
s3 = make_search({
    "trùng": [
        {"title": "A", "url": "https://ex.com/a", "snippet": ""},
        {"title": "A2", "url": "https://ex.com/a", "snippet": ""},  # trùng URL
    ],
})
f3 = make_fetch({"https://ex.com/a": "Nội dung trùng lặp."})
ds3 = DeepSearch(search_fn=s3, fetch_fn=f3)
res3 = ds3.research("trùng", max_hops=1)
check("fetch chỉ gọi 1 lần cho URL trùng", f3.calls.count("https://ex.com/a") == 1)
check("sources không có URL trùng lặp",
      len({s["url"] for s in res3["sources"]}) == len(res3["sources"]))

# ---------------------------------------------------------------------------
# Test 4: max_hops — dừng đúng giới hạn dù luôn có entity mới
print("Test 4: max_hops")
counter = {"i": 0}


def endless_search(query):
    counter["i"] += 1
    return [{"title": "P", "url": f"https://ex.com/p{counter['i']}", "snippet": ""}]


def endless_fetch(url):
    # Mỗi lần fetch sinh entity mới -> vòng lặp sẽ không bao giờ hết query mới
    n = int(url.rsplit("p", 1)[-1])
    return {"url": url, "title": "P",
            "text": f"Bài viết về Tập Đoàn {chr(64 + n)} Holdings."}


ds4 = DeepSearch(search_fn=endless_search, fetch_fn=endless_fetch)
res4 = ds4.research("vòng lặp vô hạn", max_hops=3, max_sources=50)
check("hops_used không vượt max_hops", res4["hops_used"] <= 3)
check("hops_used dừng đúng ở 3", res4["hops_used"] == 3)
check("số hop thực tế bị giới hạn", max(s["hop"] for s in res4["sources"]) <= 2)

# ---------------------------------------------------------------------------
# Test 5: max_sources + constructor validation
print("Test 5: giới hạn & validation")
s5 = make_search({"nhiều": [{"title": str(i), "url": f"https://ex.com/x{i}", "snippet": ""}
                             for i in range(20)]})
f5 = make_fetch({f"https://ex.com/x{i}": f"Nội dung trang {i}." for i in range(20)})
ds5 = DeepSearch(search_fn=s5, fetch_fn=f5)
res5 = ds5.research("nhiều", max_hops=1, max_sources=5)
check("tôn trọng max_sources", len(res5["sources"]) == 5)

try:
    DeepSearch(search_fn=None, fetch_fn=None)
    check("thiếu cả 2 fn + không import được web.py thì raise", False)
except RuntimeError:
    # Nếu chạy trong repo (import được web.py thật) thì constructor không raise;
    # trường hợp này coi như PASS vì đã có fallback thật.
    check("constructor chấp nhận fallback web.py thật hoặc raise RuntimeError", True)

try:
    DeepSearch(search_fn=s5, fetch_fn=f5).research("   ")
    check("query rỗng thì raise ValueError", False)
except ValueError:
    check("query rỗng thì raise ValueError", True)

print(f"\nTỔNG: {PASS} assertions PASS")
