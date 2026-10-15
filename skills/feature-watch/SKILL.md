---
name: feature-watch
description: "Theo dõi hệ sinh thái AI agent, báo tính năng/release mới có trích dẫn."
version: 0.1.0
---

# Skill: feature-watch

> Đây là **playbook cho agent** (LLM đọc và làm theo từng bước).
> Không phải code thực thi — mọi hành động đều qua tool có sẵn của Boom.

## Khi nào dùng

- User hỏi kiểu: "có gì mới không", "AI agent dạo này có tính năng gì mới", "framework nào vừa release".
- Cron tick hàng ngày/tuần (job `feature-watch-daily`).

## Nguồn nên quét

Hệ sinh thái AI agent: framework (openclaw, eliza/elizaOS, langchain,
autogen, crewai, composio, browser-use, skyvern...), LLM release mới,
paper/pattern mới (ReAct, DAG, memory, evals).

## Quy trình mỗi tick

### 1. Thu thập (collect)

- `news_search` với keyword: `AI`, `agent`, `LLM` trên feed `tin-moi-nhat`
  (VnExpress — mảng news connector).
- `web_search` cho release mới của framework agent, ví dụ:
  `"openclaw release notes"`, `"elizaOS changelog"`, `"langchain release"`,
  `"autogen new version"`, `"crewai release"`. Thêm GitHub releases/trending
  khi cần (`site:github.com ... releases`).
- `web_extract` vào URL release/changelog để đọc chi tiết khi cần verify.

### 2. Lọc (filter)

- **CHỈ giữ cái VERIFY được**: mỗi item phải có URL thật (release page,
  changelog, blog chính thức, hoặc bài báo). Không bịa tính năng,
  không đoán ngày release — không verify được thì loại.
- Đọc state file `memories/feature-watch.json` (danh sách URL đã báo).
  Item nào URL đã có trong state → **bỏ qua**.
- State file chưa tồn tại → coi như rỗng, tạo sau khi báo.

### 3. Xuất bản tin (report)

- Bản tin ngắn: mỗi item **1–2 dòng + link trích dẫn** `[n]`.
- Không dùng `digest_preview` khi item ít (dưới ~5) — trả text trực tiếp
  cho gọn; nhiều item thì dùng `digest_preview` cho đồng nhất.
- **Không có gì mới → trả đúng câu:** `Hôm nay không có tính năng mới đáng chú ý.`
  Đừng bịa cho có.

### 4. Cập nhật state

- Sau khi báo, append các URL vừa báo vào `memories/feature-watch.json`
  (array các URL, giữ tối đa ~500 URL gần nhất để file không phình).
- Nếu không có gì mới → không ghi gì.

## Ví dụ bản tin

```
🛰️ AI agent — cập nhật mới

1. CrewAI 0.177.0 thêm native memory server, tách khỏi vòng loop
   chính [1]
2. ElizaOS mở plugin mới cho scheduled tasks chạy nền [2]

[1] https://github.com/crewAIInc/crewAI/releases/tag/0.177.0
[2] https://github.com/elizaOS/eliza/releases/tag/...
```

## Quy tắc

- Skill này khác `competitor-news-monitor`: skill kia theo dõi *công ty*,
  skill này theo dõi *hệ sinh thái AI agent* (framework, release, pattern).
- Tiếng Việt cho phần user-facing. Link trích dẫn bắt buộc với mọi item.
- Dedupe bằng state file, không dựa vào trí nhớ.
