# -*- coding: utf-8 -*-
"""
test_deepthink.py — Test DeepThink bằng python3 thật, stdlib only.

Dữ liệu mẫu: 3 sources về topic "giá xăng tăng và bảo hiểm",
  - 2 claims chung (count >= 2)
  - 1 claim riêng (count == 1)
  - 1 entity trong query ("bảo hiểm") không source nào đề cập -> gap
Kèm test topic nhạy cảm: vẫn trả đủ perspectives, không bỏ sót/kiểm duyệt.
"""

import os
import sys
from pathlib import Path

# Import root is agent/src (repo convention); the modules under test live
# in subpackages there (they previously lived next to this file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from research.deepthink import DeepThink

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS: %s" % name)
    else:
        FAIL += 1
        print("FAIL: %s" % name)


# ---------------------------------------------------------------------------
# Dữ liệu mẫu chính
# ---------------------------------------------------------------------------
TOPIC = "giá xăng tăng và bảo hiểm"

SOURCES = [
    {"url": "https://a.vn/xang", "title": "A",
     "text": "Giá xăng tăng 1000 đồng mỗi lít từ ngày mai. Người dân lo lắng về chi phí đi lại."},
    {"url": "https://b.vn/xang", "title": "B",
     "text": "Giá xăng tăng thêm 1000 đồng một lít từ ngày mai. Chi phí sinh hoạt của người dân leo thang."},
    {"url": "https://c.vn/xang", "title": "C",
     "text": "Chi phí sinh hoạt leo thang do giá xăng tăng. Chuyên gia đề xuất giảm thuế môi trường để bình ổn giá."},
]

CROSSREF = {
    "claims": [
        {"text": "Giá xăng tăng 1000 đồng mỗi lít từ ngày mai.",
         "sources": ["https://a.vn/xang", "https://b.vn/xang"],
         "count": 2, "agreement": True},
        {"text": "Chi phí sinh hoạt của người dân leo thang do giá xăng tăng.",
         "sources": ["https://b.vn/xang", "https://c.vn/xang"],
         "count": 2, "agreement": True},
        {"text": "Chuyên gia đề xuất giảm thuế môi trường để bình ổn giá.",
         "sources": ["https://c.vn/xang"],
         "count": 1, "agreement": False},
    ],
    "total_sources": 3,
    "coverage": 1.0,
}


def test_main():
    dt = DeepThink()
    r = dt.analyze(TOPIC, SOURCES, CROSSREF)

    # 1. Đủ key trả về
    check("result có đủ 7 key",
          set(r.keys()) == {"topic", "summary", "perspectives", "gaps",
                            "consensus", "disputes", "source_scores"})

    # 2. Perspectives: 3 claims -> 3 perspectives
    check("perspectives = 3 (2 đồng thuận + 1 riêng)",
          len(r["perspectives"]) == 3)

    # 3. Phân loại kind đúng
    kinds = sorted(p["kind"] for p in r["perspectives"])
    check("kinds đúng [dong_thuan x2, rieng x1]",
          kinds == ["dong_thuan", "dong_thuan", "rieng"])

    # 4. support_count của perspective đồng thuận đầu tiên = 2
    check("support_count perspective đồng thuận = 2",
          r["perspectives"][0]["support_count"] == 2)

    # 5. Mỗi view ghi rõ nguồn theo dạng "Theo [url]"
    check("mọi view đều bắt đầu bằng 'Theo'",
          all(p["view"].startswith("Theo") for p in r["perspectives"]))

    # 6. Gaps: entity "bảo hiểm" trong query không source nào đề cập
    check("gaps tìm được 'bảo' (entity thiếu)",
          "bảo" in r["gaps"] and "hiểm" in r["gaps"])

    # 7. Entity được đề cập ("giá") không lọt vào gaps
    check("'giá' không nằm trong gaps",
          "giá" not in r["gaps"])

    # 8. Consensus: count >= ceil(3/2)=2 -> 2 claims
    check("consensus = 2 claims",
          len(r["consensus"]) == 2)

    # 9. Disputes: claim count==1 -> 1, giữ nguyên không loại
    check("disputes = 1 claim 1-source",
          len(r["disputes"]) == 1
          and "thuế môi trường" in r["disputes"][0])

    # 10. source_scores: a=1.0, b=1.0, c=0.5
    by_url = {s["url"]: s["score"] for s in r["source_scores"]}
    check("score a.vn=1.0, b.vn=1.0, c.vn=0.5",
          by_url["https://a.vn/xang"] == 1.0
          and by_url["https://b.vn/xang"] == 1.0
          and by_url["https://c.vn/xang"] == 0.5)

    # 11. source_scores sắp xếp giảm dần
    scores = [s["score"] for s in r["source_scores"]]
    check("source_scores sắp xếp giảm dần",
          scores == sorted(scores, reverse=True))

    # 12. summary 2-3 câu, nhắc tới topic
    check("summary chứa topic và không rỗng",
          bool(r["summary"]) and TOPIC in r["summary"])


