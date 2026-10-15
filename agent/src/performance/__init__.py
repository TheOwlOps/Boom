"""Gói performance cho Boom: cache TTL, lazy import, profiling nhẹ.

Các module:
- ``cache``: TTLCache + decorator ``cached`` — cache skill loading, tool schemas.
- ``lazy``: LazyLoader + ``lazy_import`` — hoãn import module nặng tới khi dùng.
- ``profile``: decorator ``timeit``, context manager ``timed``,
  ``ProfileReport``/``get_report`` — đo bottleneck.
"""

from .cache import TTLCache, cached
from .lazy import LazyLoader, lazy_import
from .profile import ProfileReport, get_report, timed, timeit

__all__ = [
    "TTLCache",
    "cached",
    "LazyLoader",
    "lazy_import",
    "ProfileReport",
    "get_report",
    "timed",
    "timeit",
]
