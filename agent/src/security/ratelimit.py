"""Rate limit theo user (token bucket) cho Boom.

Module này nằm ở ``agent/src/security/ratelimit.py`` — dùng khi muốn giới
hạn riêng từng người dùng (user_id hoặc "channel:user_id"), khác với
``agent/src/lifecycle/ratelimit.py`` (rate limit global/theo-IP ở tầng
HTTP gateway, tính rate theo phút).

- Token bucket per key: mỗi user có bucket riêng, ``allow(key)`` tiêu 1 token.
- Refill tuyến tính theo thời gian: ``refill`` token mỗi giây.
- Thread-safe: mọi truy cập state đều bọc trong ``threading.Lock``.
- Tự cleanup key không dùng > 1h để không rò rỉ RAM.

Chỉ dùng stdlib (time, threading). Deploy: copy file này vào
``agent/src/security/ratelimit.py`` của repo.
"""

from __future__ import annotations

import threading
import time

# ---------------------------------------------------------------------------
# Preset cấu hình
# ---------------------------------------------------------------------------
# DEFAULT: 30 token burst, refill 0.5 token/giây (~1 request/2s đều đặn).
#   Phù hợp cho user tương tác thông thường.
DEFAULT = {"capacity": 30, "refill": 0.5}

# STRICT: 5 token burst, refill 0.1 token/giây (~1 request/10s đều đặn).
#   Dùng cho tool nguy hiểm (gửi tin, thao tác tiền, xóa dữ liệu...) để
#   người dùng (hoặc tool agent) không thể spam hàng loạt.
STRICT = {"capacity": 5, "refill": 0.1}

# Key không chạm tới quá thời gian này sẽ bị xóa khỏi bộ nhớ (tránh RAM
# phình khi có nhiều user thoáng qua). Mặc định 3600 giây = 1 giờ.
IDLE_TTL = 3600.0


class RateLimitError(Exception):
    """Ném ra khi user vượt giới hạn rate limit.

    Thuộc tính:
    - ``retry_after``: số giây nên chờ trước khi thử lại (>= 0).
    - ``key``: key (user) bị chặn, để log/ghi audit.
    """

    def __init__(self, key, retry_after: float) -> None:
        self.key = key
        self.retry_after = float(max(0.0, retry_after))
        # Message tiếng Việt để hiển thị trực tiếp cho user cuối.
        super().__init__(
            "Bạn thao tác quá nhanh, vui lòng thử lại sau "
            f"{self.retry_after:.1f} giây."
        )


