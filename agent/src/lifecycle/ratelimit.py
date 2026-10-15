"""Rate limiter per-key kiểu token bucket cho Boom gateway.

Chỉ dùng stdlib (time, threading). Thread-safe vì gateway dùng
``ThreadingHTTPServer`` — mỗi request chạy trên một thread riêng nên mọi
truy cập state đều bọc trong ``threading.Lock``.

Đặt module ở ``agent/src/lifecycle/ratelimit.py`` để deploy vào repo.
"""

from __future__ import annotations

import math
import threading
import time


class RateLimiter:
    """Token bucket rate limiter, độc lập theo key.

    - ``rate``: số token nạp lại mỗi phút (mặc định 60 requests/minute).
    - ``burst``: sức chứa tối đa của bucket — số request cho phép vượt tức
      thời trước khi bị chặn (mặc định = ``rate``).
    - ``key``: bất kỳ hashable nào — IP, user_id, channel, ... caller tự chọn.

    Mỗi key có một bucket riêng; mỗi ``allow(key)`` tiêu 1 token. Token nạp
    lại tuyến tính theo thời gian: ``rate`` token / 60 giây.
    """

    def __init__(self, rate: float = 60.0, burst: float | None = None,
                 cleanup_interval: float = 300.0) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if burst is not None and burst <= 0:
            raise ValueError("burst must be > 0")
        self.rate = float(rate)
        self.burst = float(burst if burst is not None else rate)
        self._tokens_per_sec = self.rate / 60.0
        self.cleanup_interval = float(cleanup_interval)
        # key -> [tokens, last_refill_monotonic]
        self._buckets: dict = {}
        self._last_cleanup = time.monotonic()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ api

    def allow(self, key) -> bool:
        """Trả True nếu request được phép (tiêu 1 token), False nếu từ chối."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = [self.burst, now]
                self._buckets[key] = bucket
            else:
                elapsed = now - bucket[1]
                if elapsed > 0:
                    # Nạp token theo thời gian đã trôi qua, không vượt capacity.
                    bucket[0] = min(self.burst, bucket[0] + elapsed * self._tokens_per_sec)
                    bucket[1] = now
            if bucket[0] >= 1.0:
                bucket[0] -= 1.0
                ok = True
            else:
                ok = False
            # Tự dọn bucket cũ (lazy cleanup): thay vì chạy thread nền định
            # kỳ (tốn tài nguyên + phức tạp lifecycle), ta tranh thủ mỗi lần
            # allow() gọi: nếu đã quá `cleanup_interval` giây kể từ lần dọn
            # trước thì quét xóa các bucket đã đầy từ lâu. Bucket đầy nghĩa là
            # key đó đã không tiêu token một thời gian dài → key chết (IP cũ,
            # user đã offline). Cách này tránh memory leak khi attacker spam
            # key mới liên tục, mà không cần timer riêng.
            if now - self._last_cleanup >= self.cleanup_interval:
                self._cleanup_locked(now)
            return ok

    def get_retry_after(self, key) -> float:
        """Số giây caller nên chờ trước khi thử lại (0.0 nếu đang cho phép)."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return 0.0
            elapsed = now - bucket[1]
            tokens = min(self.burst, bucket[0] + elapsed * self._tokens_per_sec)
            if tokens >= 1.0:
                return 0.0
            # Thời gian để nạp đủ 1 token.
            wait = (1.0 - tokens) / self._tokens_per_sec
            return max(0.0, wait)

    def remaining(self, key) -> float:
        """Số token hiện còn của key (hữu ích cho header X-RateLimit)."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return self.burst
            elapsed = now - bucket[1]
            return min(self.burst, bucket[0] + elapsed * self._tokens_per_sec)

    def reset(self, key=None) -> None:
        """Xóa bucket của một key, hoặc toàn bộ nếu ``key`` là None."""
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)

    # -------------------------------------------------------------- internal

    def _cleanup_locked(self, now: float) -> None:
        """Xóa bucket đã đầy (>= capacity) — key chết, không tốn refill."""
        # Bucket đầy + đã lâu không chạm = key đã ngừng dùng. Chỉ xóa khi
        # full để không vô tình xóa key đang bị throttle (token < 1) — làm
        # vậy sẽ reset bộ đếm và cho attacker lách limit.
        dead = [k for k, (tokens, last) in self._buckets.items()
                if tokens >= self.burst and now - last >= self.cleanup_interval]
        for k in dead:
            del self._buckets[k]
        self._last_cleanup = now


def rate_limit_middleware(handler, limiter: RateLimiter, key_fn):
    """Kiểm tra rate limit cho một request của BaseHTTPRequestHandler.

    - ``handler``: instance của ``BaseHTTPRequestHandler`` (ví dụ ``_Handler``).
    - ``limiter``: ``RateLimiter`` dùng chung.
    - ``key_fn``: ``handler -> hashable`` — trích key từ request
      (ví dụ ``lambda h: h.client_address[0]`` cho IP, hoặc user_id từ header).

    Trả ``True`` nếu được phép đi tiếp, ``False`` nếu đã trả 429
    (kèm header ``Retry-After`` + body JSON theo contract của api.py).
    """
    key = key_fn(handler)
    if limiter.allow(key):
        return True
    retry_after = limiter.get_retry_after(key)
    body = (
        '{"error": "rate limit exceeded", '
        '"retry_after_seconds": %.1f}' % retry_after
    ).encode("utf-8")
    handler.send_response(429)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    # Retry-After theo RFC 7231: số giây (nguyên, làm tròn lên).
    handler.send_header("Retry-After", str(max(1, math.ceil(retry_after))))
    handler.end_headers()
    handler.wfile.write(body)
    return False


class RateLimitMixin:
    """Mixin cho ``_Handler``: gọi ``self._check_rate_limit()`` ở đầu
    ``do_GET``/``do_POST``/``do_OPTIONS``; trả False → dừng xử lý ngay.

    Cách dùng: ``class _Handler(RateLimitMixin, BaseHTTPRequestHandler)``,
    gán ``_Handler.rate_limiter`` và ``_Handler.rate_limit_key`` ở nơi khởi
    tạo server (xem README).
    """

    rate_limiter: RateLimiter | None = None
    rate_limit_key = staticmethod(lambda h: h.client_address[0])

    def _check_rate_limit(self) -> bool:
        limiter = self.rate_limiter
        if limiter is None:  # chưa cấu hình → cho qua (fail-open ở dev)
            return True
        return rate_limit_middleware(self, limiter, self.rate_limit_key)
