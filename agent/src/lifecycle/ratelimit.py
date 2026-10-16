"""Per-key token-bucket rate limiter for the Boom gateway.

Stdlib only (time, threading). Thread-safe because the gateway uses
``ThreadingHTTPServer`` — each request runs on its own thread, so all
state access is wrapped in ``threading.Lock``.

The module lives at ``agent/src/lifecycle/ratelimit.py`` for repo deployment.
"""

from __future__ import annotations

import math
import threading
import time


class RateLimiter:
    """Token bucket rate limiter, independent per key.

    - ``rate``: tokens refilled per minute (default 60 requests/minute).
    - ``burst``: bucket capacity — requests allowed to exceed momentarily
      before being blocked (default = ``rate``).
    - ``key``: any hashable — IP, user_id, channel, ... the caller's choice.

    Each key has its own bucket; each ``allow(key)`` spends 1 token. Tokens
    refill linearly with time: ``rate`` tokens / 60 seconds.
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
        """Returns True when the request is allowed (spends 1 token), False when denied."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = [self.burst, now]
                self._buckets[key] = bucket
            else:
                elapsed = now - bucket[1]
                if elapsed > 0:
                    # Refill tokens by elapsed time, capped at capacity.
                    bucket[0] = min(self.burst, bucket[0] + elapsed * self._tokens_per_sec)
                    bucket[1] = now
            if bucket[0] >= 1.0:
                bucket[0] -= 1.0
                ok = True
            else:
                ok = False
            # Self-cleaning old buckets (lazy cleanup): instead of a periodic
            # background thread (resource waste + lifecycle complexity), we
            # piggyback each allow() call: when `cleanup_interval` seconds have
            # passed since the last sweep, delete long-full buckets. A full
            # bucket means the key hasn't spent tokens in a long time → dead
            # key (old IP, offline user). This avoids memory leaks when an
            # attacker spams new keys, with no dedicated timer.
            if now - self._last_cleanup >= self.cleanup_interval:
                self._cleanup_locked(now)
            return ok

    def get_retry_after(self, key) -> float:
        """Seconds the caller should wait before retrying (0.0 when currently allowed)."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return 0.0
            elapsed = now - bucket[1]
            tokens = min(self.burst, bucket[0] + elapsed * self._tokens_per_sec)
            if tokens >= 1.0:
                return 0.0
            # Time to refill 1 token.
            wait = (1.0 - tokens) / self._tokens_per_sec
            return max(0.0, wait)

    def remaining(self, key) -> float:
        """Tokens currently left for a key (useful for the X-RateLimit header)."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return self.burst
            elapsed = now - bucket[1]
            return min(self.burst, bucket[0] + elapsed * self._tokens_per_sec)

    def reset(self, key=None) -> None:
        """Clears one key's bucket, or everything when ``key`` is None."""
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)

    # -------------------------------------------------------------- internal

    def _cleanup_locked(self, now: float) -> None:
        """Deletes full (>= capacity) buckets — dead keys, no refill cost."""
        # A full + long-untouched bucket = the key stopped being used. Only
        # delete full ones so a currently-throttled key (tokens < 1) is never
        # wiped — that would reset the counter and let an attacker bypass the limit.
        dead = [k for k, (tokens, last) in self._buckets.items()
                if tokens >= self.burst and now - last >= self.cleanup_interval]
        for k in dead:
            del self._buckets[k]
        self._last_cleanup = now


def rate_limit_middleware(handler, limiter: RateLimiter, key_fn):
    """Checks the rate limit for one BaseHTTPRequestHandler request.

    - ``handler``: a ``BaseHTTPRequestHandler`` instance (e.g. ``_Handler``).
    - ``limiter``: the shared ``RateLimiter``.
    - ``key_fn``: ``handler -> hashable`` — extracts the key from the request
      (e.g. ``lambda h: h.client_address[0]`` for IP, or user_id from a header).

    Returns ``True`` to proceed, ``False`` when a 429 was already sent
    (with ``Retry-After`` header + JSON body per api.py's contract).
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
    # Retry-After per RFC 7231: seconds (integer, rounded up).
    handler.send_header("Retry-After", str(max(1, math.ceil(retry_after))))
    handler.end_headers()
    handler.wfile.write(body)
    return False


class RateLimitMixin:
    """Mixin for ``_Handler``: call ``self._check_rate_limit()`` at the top of
    ``do_GET``/``do_POST``/``do_OPTIONS``; a False return stops handling right away.

    Usage: ``class _Handler(RateLimitMixin, BaseHTTPRequestHandler)``,
    then assign ``_Handler.rate_limiter`` and ``_Handler.rate_limit_key`` where
    the server is created (see README).
    """

    rate_limiter: RateLimiter | None = None
    rate_limit_key = staticmethod(lambda h: h.client_address[0])

    def _check_rate_limit(self) -> bool:
        limiter = self.rate_limiter
        if limiter is None:  # not configured → let through (fail-open in dev)
            return True
        return rate_limit_middleware(self, limiter, self.rate_limit_key)