class UserRateLimiter:
    """Token bucket rate limiter, độc lập theo từng user.

    ``capacity``: số token tối đa (burst cho phép tức thời).
    ``refill``: số token nạp lại mỗi giây (có thể là số lẻ, vd 0.5).
    ``idle_ttl``: key không dùng quá lâu này (giây) sẽ bị xóa — tránh rò RAM.

    Ví dụ:
        limiter = UserRateLimiter(**UserRateLimiter.PRESET_DEFAULT)
        key = f"telegram:{user_id}"   # hoặc chỉ user_id nếu single-channel
        if limiter.allow(key):
            ... xử lý ...
    """

    # Preset gắn vào class để caller dùng nhanh: UserRateLimiter(**UserRateLimiter.DEFAULT)
    PRESET_DEFAULT = dict(DEFAULT)
    PRESET_STRICT = dict(STRICT)

    def __init__(self, capacity: float = DEFAULT["capacity"],
                 refill: float = DEFAULT["refill"],
                 idle_ttl: float = IDLE_TTL) -> None:
        if capacity <= 0:
            raise ValueError("capacity phải > 0")
        if refill <= 0:
            raise ValueError("refill phải > 0")
        if idle_ttl <= 0:
            raise ValueError("idle_ttl phải > 0")
        self.capacity = float(capacity)
        self.refill = float(refill)
        self.idle_ttl = float(idle_ttl)
        # key -> [tokens, last_update_monotonic]
        # Dùng time.monotonic() để đo khoảng thời gian: không bị ảnh hưởng
        # khi đồng hồ hệ thống nhảy (NTP chỉnh giờ, đổi múi giờ...).
        self._buckets: dict = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ api

    def allow(self, key) -> bool:
        """True nếu user còn token (trừ 1), False nếu hết — không ném lỗi."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                # User mới: bucket đầy, trừ 1 token ngay.
                self._buckets[key] = [self.capacity - 1.0, now]
                self._maybe_cleanup_locked(now)
                return True
            # Nạp token theo thời gian đã trôi qua, không vượt capacity.
            elapsed = now - bucket[1]
            if elapsed > 0:
                bucket[0] = min(self.capacity, bucket[0] + elapsed * self.refill)
                bucket[1] = now
            if bucket[0] >= 1.0:
                bucket[0] -= 1.0
                ok = True
            else:
                ok = False
            self._maybe_cleanup_locked(now)
            return ok

    def allow_or_raise(self, key) -> None:
        """Giống ``allow`` nhưng ném ``RateLimitError`` khi hết token.

        Dùng cho code path muốn fail-fast: bắt exception ở tầng trên rồi
        báo lại user bằng chính message tiếng Việt của exception.
        """
        if not self.allow(key):
            raise RateLimitError(key, self.get_state(key)["retry_after"])

    def get_state(self, key) -> dict:
        """Trạng thái hiện tại của key, không tiêu token.

        Trả dict: {"tokens": float, "capacity": float, "retry_after": float}
        - ``tokens``: số token còn lại (đã refill tới thời điểm gọi).
        - ``retry_after``: 0.0 nếu đang được phép, ngược lại số giây cần chờ
          để có đủ 1 token.
        """
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return {"tokens": self.capacity, "capacity": self.capacity,
                        "retry_after": 0.0}
            elapsed = now - bucket[1]
            tokens = self.capacity
            if elapsed > 0:
                tokens = min(self.capacity, bucket[0] + elapsed * self.refill)
            retry_after = 0.0
            if tokens < 1.0:
                # Thời gian để nạp đủ 1 token đầy.
                retry_after = (1.0 - tokens) / self.refill
            return {"tokens": tokens, "capacity": self.capacity,
                    "retry_after": retry_after}

    def reset(self, key=None) -> None:
        """Reset manual — cho admin/unban.

        ``reset(key)``: xóa bucket của 1 user (lần gọi sau được full token).
        ``reset()``: xóa toàn bộ (dùng khi đổi cấu hình hoặc sự cố).
        """
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)

    def key_count(self) -> int:
        """Số key đang lưu — để monitor/debug xem có rò rỉ RAM không."""
        with self._lock:
            return len(self._buckets)

    # -------------------------------------------------------------- internal

    def _maybe_cleanup_locked(self, now: float) -> None:
        """Xóa key đã quá ``idle_ttl`` không chạm tới — chống rò rỉ RAM.

        Lazy cleanup: chỉ quét khi đã quá ``idle_ttl`` kể từ lần quét trước,
        và quét ngay trong ``allow()`` nên không cần thread nền riêng (đỡ
        phức tạp lifecycle, không tốn tài nguyên khi idle).

        Chỉ xóa key đã lâu không dùng; key đang bị throttle (token < 1)
        nhưng vẫn gọi liên tục thì ``last_update`` mới liên tục nên không
        bị xóa nhầm — không có kẽ hở để lách limit.
        """
        last = getattr(self, "_last_cleanup", 0.0)
        if now - last < self.idle_ttl:
            return
        dead = [k for k, (_, ts) in self._buckets.items()
                if now - ts >= self.idle_ttl]
        for k in dead:
            del self._buckets[k]
        self._last_cleanup = now


def make_key(channel: str, user_id) -> str:
    """Ghép key chuẩn ``"channel:user_id"`` — cùng user trên 2 kênh khác
    nhau sẽ có limit riêng (tránh 1 kênh spam làm khóa cả kênh còn lại)."""
    return f"{channel}:{user_id}"
