# DeepSearch + DeepThink — Nghiên cứu sâu trong Boom

## 1. DeepSearch là gì? DeepThink là gì?

**DeepSearch** là chế độ "tìm kiếm chủ động": thay vì gọi `web_search`
một lần rồi dừng, agent lặp nhiều vòng — đọc kết quả, trích thực thể
(tên người, công nghệ, con số...), tự sinh câu hỏi phụ (follow-up) từ
những gì còn thiếu, rồi tìm tiếp. Cuối cùng các nguồn được **cross-
reference** (đối chiếu) để đánh dấu nội dung đồng thuận và xung đột.

**DeepThink** là bước "ngồi lại suy nghĩ" sau khi đã có nguồn: nhận kết
quả của DeepSearch rồi rút ra **summary** (tóm tắt), **perspectives**
(nhiều góc nhìn: ủng hộ / phản biện / trung lập), và **gaps** (cái gì
vẫn chưa rõ, cần tìm thêm).

### So sánh ngắn với Grok (DeepSearch của xAI)

| Tiêu chí              | Grok DeepSearch            | Boom DeepSearch (bản này)            |
|-----------------------|----------------------------|--------------------------------------|
| Nguồn tìm kiếm        | Web + X (social real-time) | DuckDuckGo HTML (web.py của Boom)    |
| Entity extraction     | LLM (có ngữ cảnh)          | Regex heuristic (đơn giản, rẻ)       |
| Cross-reference       | Embedding + LLM            | Overlap từ (word overlap)            |
| Tóm tắt               | LLM sinh văn bản            | Ghép câu trích dẫn + heuristic       |
| Phụ thuộc             | API key xAI                | Không — stdlib + web.py sẵn có       |

Nói gọn: Grok mạnh nhờ LLM ở mọi bước; bản Boom chạy **offline logic**
(không tốn thêm API key, nhanh, rẻ), đổi lại độ sâu suy luận thấp hơn.
Khi Boom có provider key, có thể thay từng bước bằng LLM mà không đổi
interface (ghi ở mục 5).

## 2. Architecture

```
                        +----------------+
                        |  user / agent  |
                        +-------+--------+
                                | query
                                v
                     +----------------------+
                     |      DeepSearch      |
                     |  (multi-hop loop)    |
                     +----------+-----------+
                                |
        +-----------+-----------+-----------+------------+
        v           v           v           v            v
   web_search -> web_fetch -> trích     sinh follow-up -> cross_reference
   (DuckDuckGo)  (chi tiết)   entities   (câu hỏi phụ)   (đồng thuận/
                                                     xung đột nguồn)
                                |
                                v
                     +----------------------+
                     |      DeepThink       |
                     |     .analyze()       |
                     +----------+-----------+
                                |
                    summary + perspectives + gaps
```

Chi tiết từng bước:

1. **search** — `web_search(query, max_results=10)` qua DuckDuckGo HTML
   (không API key), lấy danh sách `{title, url, snippet}`.
2. **fetch** — `web_fetch(url)` lấy nội dung chi tiết từng nguồn (tối đa
   500 KiB, có circuit breaker, retry).
3. **entities** — regex trích tên riêng, con số, ngày tháng từ text.
   Regex rẻ nhưng "mù ngữ cảnh": "Apple" công ty hay trái cây thì không
   phân biệt được.
4. **follow-up** — từ entities + snippet còn thiếu, sinh câu hỏi phụ, ví
   dụ: entity xuất hiện ở 1 nguồn nhưng chưa có định nghĩa -> query
   `"entity là gì"`.
5. **crossref** — `cross_reference(sources)`: với mỗi cặp nguồn, tính
   độ overlap từ vựng; overlap cao + cùng khẳng định -> đồng thuận,
   overlap cao + từ phủ định khác nhau -> nghi xung đột.
6. **think** — `DeepThink.analyze()` ghép các câu trích dẫn quan trọng
   thành summary, nhóm perspectives theo sắc thái, liệt kê gaps là các
   entity chỉ xuất hiện 1 lần hoặc câu hỏi follow-up chưa trả lời được.

## 3. Cách dùng (code Python)