def test_no_crossref():
    # crossref_result=None -> mỗi source = 1 perspective
    dt = DeepThink()
    r = dt.analyze(TOPIC, SOURCES, None)

    check("crossref None: 3 sources -> 3 perspectives",
          len(r["perspectives"]) == 3)
    check("crossref None: tất cả là quan điểm riêng",
          all(p["kind"] == "rieng" for p in r["perspectives"]))
    check("crossref None: view vẫn ghi rõ nguồn",
          all(p["view"].startswith("Theo https://") for p in r["perspectives"]))


def test_sensitive_topic():
    # NGUYÊN TẮC KHÔNG NÉ TRÁNH: topic nhạy cảm vẫn phân tích đầy đủ,
    # không bỏ sót perspective, không loại dispute, không kiểm duyệt nội dung.
    dt = DeepThink()
    topic = "quan điểm chính trị về chính sách thuế mới"
    sources = [
        {"url": "https://x.vn/a", "title": "A",
         "text": "Phe ủng hộ cho rằng chính sách thuế mới giúp giảm bất bình đẳng."},
        {"url": "https://y.vn/b", "title": "B",
         "text": "Phe phản đối cho rằng chính sách thuế mới làm tăng gánh nặng doanh nghiệp."},
    ]
    crossref = {
        "claims": [
            {"text": "Phe ủng hộ cho rằng chính sách thuế mới giúp giảm bất bình đẳng.",
             "sources": ["https://x.vn/a"], "count": 1, "agreement": False},
            {"text": "Phe phản đối cho rằng chính sách thuế mới làm tăng gánh nặng doanh nghiệp.",
             "sources": ["https://y.vn/b"], "count": 1, "agreement": False},
        ],
        "total_sources": 2,
    }
    r = dt.analyze(topic, sources, crossref)

    check("topic nhạy cảm: đủ 2 perspectives, không bỏ sót",
          len(r["perspectives"]) == 2)
    check("topic nhạy cảm: disputes giữ nguyên 2 claims (không loại)",
          len(r["disputes"]) == 2)
    views = " ".join(p["view"] for p in r["perspectives"])
    check("topic nhạy cảm: nội dung 2 phe đều xuất hiện trong views (không kiểm duyệt)",
          "giảm bất bình đẳng" in views and "tăng gánh nặng doanh nghiệp" in views)
    check("topic nhạy cảm: mỗi view ghi rõ nguồn",
          "Theo https://x.vn/a" in views and "Theo https://y.vn/b" in views)


def test_edge_cases():
    dt = DeepThink()
    # sources rỗng: không crash, perspectives/scores rỗng, gaps = keywords topic
    r = dt.analyze("giá xăng tăng", [], None)
    check("sources rỗng: không crash, perspectives rỗng",
          r["perspectives"] == [] and r["source_scores"] == [])
    check("sources rỗng: gaps = toàn bộ keywords topic",
          set(r["gaps"]) == {"giá", "xăng", "tăng"})
    # topic rỗng: gaps rỗng, vẫn trả đủ cấu trúc
    r2 = dt.analyze("", SOURCES, CROSSREF)
    check("topic rỗng: gaps rỗng, cấu trúc đầy đủ",
          r2["gaps"] == [] and len(r2["perspectives"]) == 3)


if __name__ == "__main__":
    test_main()
    test_no_crossref()
    test_sensitive_topic()
    test_edge_cases()
    print("\n==== %d PASS, %d FAIL ====" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
