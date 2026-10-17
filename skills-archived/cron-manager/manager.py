#!/usr/bin/env python3
"""Skill cron-manager: quản lý cron jobs + validate cron expression.

Chức năng:
  - parse_cron(expr): validate cron expression, hỗ trợ @daily/@hourly/...
    và dạng 5 trường chuẩn (vd: "*/5 * * * *")
  - add(name, schedule, command): thêm job (lưu vào file JSON)
  - list_jobs(): liệt kê jobs
  - remove(name): xóa job
  - describe(expr): mô tả tiếng Việt dễ đọc ("mỗi 5 phút một lần")
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class CronError(ValueError):
    """Cron expression hoặc thao tác quản lý job không hợp lệ."""


# ---------------------------------------------------------------------------
# Model dữ liệu (pydantic)
# ---------------------------------------------------------------------------

class CronExpression(BaseModel):
    """Kết quả parse 1 cron expression: mỗi trường là list các giá trị int."""

    minute: list[int]
    hour: list[int]
    day: list[int]
    month: list[int]
    weekday: list[int]
    raw: str
    alias: Optional[str] = None  # vd: "@daily", None nếu viết dạng 5 trường


class CronJob(BaseModel):
    """Một cron job đã lưu."""

    name: str
    schedule: str
    command: str
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    enabled: bool = True


# ---------------------------------------------------------------------------
# Bảng tra cứu cron
# ---------------------------------------------------------------------------

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DOW_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}

# Mỗi trường: (giá trị nhỏ nhất, giá trị lớn nhất, dict tên viết tắt)
_FIELD_SPECS: dict[str, tuple[int, int, dict[str, int]]] = {
    "minute": (0, 59, {}),
    "hour": (0, 23, {}),
    "day": (1, 31, {}),
    "month": (1, 12, _MONTH_NAMES),
    "weekday": (0, 7, _DOW_NAMES),  # 7 cũng là Chủ nhật, sẽ chuẩn hóa về 0
}

_ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

# Tên job chỉ cho phép ký tự an toàn (tránh injection khi ghi crontab thật).
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


# ---------------------------------------------------------------------------
# Parse & validate cron expression
# ---------------------------------------------------------------------------

def _parse_value(token: str, field: str, lo: int, hi: int, names: dict[str, int]) -> int:
    """Parse 1 token đơn (số hoặc tên như 'jan', 'mon') thành int trong khoảng."""
    token = token.strip().lower()
    if token in names:
        value = names[token]
    else:
        try:
            value = int(token)
        except ValueError:
            raise CronError(f"giá trị không hợp lệ cho trường {field}: {token!r}")
    if not (lo <= value <= hi):
        raise CronError(f"{field}={value} nằm ngoài khoảng cho phép {lo}-{hi}")
    return value


def _parse_range(text: str, field: str, lo: int, hi: int, names: dict[str, int]) -> tuple[int, int]:
    """Parse 'a-b' thành (start, end)."""
    parts = text.split("-", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise CronError(f"khoảng không hợp lệ cho trường {field}: {text!r}")
    start = _parse_value(parts[0], field, lo, hi, names)
    end = _parse_value(parts[1], field, lo, hi, names)
    if start > end:
        raise CronError(f"khoảng ngược cho trường {field}: {text!r} (start > end)")
    return start, end


def _parse_step(step_s: str, field: str, span: int) -> int:
    """Parse bước nhảy 'n' trong '*/n', phải là số nguyên >= 1."""
    try:
        step = int(step_s.strip())
    except ValueError:
        raise CronError(f"bước nhảy không hợp lệ cho trường {field}: {step_s!r}")
    if step < 1:
        raise CronError(f"bước nhảy phải >= 1 cho trường {field}: {step_s!r}")
    if step > span:
        raise CronError(f"bước nhảy {step} lớn hơn cả khoảng {field} (1-{span})")
    return step


def _parse_part(part: str, field: str, lo: int, hi: int, names: dict[str, int]) -> list[int]:
    """Parse 1 mảnh trong trường (đã tách bởi dấu phẩy)."""
    part = part.strip()
    if not part:
        raise CronError(f"mảnh rỗng trong trường {field}")
    # Dạng bước nhảy: */n, a-b/n, a/n
    if "/" in part:
        base, step_s = part.split("/", 1)
        step = _parse_step(step_s, field, hi - lo + 1)
        base = base.strip()
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            start, end = _parse_range(base, field, lo, hi, names)
        else:
            start = _parse_value(base, field, lo, hi, names)
            end = hi
        return list(range(start, end + 1, step))
    # Dạng khoảng: a-b
    if "-" in part:
        start, end = _parse_range(part, field, lo, hi, names)
        return list(range(start, end + 1))
    # Dạng *: toàn bộ khoảng
    if part == "*":
        return list(range(lo, hi + 1))
    # Dạng đơn: số hoặc tên
    return [_parse_value(part, field, lo, hi, names)]


def _parse_field(text: str, field: str) -> list[int]:
    """Parse 1 trường cron (có thể chứa dấu phẩy), trả về list int đã sắp xếp."""
    lo, hi, names = _FIELD_SPECS[field]
    values: set[int] = set()
    for part in text.split(","):
        values.update(_parse_part(part, field, lo, hi, names))
    out = sorted(values)
    # Chuẩn hóa thứ trong tuần: 7 cũng là Chủ nhật -> gộp về 0
    if field == "weekday":
        out = sorted({0 if v == 7 else v for v in out})
    return out


def parse_cron(expr: str) -> CronExpression:
    """Validate cron expression, ném CronError nếu sai.

    Hỗ trợ: @reboot, @yearly/@annually, @monthly, @weekly, @daily/@midnight,
    @hourly, hoặc 5 trường chuẩn "phút giờ ngày tháng thứ"
    (vd: "*/5 * * * *", "0 9 * * mon-fri").
    """
    expr = (expr or "").strip()
    if not expr:
        raise CronError("cron expression rỗng")
    # @reboot là trường hợp đặc biệt: không có 5 trường giờ giấc
    if expr.lower() == "@reboot":
        return CronExpression(
            minute=[], hour=[], day=[], month=[], weekday=[],
            raw=expr, alias="@reboot",
        )
    alias: Optional[str] = None
    lowered = expr.lower()
    if lowered in _ALIASES:
        alias, expr = lowered, _ALIASES[lowered]
    parts = expr.split()
    if len(parts) != 5:
        raise CronError(
            f"cần đúng 5 trường 'phút giờ ngày tháng thứ', nhận được {len(parts)}: {expr!r}"
        )
    fields = ["minute", "hour", "day", "month", "weekday"]
    parsed = {f: _parse_field(p, f) for f, p in zip(fields, parts)}
    return CronExpression(raw=expr, alias=alias, **parsed)


def _is_full_step(values: list[int], lo: int, hi: int) -> bool:
    """Kiểm tra values có phải dạng 'mỗi n đơn vị' phủ kín khoảng không."""
    if len(values) < 2:
        return False
    step = values[1] - values[0]
    return values == list(range(lo, hi + 1, step))


def describe(expr: str) -> str:
    """Mô tả tiếng Việt dễ đọc cho cron expression (để agent báo cáo user)."""
    c = parse_cron(expr)
    alias_vi = {
        "@reboot": "chạy một lần mỗi khi máy khởi động lại",
        "@yearly": "mỗi năm một lần (00:00 ngày 1/1)",
        "@annually": "mỗi năm một lần (00:00 ngày 1/1)",
        "@monthly": "mỗi tháng một lần (00:00 ngày mùng 1)",
        "@weekly": "mỗi tuần một lần (00:00 Chủ nhật)",
        "@daily": "mỗi ngày một lần (00:00)",
        "@midnight": "mỗi ngày một lần (00:00)",
        "@hourly": "mỗi giờ một lần (vào phút 00)",
    }
    if c.alias in alias_vi:
        return alias_vi[c.alias]  # type: ignore[index]
    # Mẫu "mỗi N phút", các trường còn lại là *
    if (len(c.hour) == 24 and len(c.day) == 31 and len(c.month) == 12
            and len(c.weekday) == 7 and _is_full_step(c.minute, 0, 59)):
        return f"mỗi {c.minute[1] - c.minute[0]} phút một lần"
    # Mẫu "mỗi N giờ vào phút M"
    if (len(c.minute) == 1 and len(c.day) == 31 and len(c.month) == 12
            and len(c.weekday) == 7 and _is_full_step(c.hour, 0, 23)):
        return f"mỗi {c.hour[1] - c.hour[0]} giờ một lần (vào phút {c.minute[0]:02d})"
    # Mẫu "hằng ngày lúc H:M"
    if (len(c.minute) == 1 and len(c.hour) == 1 and len(c.day) == 31
            and len(c.month) == 12 and len(c.weekday) == 7):
        return f"hằng ngày lúc {c.hour[0]:02d}:{c.minute[0]:02d}"
    return (
        f"phút {c.minute}, giờ {c.hour}, ngày {c.day}, "
        f"tháng {c.month}, thứ {c.weekday}"
    )


# ---------------------------------------------------------------------------
# Lưu trữ jobs (file JSON)
# ---------------------------------------------------------------------------

def _default_store() -> Path:
    """Vị trí mặc định lưu jobs: ~/.boom/cron_jobs.json."""
    p = Path.home() / ".boom" / "cron_jobs.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _resolve_store(store: Optional[str | Path]) -> Path:
    return Path(store) if store else _default_store()


def _load(store: Path) -> list[dict]:
    if not store.exists():
        return []
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise CronError(f"không đọc được file jobs {store}: {exc}")
    if not isinstance(data, list):
        raise CronError(f"file jobs {store} sai định dạng (cần list)")
    return data


def _save(store: Path, jobs: list[dict]) -> None:
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")


def add(
    name: str,
    schedule: str,
    command: str,
    store: Optional[str | Path] = None,
) -> CronJob:
    """Thêm 1 cron job. Validate tên + schedule trước khi lưu, tên trùng -> lỗi."""
    name = (name or "").strip()
    if not name:
        raise CronError("tên job rỗng")
    if not _NAME_RE.match(name):
        raise CronError(f"tên job không hợp lệ (chỉ a-z, 0-9, _ . -): {name!r}")
    if not (command or "").strip():
        raise CronError("command rỗng")
    parse_cron(schedule)  # validate schedule trước khi lưu
    spath = _resolve_store(store)
    jobs = _load(spath)
    if any(j.get("name") == name for j in jobs):
        raise CronError(f"job {name!r} đã tồn tại (dùng remove() trước khi thêm lại)")
    job = CronJob(name=name, schedule=schedule.strip(), command=command.strip())
    jobs.append(job.model_dump())
    _save(spath, jobs)
    return job


def list_jobs(store: Optional[str | Path] = None) -> list[CronJob]:
    """Liệt kê tất cả jobs đã lưu."""
    return [CronJob(**j) for j in _load(_resolve_store(store))]


def remove(name: str, store: Optional[str | Path] = None) -> bool:
    """Xóa job theo tên. Trả về True nếu xóa được, False nếu không tồn tại."""
    spath = _resolve_store(store)
    jobs = _load(spath)
    kept = [j for j in jobs if j.get("name") != name]
    if len(kept) == len(jobs):
        return False
    _save(spath, kept)
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="cron-manager: quản lý cron jobs")
    ap.add_argument("--store", default=None, help="file JSON lưu jobs (mặc định ~/.boom/cron_jobs.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="thêm job")
    a.add_argument("name")
    a.add_argument("schedule", help='vd: "@daily", "@hourly", "*/5 * * * *"')
    a.add_argument("command")

    sub.add_parser("list", help="liệt kê jobs")

    r = sub.add_parser("remove", help="xóa job")
    r.add_argument("name")

    v = sub.add_parser("validate", help="kiểm tra cron expression")
    v.add_argument("expr")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "add":
            job = add(args.name, args.schedule, args.command, store=args.store)
            print(job.model_dump_json(indent=2))
            print(f"Mô tả: {describe(job.schedule)}")
        elif args.cmd == "list":
            jobs = list_jobs(store=args.store)
            print(json.dumps([j.model_dump() for j in jobs], ensure_ascii=False, indent=2))
        elif args.cmd == "remove":
            ok = remove(args.name, store=args.store)
            print(json.dumps({"removed": ok}))
            return 0 if ok else 1
        elif args.cmd == "validate":
            c = parse_cron(args.expr)
            print(c.model_dump_json(indent=2))
            print(f"Mô tả: {describe(args.expr)}")
    except CronError as exc:
        print(f"Lỗi: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
