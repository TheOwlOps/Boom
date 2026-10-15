# -*- coding: utf-8 -*-
"""Test crossref.py — chạy: python3 test_crossref.py (KHÔNG cần internet)."""

from crossref import (
    cross_reference, split_sentences, normalize_sentence,
    sentences_same_claim, OVERLAP_THRESHOLD,
)

PASS = 0


def check(name, cond):
    global PASS
    assert cond, "FAIL: %s" % name
    PASS += 1
    print("PASS: %s" % name)


SOURCES = [
    {
        "url": "https://a.example.com/1",
        "title": "Bài A",
        "text": (
            "Python là ngôn ngữ lập trình rất phổ biến hiện nay. "
            "Nhiều công ty lớn sử dụng Python cho backend. "
            "Thời tiết hôm nay ở Hà Nội rất đẹp."
        ),
    },
    {
        "url": "https://b.example.com/2",
        "title": "Bài B",
        "text": (
            "Python is a very popular programming language today. "
            "Nhiều công ty lớn dùng Python cho backend của họ."
        ),
    },
    {
        "url": "https://c.example.com/3",
        "title": "Bài C",
        "text": (
            "Python là một ngôn ngữ lập trình phổ biến trên thế giới. "
            "Giá cà phê hôm nay tăng mạnh do hạn hán."
        ),
    },
]

result = cross_reference(SOURCES)
claims = result["claims"]

# 1. tổng số source
check("total_sources == 3", result["total_sources"] == 3)

# 2. tìm đúng 2 claims chung (claim Python-phổ-biến, claim công-ty-dùng-Python)
shared = [c for c in claims if c["agreement"]]
check("đúng 2 claims chung", len(shared) == 2)

# 3. count của mỗi claim chung == 2
check("claim chung 1 có count 2", all(c["count"] == 2 for c in shared))

# 4. sources của claim chung gồm đúng 2 url
for i, c in enumerate(shared):
    check("claim chung %d có đúng 2 sources" % (i + 1), len(c["sources"]) == 2)
    check("claim chung %d sources là url thật" % (i + 1),
          all(u.startswith("https://") for u in c["sources"]))

# 5. sắp xếp: claim count cao đứng trước
counts = [c["count"] for c in claims]
check("claims sắp xếp count giảm dần", counts == sorted(counts, reverse=True))

# 6. câu riêng (thời tiết, giá cà phê) không bị gom nhầm vào claim chung
texts = " ".join(c["text"] for c in shared)
check("câu thời tiết không lọt vào claim chung",
      "thời tiết" not in texts.lower() and "hà nội" not in texts.lower())
check("câu giá cà phê không lọt vào claim chung", "cà phê" not in texts.lower())

# 7. câu không liên quan -> không match nhau
kw_weather = normalize_sentence("Thời tiết hôm nay ở Hà Nội rất đẹp.")
kw_coffee = normalize_sentence("Giá cà phê hôm nay tăng mạnh do hạn hán.")
check("2 câu không liên quan không cùng claim",
      not sentences_same_claim(kw_weather, kw_coffee))

# 8. 2 câu cùng ý (khác cách diễn đạt) -> match
kw1 = normalize_sentence("Nhiều công ty lớn sử dụng Python cho backend.")
kw2 = normalize_sentence("Nhiều công ty lớn dùng Python cho backend của họ.")
check("2 câu cùng ý được gom chung", sentences_same_claim(kw1, kw2))

# 9. source rỗng -> không crash, trả về rỗng hợp lệ
r = cross_reference([{"url": "https://x.example.com", "title": "X", "text": ""}])
check("source rỗng không crash", r["claims"] == [] and r["total_sources"] == 1)
check("coverage source rỗng == 0", r["coverage"] == 0.0)

# 10. list rỗng -> không crash
r = cross_reference([])
check("list rỗng không crash",
      r["claims"] == [] and r["total_sources"] == 0 and r["coverage"] == 0.0)

# 11. coverage: 3/3 sources có claim chung
check("coverage == 1.0", result["coverage"] == 1.0)

# 12. split_sentences tách đúng
sents = split_sentences("Xin chào! Bạn khỏe không? Tôi ổn.")
check("tách được 3 câu", len(sents) == 3)

# 13. normalize bỏ stopwords
kw = normalize_sentence("Python là ngôn ngữ lập trình")
check("stopwords 'là' bị loại", "là" not in kw and "python" in kw)

# 14. ngưỡng overlap mặc định là 0.6
check("OVERLAP_THRESHOLD == 0.6", OVERLAP_THRESHOLD == 0.6)

# 15. text thiếu key -> không crash
r = cross_reference([{"url": "https://y.example.com"}])
check("source thiếu text không crash", r["total_sources"] == 1)

print("\nTất cả %d assertions PASS." % PASS)
