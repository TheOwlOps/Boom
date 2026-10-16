"""TTLCache — an expiring cache, used for skill loading and tool schemas.

The problem in Boom:
- ``SkillLoader.list_skills()`` rescans every SKILL.md on each cache-miss.
- ``SkillsHub.search_catalog()`` rereads the whole catalog on each search.
- Tool schemas in environment.py are rebuilt on every call.

TTLCache caches these expensive results by key, expires them after a TTL,
and can evict the oldest entries (LRU) when full. Pure stdlib, thread-safe
(monotonic ``time.monotonic`` clock, immune to system clock skew).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generic, Hashable, Optional, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


@dataclass
class _Entry(Generic[V]):
    """One cache entry: value + expiry time (monotonic clock)."""
    value: V
    expires_at: float  # time.monotonic(); float("inf") = never expires


@dataclass
class CacheStats:
    """Hit/miss stats for measuring cache effectiveness."""
    hits: int = 0
    misses: int = 0

    @property
    def hit_rate(self) -> float:
        # Hit rate: 1.0 = every get hit the cache.
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class TTLCache(Generic[K, V]):
    """Dict-like cache with per-key TTL, thread-safe, size-bounded.

    Skill loading example::

        skill_cache = TTLCache[str, list](maxsize=128, default_ttl=60.0)
        skills = skill_cache.get_or_set("list_skills", lambda: loader.list_skills())
    """

    def __init__(self, maxsize: int = 256, default_ttl: Optional[float] = 60.0):
        # maxsize: max number of keys; None = unbounded.
        # default_ttl: default TTL (seconds); None = never expires.
        self.maxsize = maxsize
        self.default_ttl = default_ttl
        self._data: "OrderedDict[K, _Entry[V]]" = OrderedDict()
        self._lock = threading.RLock()
        self.stats = CacheStats()

    # ------------------------------------------------------------------
    # Main API
    # ------------------------------------------------------------------

    def _is_expired(self, entry: _Entry[V], now: float) -> bool:
        return entry.expires_at <= now

    def set(self, key: K, value: V, ttl: Optional[float] = None) -> None:
        """Writes a value into the cache; ``ttl`` (seconds) overrides default_ttl."""
        ttl = self.default_ttl if ttl is None else ttl
        expires_at = float("inf") if ttl is None else time.monotonic() + ttl
        with self._lock:
            # Remove the old key first so OrderedDict treats this as newest (LRU).
            self._data.pop(key, None)
            self._data[key] = _Entry(value=value, expires_at=expires_at)
            self._evict_if_needed()

    def get(self, key: K, default: Optional[V] = None) -> Optional[V]:
        """Reads a value; expired or missing -> returns ``default``."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.stats.misses += 1
                return default
            if self._is_expired(entry, time.monotonic()):
                # Expired entry: delete right away to bound memory.
                del self._data[key]
                self.stats.misses += 1
                return default
            # Cache hit: move to the LRU front (keeps hot entries).
            self._data.move_to_end(key)
            self.stats.hits += 1
            return entry.value

    def get_or_set(
        self, key: K, factory: Callable[[], V], ttl: Optional[float] = None
    ) -> V:
        """Cache-aside: hit returns immediately, miss calls ``factory()`` then caches.

        The main pattern for wrapping expensive functions like
        ``SkillLoader._scan_skills()`` or tool schema building.
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is not None and not self._is_expired(entry, time.monotonic()):
                self._data.move_to_end(key)
                self.stats.hits += 1
                return entry.value
            if entry is not None:
                del self._data[key]  # Expired -> delete.
            self.stats.misses += 1
        # Calls the factory OUTSIDE the lock so other threads aren't blocked
        # during slow computation.
        value = factory()
        self.set(key, value, ttl=ttl)
        return value

    def delete(self, key: K) -> bool:
        """Deletes one key; returns True when the key existed."""
        with self._lock:
            return self._data.pop(key, None) is not None

    def clear(self) -> None:
        """Clears the whole cache (use when skill_dir changes in bulk)."""
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __contains__(self, key: K) -> bool:
        with self._lock:
            entry = self._data.get(key)
            return entry is not None and not self._is_expired(entry, time.monotonic())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _evict_if_needed(self) -> None:
        """Evicts the least-recently-used entry (OrderedDict head) past maxsize."""
        if self.maxsize is None:
            return
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)


def cached(ttl: Optional[float] = 60.0, maxsize: int = 256):
    """Decorator caching a function's result by (args, kwargs) with TTL.

    For pure/expensive functions like tool schema builders::

        @cached(ttl=300.0)
        def build_tool_schema(tool_name: str) -> dict: ...
    """

    def decorator(fn: Callable):
        cache: TTLCache = TTLCache(maxsize=maxsize, default_ttl=ttl)

        def _make_key(args, kwargs):
            # Keys must be hashable: tuple-ify args + sorted kwargs.
            return (args, tuple(sorted(kwargs.items())))

        def wrapper(*args, **kwargs):
            return cache.get_or_set(_make_key(args, kwargs), lambda: fn(*args, **kwargs))

        wrapper.cache = cache  # type: ignore[attr-defined]
        wrapper.__wrapped__ = fn
        wrapper.__name__ = getattr(fn, "__name__", "wrapper")
        return wrapper

    return decorator
