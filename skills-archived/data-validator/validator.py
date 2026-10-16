"""Validate JSON/CSV theo schema: type checking, required, format (email/url/date)."""
from __future__ import annotations

import csv
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import StrictBool, StrictFloat, StrictInt, StrictStr

# Map tên type trong schema -> pydantic strict type để ép type nghiêm ngặt
TYPE_MAP = {
    "str": StrictStr,
    "int": StrictInt,
    "float": StrictFloat,
    "bool": StrictBool,
    "list": list,
    "dict": dict,
}

# Regex kiểm tra format phổ biến
EMAIL_PAT = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
URL_PAT = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)
# Chấp nhận YYYY-MM-DD hoặc ISO datetime
DATE_PAT = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _check_format(value: Any, fmt: str) -> bool:
    """Kiểm tra giá trị có đúng format yêu cầu không."""
    if fmt == "email":
        return isinstance(value, str) and bool(EMAIL_PAT.match(value))
    if fmt == "url":
        return isinstance(value, str) and bool(URL_PAT.match(value))
    if fmt == "date":
        if not isinstance(value, str) or not DATE_PAT.match(value):
            return False
        try:
            # Kiểm tra ngày có thật (loại 2026-13-99)
            date.fromisoformat(value[:10])
            return True
        except ValueError:
            return False
    return True  # format không biết -> bỏ qua


def validate_json(data: Any, schema: dict) -> dict:
    """Validate một dict theo schema.

    Schema: {field: {"type": "str|int|float|bool|list|dict",
                      "required": bool, "format": "email|url|date" (optional)}}

    Returns:
        {"valid": bool, "errors": [{"field": ..., "message": ...}]}.
    """
    errors: list[dict] = []
    if not isinstance(data, dict):
        return {
            "valid": False,
            "errors": [{"field": None, "message": "Dữ liệu phải là dict"}],
        }

    for field, rules in schema.items():
        required = rules.get("required", False)
        if field not in data:
            if required:
                errors.append(
                    {"field": field, "message": f"Thiếu field bắt buộc: {field}"}
                )
            continue

        value = data[field]
        expected = rules.get("type")
        if expected and expected in TYPE_MAP:
            try:
                # Ép type nghiêm ngặt bằng pydantic (bool không ép thành int...)
                from pydantic import TypeAdapter

                TypeAdapter(TYPE_MAP[expected]).validate_python(value)
            except Exception:
                errors.append(
                    {
                        "field": field,
                        "message": f"Sai type: mong đợi {expected}, nhận {type(value).__name__}",
                    }
                )
                continue  # type sai thì không check format nữa

        fmt = rules.get("format")
        if fmt and not _check_format(value, fmt):
            errors.append(
                {"field": field, "message": f"Sai format {fmt}: {value!r}"}
            )

    return {"valid": not errors, "errors": errors}


def validate_csv(path: str | Path, columns: list[str]) -> dict:
    """Validate file CSV: đủ cột yêu cầu, mỗi dòng đủ số cột, không dòng trống lạ.

    Returns:
        {"valid": bool, "errors": [...], "rows": số_dòng_dữ_liệu}.
    """
    errors: list[dict] = []
    path = Path(path)
    if not path.is_file():
        return {
            "valid": False,
            "errors": [{"field": None, "message": f"File không tồn tại: {path}"}],
            "rows": 0,
        }

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return {
                "valid": False,
                "errors": [{"field": None, "message": "File CSV rỗng"}],
                "rows": 0,
            }
        header = [h.strip() for h in header]
        missing = [c for c in columns if c not in header]
        for col in missing:
            errors.append({"field": col, "message": f"Thiếu cột yêu cầu: {col}"})

        rows = 0
        for lineno, row in enumerate(reader, start=2):
            if not any(cell.strip() for cell in row):
                continue  # bỏ qua dòng hoàn toàn trống
            rows += 1
            if len(row) != len(header):
                errors.append(
                    {
                        "field": None,
                        "message": f"Dòng {lineno}: {len(row)} cột, mong đợi {len(header)}",
                    }
                )

    return {"valid": not errors, "errors": errors, "rows": rows}