```python
# Dùng trực tiếp lõi nghiên cứu
from research import DeepSearch, DeepThink, cross_reference

ds = DeepSearch(search_fn=web_search, fetch_fn=web_fetch, max_hops=3)
result = ds.research("BoomBot kiến trúc 3 lớp")

print(result["num_sources"])   # số nguồn thu thập được
print(result["entities"][:10]) # top 10 thực thể
print(result["follow_ups"])    # câu hỏi phụ đã sinh

tk = DeepThink()
analysis = tk.analyze(topic="BoomBot kiến trúc 3 lớp",
                      research_result=result)
print(analysis["summary"])
print(analysis["perspectives"])  # ủng hộ / phản biện / trung lập
print(analysis["gaps"])          # khoảng trống thông tin
```

Dùng qua tool wrapper (khuyến nghị cho agent):

```python
from research.tools_wrapper import deep_search, deep_think

r = deep_search("tình hình giá cà phê hôm nay", max_hops=3)
# r = {ok, query, num_sources, sources[5]{url,title,snippet},
#      entities[10], follow_ups, agreement}

t = deep_think("giá cà phê hôm nay")
# t = {ok, topic, summary, perspectives, gaps, num_sources}
```

## 4. Cách wire vào environment.py (copy-paste)

1. Copy 3 file vào repo:
   - `agent/src/research/__init__.py`   <- file `__init__.py` này
   - `agent/src/research/tools_wrapper.py`
   - (lõi `deepsearch.py`, `deepthink.py`, `crossref.py` do worker khác viết)

2. Thêm import ở đầu `environment.py`:

```python
from research.tools_wrapper import deep_search, deep_think
```

3. Trong `__init__` của environment, sau block đăng ký `web_*`, thêm:

```python
self._tools["deep_search"] = {
    "name": "deep_search",
    "func": deep_search,
    "description": "Nghiên cứu sâu đa bước: tìm kiếm, đọc chi tiết, "
                   "trích thực thể, sinh câu hỏi phụ, đối chiếu nguồn. "
                   "Trả về top 5 nguồn, top 10 thực thể, mức đồng thuận.",
    "schema": _schema(
        {"query": {"type": "string", "description": "Câu hỏi/chủ đề cần nghiên cứu."},
         "max_hops": {"type": "integer", "description": "Số vòng tìm kiếm tối đa (mặc định 3, tối đa 10)."}},
        ["query"],
    ),
}
self._tools["deep_think"] = {
    "name": "deep_think",
    "func": deep_think,
    "description": "Suy luận sâu: chạy deep_search rồi phân tích thành "
                   "tóm tắt, các góc nhìn và khoảng trống thông tin.",
    "schema": _schema(
        {"topic": {"type": "string", "description": "Chủ đề cần suy luận."},
         "query": {"type": "string", "description": "Câu hỏi tìm kiếm tùy chọn (mặc định dùng topic)."}},
        ["topic"],
    ),
}
```

Lưu ý: `tools_wrapper` tự fail-soft — nếu `research` chưa có trên máy,
tool trả về `{"ok": False, "error": ...}` chứ không làm crash gateway.

## 5. Giới hạn hiện tại và hướng nâng cấp

- **Entity extraction regex đơn giản**: bắt tên viết hoa, số, ngày tháng
  bằng pattern; sai nhiều với tiếng Việt (tên không dấu, từ viết tắt)
  và không hiểu ngữ nghĩa. Nâng cấp: gọi LLM `extract_entities` qua
  provider registry của Boom.
- **Cross-reference overlap từ (chưa dùng embedding)**: hai nguồn viết
  khác chữ nhưng cùng ý sẽ bị đánh giá thấp. Nâng cấp: dùng embedding
  (local như MiniLM, hoặc API) tính cosine similarity.
- **Cần LLM để tóm tắt hay hơn**: summary hiện tại là ghép câu trích
  dẫn, đọc hơi rời rạc. Nâng cấp: `DeepThink.analyze()` nhận thêm
  `llm_fn` tùy chọn — có thì dùng LLM viết lại, không thì fallback
  heuristic (giữ nguyên interface).
- `max_hops` bị chặn tối đa 10 để tránh vòng lặp tốn request; mỗi hop
  gọi thêm web_search + web_fetch nên latency tăng tuyến tính.
