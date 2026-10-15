"""TTLCache — cache có thời hạn sử dụng, dùng cho skill loading và tool schemas.

Bài toán trong Boom:
- ``SkillLoader.list_skills()`` mỗi lần cache-miss quét lại toàn bộ SKILL.md.
- ``SkillsHub.search_catalog()`` đọc lại cả catalog mỗi lần search.
- Tool schemas trong environment.py được build lại mỗi lần gọi.

TTLCache cho phép cache kết quả tính toán đắt đỏ này theo key, tự hết hạn
sau TTL, và có thể đuổi mục cũ nhất (LRU) khi đầy. Toàn bộ bằng stdlib,
thread-safe (đồng hồ đơn điệu ``time.monotonic`` chống lệch giờ hệ thống).
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
    """Một mục cache: giá trị + thời điểm hết hạn (monotonic clock)."""
    value: V
    expires_at: float  # time.monotonic(); float("inf") = không bao giờ hết hạn


@dataclass
class CacheStats:
    """Thống kê hit/miss để đo hiệu quả cache."""
    hits: int = 0
    misses: int = 0

    @property
    def hit_rate(self) -> float:
        # Tỉ lệ hit: 1.0 = mọi lần get đều trúng cache.
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class TTLCache(Generic[K, V]):
    """Cache dạng dict có TTL cho từng key, thread-safe, giới hạn kích thước.

    Ví dụ dùng cho skill loading::

        skill_cache = TTLCache[str, list](maxsize=128, default_ttl=60.0)
        skills = skill_cache.get_or_set("list_skills", lambda: loader.list_skills())
    """

    def __init__(self, maxsize: int = 256, default_ttl: Optional[float] = 60.0):
        # maxsize: số key tối đa; None = không giới hạn.
        # default_ttl: TTL mặc định (giây); None = không bao giờ hết hạn.
        self.maxsize = maxsize
        self.default_ttl = default_ttl
        self._data: "OrderedDict[K, _Entry[V]]" = OrderedDict()
        self._lock = threading.RLock()
        self.stats = CacheStats()

    # ------------------------------------------------------------------
    # API chính
    # ------------------------------------------------------------------

    def _is_expired(self, entry: _Entry[V], now: float) -> bool:
        return entry.expires_at <= now

    def set(self, key: K, value: V, ttl: Optional[float] = None) -> None:
        """Ghi giá trị vào cache; ``ttl`` (giây) ghi đè default_ttl."""
        ttl = self.default_ttl if ttl is None else ttl
        expires_at = float("inf") if ttl is None else time.monotonic() + ttl
        with self._lock:
            # Xóa key cũ trước để OrderedDict coi đây là mục mới nhất (LRU).
            self._data.pop(key, None)
            self._data[key] = _Entry(value=value, expires_at=expires_at)
            self._evict_if_needed()

    def get(self, key: K, default: Optional[V] = None) -> Optional[V]:
        """Đọc giá trị; hết hạn hoặc không tồn tại -> trả ``default``."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.stats.misses += 1
                return default
            if self._is_expired(entry, time.monotonic()):
                # Mục hết hạn: xóa luôn để đỡ phình bộ nhớ.
                del self._data[key]
                self.stats.misses += 1
                return default
            # Trúng cache: đưa lên đầu LRU (giữ lại mục hay dùng).
            self._data.move_to_end(key)
            self.stats.hits += 1
            return entry.value

    def get_or_set(
        self, key: K, factory: Callable[[], V], ttl: Optional[float] = None
    ) -> V:
        """Cache-aside: trúng thì trả luôn, trượt thì gọi ``factory()`` rồi cache.

        Đây là pattern chính để bọc các hàm đắt đỏ như
        ``SkillLoader._scan_skills()`` hay build tool schemas.
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is not None and not self._is_expired(entry, time.monotonic()):
                self._data.move_to_end(key)
                self.stats.hits += 1
                return entry.value
            if entry is not None:
                del self._data[key]  # Hết hạn -> xóa.
            self.stats.misses += 1
        # Gọi factory NGOÀI lock để thread khác không bị chặn khi tính toán lâu.
        value = factory()
        self.set(key, value, ttl=ttl)
        return value

    def delete(self, key: K) -> bool:
        """Xóa một key; trả True nếu key tồn tại."""
        with self._lock:
            return self._data.pop(key, None) is not None

    def clear(self) -> None:
        """Xóa toàn bộ cache (dùng khi skill_dir thay đổi hàng loạt)."""
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
    # Nội bộ
    # ------------------------------------------------------------------

    def _evict_if_needed(self) -> None:
        """Đuổi mục ít dùng nhất (đầu OrderedDict) khi vượt maxsize."""
        if self.maxsize is None:
            return
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)


def cached(ttl: Optional[float] = 60.0, maxsize: int = 256):
    """Decorator cache kết quả hàm theo (args, kwargs) với TTL.

    Dùng cho các hàm pure/đắt đỏ như build tool schema::

        @cached(ttl=300.0)
        def build_tool_schema(tool_name: str) -> dict: ...
    """

    def decorator(fn: Callable):
        cache: TTLCache = TTLCache(maxsize=maxsize, default_ttl=ttl)

        def _make_key(args, kwargs):
            # Key phải hashable: tuple hóa args + kwargs đã sắp xếp.
            return (args, tuple(sorted(kwargs.items())))

        def wrapper(*args, **kwargs):
            return cache.get_or_set(_make_key(args, kwargs), lambda: fn(*args, **kwargs))

        wrapper.cache = cache  # type: ignore[attr-defined]
        wrapper.__wrapped__ = fn
        wrapper.__name__ = getattr(fn, "__name__", "wrapper")
        return wrapper

    return decorator
