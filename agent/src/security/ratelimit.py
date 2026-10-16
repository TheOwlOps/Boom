"""Per-user rate limiting (token bucket) for Boom.

This module lives at ``agent/src/security/ratelimit.py`` — use it to limit
individual users (user_id or "channel:user_id"), unlike
``agent/src/lifecycle/ratelimit.py`` (global/per-IP rate limiting at the
HTTP gateway layer, counted per minute).

- Per-key token bucket: each user has their own bucket, ``allow(key)``
  consumes 1 token.
- Linear time-based refill: ``refill`` tokens per second.
- Thread-safe: all state access wrapped in ``threading.Lock``.
- Auto-cleans keys idle for > 1h to avoid RAM leaks.

Stdlib only (time, threading). Deploy: copy this file into the repo's
``agent/src/security/ratelimit.py``.
"""

from __future__ import annotations

import threading
import time

# ---------------------------------------------------------------------------
# Config presets
# ---------------------------------------------------------------------------
# DEFAULT: 30 token burst, 0.5 token/sec refill (~1 request per 2s steady).
#   Fits normal interactive users.
DEFAULT = {"capacity": 30, "refill": 0.5}

# STRICT: 5 token burst, 0.1 token/sec refill (~1 request per 10s steady).
#   For dangerous tools (sending messages, money ops, data deletion...) so a
#   user (or an agent tool) can't spam in bulk.
STRICT = {"capacity": 5, "refill": 0.1}

# Keys idle longer than this are dropped from memory (avoids RAM growth
# with many transient users). Default 3600 seconds = 1 hour.
IDLE_TTL = 3600.0


class RateLimitError(Exception):
    """Raised when a user exceeds the rate limit.

    Attributes:
    - ``retry_after``: seconds to wait before retrying (>= 0).
    - ``key``: the blocked key (user), for logging/audit.
    """

    def __init__(self, key, retry_after: float) -> None:
        self.key = key
        self.retry_after = float(max(0.0, retry_after))
        # Message shown directly to the end user.
        super().__init__(
            "You're going too fast, please try again in "
            f"{self.retry_after:.1f} seconds."
        )


class UserRateLimiter:
    """Token bucket rate limiter, independent per user.

    ``capacity``: max tokens (instant burst allowed).
    ``refill``: tokens refilled per second (may be fractional, e.g. 0.5).
    ``idle_ttl``: keys idle longer than this (seconds) are dropped — no RAM leak.

    Example:
        limiter = UserRateLimiter(**UserRateLimiter.PRESET_DEFAULT)
        key = f"telegram:{user_id}"   # or just user_id for single-channel
        if limiter.allow(key):
            ... handle ...
    """

    # Class-level presets for quick use: UserRateLimiter(**UserRateLimiter.DEFAULT)
    PRESET_DEFAULT = dict(DEFAULT)
    PRESET_STRICT = dict(STRICT)

    def __init__(self, capacity: float = DEFAULT["capacity"],
                 refill: float = DEFAULT["refill"],
                 idle_ttl: float = IDLE_TTL) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if refill <= 0:
            raise ValueError("refill must be > 0")
        if idle_ttl <= 0:
            raise ValueError("idle_ttl must be > 0")
        self.capacity = float(capacity)
        self.refill = float(refill)
        self.idle_ttl = float(idle_ttl)
        # key -> [tokens, last_update_monotonic]
        # time.monotonic() measures durations: immune to system clock jumps
        # (NTP corrections, timezone changes...).
        self._buckets: dict = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ api

    def allow(self, key) -> bool:
        """True if the user still has a token (consumes 1), False if empty — never raises."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                # New user: full bucket, consume 1 token immediately.
                self._buckets[key] = [self.capacity - 1.0, now]
                self._maybe_cleanup_locked(now)
                return True
            # Refill by elapsed time, never exceeding capacity.
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
        """Like ``allow`` but raises ``RateLimitError`` when out of tokens.

        For fail-fast code paths: catch the exception one layer up and report
        back to the user with the exception's own message.
        """
        if not self.allow(key):
            raise RateLimitError(key, self.get_state(key)["retry_after"])

    def get_state(self, key) -> dict:
        """Current state of a key, without consuming a token.

        Returns {"tokens": float, "capacity": float, "retry_after": float}
        - ``tokens``: tokens remaining (refilled up to call time).
        - ``retry_after``: 0.0 when currently allowed, otherwise seconds to
          wait for 1 full token.
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
                # Time to refill one full token.
                retry_after = (1.0 - tokens) / self.refill
            return {"tokens": tokens, "capacity": self.capacity,
                    "retry_after": retry_after}

    def reset(self, key=None) -> None:
        """Manual reset — for admin/unban.

        ``reset(key)``: drops one user's bucket (next call gets full tokens).
        ``reset()``: drops everything (config changes or incidents).
        """
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)

    def key_count(self) -> int:
        """Number of keys stored — for monitoring/debugging RAM leaks."""
        with self._lock:
            return len(self._buckets)

    # -------------------------------------------------------------- internal

    def _maybe_cleanup_locked(self, now: float) -> None:
        """Drops keys idle past ``idle_ttl`` — anti RAM leak.

        Lazy cleanup: only scans when ``idle_ttl`` has passed since the last
        scan, and runs inline in ``allow()`` so no background thread is needed
        (simpler lifecycle, no resources spent while idle).

        Only long-idle keys are dropped; a key being throttled (tokens < 1)
        but called continuously keeps getting fresh ``last_update`` timestamps,
        so it's never wrongly dropped — no loophole to dodge the limit.
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
    """Builds the canonical ``"channel:user_id"`` key — the same user on 2
    different channels gets separate limits (one spamming channel can't lock
    out the other)."""
    return f"{channel}:{user_id}"
