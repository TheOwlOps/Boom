---
name: perf-optimizer
description: "Phân tích bottleneck hiệu năng bằng AST: vòng lặp lồng O(n²), I/O trong loop, nối chuỗi +=, N+1 query, thiếu cache."
version: 1.0.0
author: Boom Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [performance, optimization, bottleneck, ast, profiling-static]
    related_skills: [code-review]
---

# Perf Optimizer

Phân tích tĩnh code Python tìm bottleneck hiệu năng bằng AST (stdlib),
mỗi phát hiện kèm code mẫu đã tối ưu dùng đúng tên biến trong code gốc.

## Khi nào dùng

- Code chạy chậm, cần tìm điểm nghẽn trước khi optimize
- Review hiệu năng trước khi merge code xử lý dữ liệu lớn
- Khi user nói "tối ưu code này", "tìm bottleneck", "code chạy chậm quá"

**Không dùng cho:** profiling runtime (dùng `cProfile` cho số liệu thực tế);
ngôn ngữ khác Python. Skill tìm *mẫu code xấu*, không đo thời gian chạy.

## Input

- **Đường dẫn:** 1 file `.py` hoặc 1 thư mục (quét đệ quy)

## Output

List suggestions, mỗi suggestion có schema:

```json
[
  {
    "file": "agent/src/process.py",
    "line": 30,
    "pattern": "nested_loop",
    "severity": "medium",
    "problem": "Vòng lặp lồng nhau 2 cấp — độ phức tạp O(n^2)...",
    "optimized_code": "lookup = set(ys)  # hoặc dict nếu cần ánh xạ giá trị\nfor x in xs:\n    if x in lookup:  # O(1) thay vì lặp lồng nhau\n        ..."
  }
]
```

| Pattern | Severity | Ý nghĩa |
|---------|----------|---------|
| `nested_loop` | medium/high | Vòng lặp lồng nhau O(n²) trở lên → dùng set/dict tra cứu O(1) |
| `io_in_loop` | high | I/O lặp lại trong vòng lặp (open, requests, socket) → batch/hoist |
| `string_concat` | medium | Nối chuỗi `+=` trong loop → gom list rồi `"".join()` |
| `n_plus_one` | high | Query DB trong vòng lặp → 1 query với `IN` / `executemany` |
| `missing_cache` | medium | Gọi hàm tốn kém lặp lại trong loop → `@lru_cache` |
| `list_membership` | low | `x in list` trong loop → đổi sang `set` |

## Ví dụ

### CLI

```bash
# Phân tích 1 file, in markdown
python3 skills/perf-optimizer/optimizer.py agent/src/process.py

# Phân tích cả thư mục, in JSON
python3 skills/perf-optimizer/optimizer.py agent/src/ --json
```

### Python API

```python
from skills.perf_optimizer.optimizer import optimize_source, to_markdown

code = """
for user in users:
    for order in orders:
        if order.user_id == user.id:
            print(order)
"""
suggestions = optimize_source(code, filename="demo.py")
for s in suggestions:
    print(s.pattern, s.file, s.line)
    print(s.problem)
    print(s.optimized_code)
```

## Pitfalls

- Heuristic tĩnh: có thể báo thừa (vd: vòng lặp lồng nhỏ, dữ liệu ít thì
  không đáng tối ưu) — cân nhắc kích thước dữ liệu thực tế
- `missing_cache` chỉ gợi ý `@lru_cache` khi hàm do người dùng định nghĩa và
  chứa vòng lặp; hàm có side-effect thì KHÔNG được cache — kiểm tra thủ công
- Luôn đo lại bằng `cProfile`/benchmark sau khi áp dụng gợi ý
