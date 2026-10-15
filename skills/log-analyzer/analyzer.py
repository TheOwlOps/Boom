"""Phân tích file log: đếm lỗi/warning, gom nhóm message, phát hiện slow ops."""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

# Các mẫu regex nhận diện mức độ log (không phân biệt hoa/thường)
ERROR_PAT = re.compile(r"\b(ERROR|CRITICAL|FATAL|EXCEPTION|FAILED)\b", re.IGNORECASE)
WARN_PAT = re.compile(r"\b(WARN|WARNING)\b", re.IGNORECASE)
# Stack trace: dòng Traceback hoặc dòng bắt đầu bằng khoảng trắng + "at"/"File"
TRACEBACK_PAT = re.compile(r"Traceback \(most recent call last\)|^\s+(at |File \")", re.MULTILINE)
# Slow op: các cách ghi thời gian phổ biến
DURATION_PAT = re.compile(
    r"(?:duration|elapsed|took|in|after)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|seconds?|µs|us)",
    re.IGNORECASE,
)
# Timestamp đầu dòng để loại bỏ khi gom nhóm message
TIMESTAMP_PAT = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?|\d{2}:\d{2}:\d{2})"
)
# ID động (request-id, pid, uuid, hex) để chuẩn hoá message
DYNAMIC_PAT = re.compile(
    r"\b(?:req[_-]?id|request_id|pid|trace_id|span_id|session_id)=\S+"
    r"|\b[0-9a-fA-F-]{8,}\b|\b\d{4,}\b"
)


def normalize_message(line: str) -> str:
    """Loại bỏ timestamp và ID động để các lỗi giống nhau gom thành một bucket."""
    line = TIMESTAMP_PAT.sub("", line).strip()
    line = ERROR_PAT.sub("", line)
    line = WARN_PAT.sub("", line)
    line = DYNAMIC_PAT.sub("<id>", line)
    return " ".join(line.split())  # gom khoảng trắng thừa


def analyze_log(path: str | Path, slow_threshold_ms: float = 1000.0) -> dict:
    """Phân tích một file log.

    Args:
        path: Đường dẫn file log.
        slow_threshold_ms: Ngưỡng (ms) để một operation bị coi là chậm.

    Returns:
        dict với các khoá: total_lines, errors, warnings, top_errors,
        stack_traces, slow_ops.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    errors = 0
    warnings = 0
    error_counter: Counter[str] = Counter()
    slow_ops: list[dict] = []

    for lineno, line in enumerate(lines, start=1):
        if ERROR_PAT.search(line):
            errors += 1
            normalized = normalize_message(line)
            if normalized:
                error_counter[normalized] += 1
        elif WARN_PAT.search(line):
            warnings += 1
        # Phát hiện operation chậm trong mọi dòng
        for m in DURATION_PAT.finditer(line):
            value, unit = float(m.group(1)), m.group(2).lower()
            if unit in ("s", "second", "seconds"):
                value *= 1000.0
            elif unit in ("us", "µs", "microsecond", "microseconds"):
                value /= 1000.0
            if value >= slow_threshold_ms:
                slow_ops.append(
                    {
                        "operation": line.strip()[:200],
                        "duration_ms": round(value, 2),
                        "line": lineno,
                    }
                )

    # Đếm số stack trace trong toàn bộ file
    stack_traces = len(TRACEBACK_PAT.findall(text))

    top_errors = [
        {"message": msg, "count": count}
        for msg, count in error_counter.most_common(10)
    ]

    return {
        "total_lines": len(lines),
        "errors": errors,
        "warnings": warnings,
        "top_errors": top_errors,
        "stack_traces": stack_traces,
        "slow_ops": slow_ops,
    }
