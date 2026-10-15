---
name: doc-researcher
description: "Lập kế hoạch nghiên cứu cho 1 topic: câu hỏi cần trả lời, nguồn cần tra, template outline markdown để agent điền."
version: 1.0.0
author: Boom
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [research, documentation, planning, outline]
    related_skills: [research/deep-research]
---

# Doc-Researcher — Research Plan & Outline

## Tên
`doc-researcher`

## Mô tả
Nhận một topic nghiên cứu → sinh **research plan** có cấu trúc: phân rã topic thành các câu hỏi cần trả lời, gợi ý nguồn cần tra cho từng câu hỏi (docs chính thức, paper, blog, source code, cộng đồng), rồi tổng hợp thành **outline markdown** kèm template để agent (hoặc người) điền kết quả vào.

Skill này không gọi web thật — nó tạo framework để quá trình nghiên cứu có hệ thống, tránh bỏ sót góc nhìn.

## Khi nào dùng
- Trước khi nghiên cứu công nghệ/thư viện mới ("nên dùng X hay Y?")
- Khi cần viết tài liệu tổng hợp, báo cáo so sánh, RFC
- Khi muốn chia việc research cho nhiều subagent theo từng câu hỏi

## Input
- `topic` (str): chủ đề nghiên cứu, vd `"So sánh Redis vs Valkey cho cache session"`
- `goal` (str, optional): mục tiêu cuối cùng, vd `"chọn 1 giải pháp để triển khai"`
- `depth` ("quick" | "standard" | "deep", mặc định `"standard"`): độ sâu nghiên cứu
- `questions` (list[str], optional): câu hỏi có sẵn của người dùng, được merge vào plan

## Output
Dict:
- `topic`, `goal`, `depth`
- `questions`: list `{id, question, why, suggested_sources[]}`
- `outline_markdown`: outline đầy đủ với template `> TODO:` để điền kết quả
- `source_checklist`: checklist nguồn theo loại (official docs, paper, blog, code, community)

## Ví dụ

```python
from doc_researcher.research import build_research_plan, render_outline

plan = build_research_plan(
    topic="So sánh Redis vs Valkey cho cache session",
    goal="chọn 1 giải pháp để triển khai trong Q4",
    depth="standard",
)
print(render_outline(plan))   # outline markdown để bắt đầu nghiên cứu
```
