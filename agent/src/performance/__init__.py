"""Performance package for Boom: TTL cache, lazy import, lightweight profiling.

Modules:
- ``cache``: TTLCache + ``cached`` decorator — caches skill loading, tool schemas.
- ``lazy``: LazyLoader + ``lazy_import`` — defers heavy imports until first use.
- ``profile``: ``timeit`` decorator, ``timed`` context manager,
  ``ProfileReport``/``get_report`` — bottleneck measurement.
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
