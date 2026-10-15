"""Profiling nhẹ: decorator ``timeit`` + ``ProfileReport`` tổng hợp.

Mục đích: đo xem skill loading / tool schema build / search catalog
tốn bao lâu, để biết bottleneck thật ở đâu thay vì đoán.

Cách dùng::

    from performance.profile import timeit, get_report

    @timeit("skill_scan")
    def scan_skills(): ...

    @timeit  # tên mặc định = tên hàm
    def search_catalog(q): ...

    print(get_report().summary())

Hoặc đo một đoạn code bất kỳ bằng context manager::

    with timed("heavy_section"):
        ...
"""

from __future__ import annotations

import functools
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional


@dataclass
class TimingRecord:
    """Một lần đo: tên + thời gian chạy (giây)."""
    name: str
    elapsed: float


@dataclass
class _Aggregate:
    """Tổng hợp theo tên: số lần gọi, tổng thời gian, min, max."""
    calls: int = 0
    total: float = 0.0
    min: float = field(default_factory=lambda: float("inf"))
    max: float = 0.0

    @property
    def avg(self) -> float:
        return self.total / self.calls if self.calls else 0.0

    def add(self, elapsed: float) -> None:
        self.calls += 1
        self.total += elapsed
        self.min = min(self.min, elapsed)
        self.max = max(self.max, elapsed)


class ProfileReport:
    """Thu thập các TimingRecord, thread-safe, in báo cáo sắp xếp theo tổng thời gian."""

    def __init__(self):
        self._lock = threading.Lock()
        self._records: List[TimingRecord] = []
        self._agg: Dict[str, _Aggregate] = {}

    def record(self, name: str, elapsed: float) -> None:
        """Ghi một lần đo vào báo cáo."""
        with self._lock:
            self._records.append(TimingRecord(name=name, elapsed=elapsed))
            self._agg.setdefault(name, _Aggregate()).add(elapsed)

    def reset(self) -> None:
        """Xóa toàn bộ dữ liệu đo (dùng giữa các lần benchmark)."""
        with self._lock:
            self._records.clear()
            self._agg.clear()

    @property
    def total_calls(self) -> int:
        with self._lock:
            return len(self._records)

    def summary(self, limit: Optional[int] = None) -> str:
        """Bảng text: name | calls | total(s) | avg(ms) | min(ms) | max(ms).

        Sắp xếp theo tổng thời gian giảm dần — bottleneck nằm ở dòng đầu.
        """
        with self._lock:
            items = sorted(self._agg.items(), key=lambda kv: kv[1].total, reverse=True)
        if limit is not None:
            items = items[:limit]
        lines = [
            f"{'name':<32} {'calls':>6} {'total(s)':>10} {'avg(ms)':>10} {'min(ms)':>10} {'max(ms)':>10}",
            "-" * 92,
        ]
        for name, agg in items:
            lines.append(
                f"{name:<32} {agg.calls:>6} {agg.total:>10.4f} "
                f"{agg.avg * 1000:>10.2f} {agg.min * 1000:>10.2f} {agg.max * 1000:>10.2f}"
            )
        if not items:
            lines.append("(chưa có dữ liệu đo nào)")
        return "\n".join(lines)

    def totals(self) -> Dict[str, float]:
        """Trả về {name: tổng_thời_gian} để assert trong test."""
        with self._lock:
            return {name: agg.total for name, agg in self._agg.items()}

    def call_counts(self) -> Dict[str, int]:
        """Trả về {name: số_lần_gọi} để assert trong test."""
        with self._lock:
            return {name: agg.calls for name, agg in self._agg.items()}


# Báo cáo toàn cục: mọi @timeit không truyền report riêng đều ghi vào đây.
_global_report = ProfileReport()


def get_report() -> ProfileReport:
    """Lấy báo cáo profiling toàn cục."""
    return _global_report


def timeit(_fn=None, *, name: Optional[str] = None, report: Optional[ProfileReport] = None):
    """Decorator đo thời gian chạy hàm, dùng được cả 2 kiểu::

        @timeit
        def f(): ...

        @timeit("skill_scan")
        def g(): ...

    Giữ nguyên tên/docstring/return value của hàm gốc (functools.wraps).
    """
    target_report = report if report is not None else _global_report

    def decorator(fn: Callable):
        label = name or getattr(fn, "__name__", "anonymous")

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()  # Đồng hồ chính xác nhất để đo khoảng thời gian.
            try:
                return fn(*args, **kwargs)
            finally:
                target_report.record(label, time.perf_counter() - start)

        return wrapper

    # Hỗ trợ cả @timeit (không ngoặc) và @timeit("label") (có ngoặc).
    if _fn is not None and callable(_fn):
        return decorator(_fn)
    return decorator


@contextmanager
def timed(name: str, report: Optional[ProfileReport] = None) -> Iterator[None]:
    """Đo một khối code bất kỳ::

        with timed("skill_scan"):
            skills = loader.list_skills()
    """
    target_report = report if report is not None else _global_report
    start = time.perf_counter()
    try:
        yield
    finally:
        target_report.record(name, time.perf_counter() - start)
