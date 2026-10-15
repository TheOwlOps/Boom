---
name: log-analyzer
description: Phân tích file log, đếm lỗi và tìm pattern chậm.
version: 1.0.0
author: boom-overnight
license: MIT
platforms: [linux, macos]
tags: [logs, debugging, monitoring]
category: devops
---

# Log Analyzer

Phân tích file log (app, server, worker) để trích xuất tín hiệu quan trọng:

- Đếm `errors` / `warnings` theo mức độ.
- Gom nhóm message lỗi lặp lại (`top_errors`).
- Phát hiện stack trace Python/JS.
- Phát hiện operation chậm (`slow_ops`) theo ngưỡng thời gian.

## Cách dùng

```python
from analyzer import analyze_log

result = analyze_log("/var/log/app.log", slow_threshold_ms=1000)
print(result["total_lines"], result["errors"], result["warnings"])
```

Dùng `terminal` để mở file log và `read_file` để xem đoạn log quanh lỗi
lớn nhất. Kết quả trả về là dict:

```python
{
  "total_lines": 1234,
  "errors": 12,
  "warnings": 34,
  "top_errors": [{"message": "...", "count": 5}],
  "stack_traces": 3,
  "slow_ops": [{"operation": "...", "duration_ms": 2500, "line": 42}],
}
```

## Quy ước nhận diện

- Mức độ: từ khóa `ERROR`, `WARN`/`WARNING`, `CRITICAL`, `EXCEPTION` (không phân biệt hoa/thường).
- Slow op: dòng chứa `duration`/`elapsed`/`took`/`in`/`after` + số + đơn vị vượt `slow_threshold_ms`.
- Message được chuẩn hoá trước khi gom nhóm: bỏ timestamp, PID, số request-id
  để các bản sao của cùng một lỗi gom vào một bucket.
