---
name: log-analyzer
description: Analyze log files, count errors, and find slow patterns.
version: 1.0.0
author: boom-overnight
license: MIT
platforms: [linux, macos]
tags: [logs, debugging, monitoring]
category: devops
---

# Log Analyzer

Analyze log files (app, server, worker) to extract the important signals:

- Count `errors` / `warnings` by severity.
- Group repeated error messages (`top_errors`).
- Detect Python/JS stack traces.
- Detect slow operations (`slow_ops`) against a time threshold.

## Usage

```python
from analyzer import analyze_log

result = analyze_log("/var/log/app.log", slow_threshold_ms=1000)
print(result["total_lines"], result["errors"], result["warnings"])
```

Use `terminal` to open the log file and `read_file` to view the log around the
biggest error. The result is a dict:

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

## Detection conventions

- Severity: keywords `ERROR`, `WARN`/`WARNING`, `CRITICAL`, `EXCEPTION` (case-insensitive).
- Slow op: a line containing `duration`/`elapsed`/`took`/`in`/`after` + a number + a unit exceeding `slow_threshold_ms`.
- Messages are normalized before grouping: timestamps, PIDs, request-id numbers
  are stripped so copies of the same error land in one bucket.
