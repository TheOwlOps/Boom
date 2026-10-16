"""Lightweight profiling: the ``timeit`` decorator + the ``ProfileReport`` aggregate.

Purpose: measure how long skill loading / tool schema builds / catalog
searches take, to find the real bottleneck instead of guessing.

Usage::

    from performance.profile import timeit, get_report

    @timeit("skill_scan")
    def scan_skills(): ...

    @timeit  # default name = the function's name
    def search_catalog(q): ...

    print(get_report().summary())

Or measure any code block with the context manager::

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
    """One measurement: name + runtime (seconds)."""
    name: str
    elapsed: float


@dataclass
class _Aggregate:
    """Per-name aggregate: call count, total time, min, max."""
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
    """Collects TimingRecords, thread-safe, prints a report sorted by total time."""

    def __init__(self):
        self._lock = threading.Lock()
        self._records: List[TimingRecord] = []
        self._agg: Dict[str, _Aggregate] = {}

    def record(self, name: str, elapsed: float) -> None:
        """Records one measurement in the report."""
        with self._lock:
            self._records.append(TimingRecord(name=name, elapsed=elapsed))
            self._agg.setdefault(name, _Aggregate()).add(elapsed)

    def reset(self) -> None:
        """Clears all measured data (use between benchmarks)."""
        with self._lock:
            self._records.clear()
            self._agg.clear()

    @property
    def total_calls(self) -> int:
        with self._lock:
            return len(self._records)

    def summary(self, limit: Optional[int] = None) -> str:
        """Text table: name | calls | total(s) | avg(ms) | min(ms) | max(ms).

        Sorted by descending total time — the bottleneck is on the first line.
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
            lines.append("(no measurements yet)")
        return "\n".join(lines)

    def totals(self) -> Dict[str, float]:
        """Returns {name: total_time} for assertions in tests."""
        with self._lock:
            return {name: agg.total for name, agg in self._agg.items()}

    def call_counts(self) -> Dict[str, int]:
        """Returns {name: call_count} for assertions in tests."""
        with self._lock:
            return {name: agg.calls for name, agg in self._agg.items()}


# Global report: every @timeit without its own report writes here.
_global_report = ProfileReport()


def get_report() -> ProfileReport:
    """Gets the global profiling report."""
    return _global_report


def timeit(_fn=None, *, name: Optional[str] = None, report: Optional[ProfileReport] = None):
    """Decorator measuring a function's runtime, usable both ways::

        @timeit
        def f(): ...

        @timeit("skill_scan")
        def g(): ...

    Preserves the original function's name/docstring/return value (functools.wraps).
    """
    target_report = report if report is not None else _global_report

    def decorator(fn: Callable):
        label = name or getattr(fn, "__name__", "anonymous")

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()  # the most precise clock for durations.
            try:
                return fn(*args, **kwargs)
            finally:
                target_report.record(label, time.perf_counter() - start)

        return wrapper

    # Supports both @timeit (no parens) and @timeit("label") (with parens).
    if _fn is not None and callable(_fn):
        return decorator(_fn)
    return decorator


@contextmanager
def timed(name: str, report: Optional[ProfileReport] = None) -> Iterator[None]:
    """Measures any code block::

        with timed("skill_scan"):
            skills = loader.list_skills()
    """
    target_report = report if report is not None else _global_report
    start = time.perf_counter()
    try:
        yield
    finally:
        target_report.record(name, time.perf_counter() - start)
