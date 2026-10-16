"""Circuit breaker for the Boom agent.

Purpose: fail fast on calls to external services (Telegram API, LLM
providers, webhooks...) while a service is down, avoiding hanging the
gateway's thread pool and avoiding request-bombing a failing service.

State lifecycle:
    CLOSED  ──(consecutive fails >= threshold)──▶ OPEN
      ▲                                            │
      │                                            │ (after recovery_timeout elapses)
      │                                            ▼
      └────(half-open call OK, counters reset)◀── HALF_OPEN
              (half-open call fails → back to OPEN, wait clock resets)

Thread-safe: all state reads/writes hold `threading.Lock`, because the
gateway uses ThreadingHTTPServer (many threads sharing one breaker).
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import threading
import time
from enum import Enum


class CircuitState(str, Enum):
    """The three states of a breaker."""

    CLOSED = "closed"       # normal: let every call through, count consecutive fails
    OPEN = "open"           # fail fast: don't call fn, raise CircuitOpenError immediately
    HALF_OPEN = "half_open"  # probing: let at most half_open_max_calls trial calls through


class CircuitOpenError(Exception):
    """Raised when the breaker is OPEN (fail fast) or HALF_OPEN is out of trial slots."""

    def __init__(self, message: str = "circuit is open"):
        super().__init__(message)


class CircuitBreaker:
    """Thread-safe circuit breaker wrapping calls to external services.

    Main API: :meth:`call` (sync) and :meth:`acall` (async).
    Also usable as a decorator ``@breaker`` or a context manager ``with breaker:``.
    """

    def __init__(
        self,
        name: str = "circuit",
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 1,
        expected_exception: type[BaseException] | tuple[type[BaseException], ...] = Exception,
        clock: callable = time.monotonic,  # type: ignore[valid-type]
    ) -> None:
        """Initialize the breaker.

        :param name: identifier (so logs/health checks can tell services apart)
        :param failure_threshold: CONSECUTIVE fails to open the circuit (CLOSED → OPEN)
        :param recovery_timeout: seconds to wait (OPEN) before probing again
        :param half_open_max_calls: max concurrent trial calls while HALF_OPEN
        :param expected_exception: which exceptions count as failures (default: all
            Exceptions; BaseExceptions like KeyboardInterrupt don't count)
        :param clock: time source (monotonic) — tests pass a fake clock to avoid real sleeps
        """
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if recovery_timeout < 0:
            raise ValueError("recovery_timeout must be >= 0")
        if half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be >= 1")

        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self.expected_exception = expected_exception
        self._clock = clock

        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failure_count = 0      # consecutive fails (reset by one success in CLOSED)
        self._success_count = 0      # total successes through the breaker (health stats)
        self._last_failure_time: float | None = None  # last failure time (for timeout math)
        self._half_open_in_flight = 0  # trial calls currently running while HALF_OPEN

    # ------------------------------------------------------------------
    # Public state (for health checks) — every getter holds the lock
    # ------------------------------------------------------------------
    @property
    def state(self) -> CircuitState:
        """Current state. If OPEN but recovery_timeout has elapsed,
        lazily transitions to HALF_OPEN before returning."""
        with self._lock:
            self._maybe_half_open_locked()
            return self._state

    @property
    def failure_count(self) -> int:
        """Current consecutive failure count (fail-fast hits while OPEN excluded)."""
        with self._lock:
            return self._failure_count

    @property
    def last_failure_time(self) -> float | None:
        """Clock time of the last failure; None if never failed."""
        with self._lock:
            return self._last_failure_time

    @property
    def success_count(self) -> int:
        """Total successful calls through the breaker since creation / reset."""
        with self._lock:
            return self._success_count

    def reset(self) -> None:
        """Return the breaker to CLOSED like new, clearing all stats."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None
            self._half_open_in_flight = 0

    # ------------------------------------------------------------------
    # Internal logic (always called with the lock held)
    # ------------------------------------------------------------------
    def _maybe_half_open_locked(self) -> None:
        """If OPEN and recovery_timeout has passed since the last failure,
        move to HALF_OPEN to allow probing. Lazy transition: state only
        changes when someone reads state / makes a call."""
        if (
            self._state is CircuitState.OPEN
            and self._last_failure_time is not None
            and (self._clock() - self._last_failure_time) >= self.recovery_timeout
        ):
            # Open for probing: reset the in-flight trial count
            self._state = CircuitState.HALF_OPEN
            self._half_open_in_flight = 0

    def _enter_call_locked(self) -> None:
        """Pre-call check. Raises CircuitOpenError if the call isn't allowed.

        - CLOSED: always let through.
        - OPEN: raise immediately (fail fast), never touch fn.
        - HALF_OPEN: let at most half_open_max_calls concurrent trials through;
          over the limit → raise like OPEN.
        """
        self._maybe_half_open_locked()
        if self._state is CircuitState.CLOSED:
            return
        if self._state is CircuitState.OPEN:
            raise CircuitOpenError(f"circuit '{self.name}' is OPEN (fail fast)")
        # HALF_OPEN: cap concurrent trial calls
        if self._half_open_in_flight >= self.half_open_max_calls:
            raise CircuitOpenError(
                f"circuit '{self.name}' is HALF_OPEN and trial slots are full"
            )
        self._half_open_in_flight += 1

    def _record_success_locked(self) -> None:
        """Successful call:
        - In HALF_OPEN → close the circuit back to CLOSED, reset all counters.
        - In CLOSED → reset the consecutive-failure streak (only CONSECUTIVE
          fails open the circuit).
        """
        self._success_count += 1
        if self._state is CircuitState.HALF_OPEN:
            # Trial succeeded → service recovered, close the circuit
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = None
            self._half_open_in_flight = 0
        else:
            self._failure_count = 0

    def _record_failure_locked(self) -> None:
        """Failed call:
        - In CLOSED: bump failure_count; hit the threshold → open (OPEN).
        - In HALF_OPEN: service still failing → re-open, restart the wait clock.
        """
        self._failure_count += 1
        self._last_failure_time = self._clock()
        if self._state is CircuitState.HALF_OPEN:
            # Trial failed → service not back, re-open and keep waiting
            self._state = CircuitState.OPEN
            self._half_open_in_flight = 0
        elif (
            self._state is CircuitState.CLOSED
            and self._failure_count >= self.failure_threshold
        ):
            # Enough consecutive fails → open the circuit
            self._state = CircuitState.OPEN

    def _release_half_open_slot_locked(self) -> None:
        """Release one trial slot (when fn was cancelled/interrupted without
        going through the normal success/failure path)."""
        if self._half_open_in_flight > 0:
            self._half_open_in_flight -= 1

    # ------------------------------------------------------------------
    # Main API
    # ------------------------------------------------------------------
    def call(self, fn, *args, **kwargs):
        """Run sync `fn` behind the breaker.

        Raises CircuitOpenError immediately if the call isn't allowed (fail fast).
        Exceptions from fn (of the expected_exception type) are counted as
        failures and re-raised to the caller — the breaker never swallows errors.
        """
        with self._lock:
            self._enter_call_locked()
        try:
            result = fn(*args, **kwargs)
        except self.expected_exception:
            with self._lock:
                self._record_failure_locked()
            raise
        except BaseException:
            # Non-counted error (e.g. KeyboardInterrupt) → just release the slot
            with self._lock:
                if self._state is CircuitState.HALF_OPEN:
                    self._release_half_open_slot_locked()
            raise
        with self._lock:
            self._record_success_locked()
        return result

    async def acall(self, fn, *args, **kwargs):
        """Run `fn` (sync or async callable) behind the breaker.

        `fn` may be a plain function or a coroutine function; an awaitable
        result is awaited. State transitions mirror `call`.
        """
        with self._lock:
            self._enter_call_locked()
        try:
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except self.expected_exception:
            with self._lock:
                self._record_failure_locked()
            raise
        except BaseException:
            with self._lock:
                if self._state is CircuitState.HALF_OPEN:
                    self._release_half_open_slot_locked()
            raise
        with self._lock:
            self._record_success_locked()
        return result

    def __call__(self, fn):
        """Use as a decorator: ``@breaker`` wraps a sync or async function.

        - Async functions (async def / returning a coroutine) → via `acall`.
        - Sync functions → via `call`.
        """
        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                return await self.acall(fn, *args, **kwargs)

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            return self.call(fn, *args, **kwargs)

        return sync_wrapper

    def __enter__(self) -> "CircuitBreaker":
        """Context manager: checks call permission on entry, fails fast here."""
        with self._lock:
            self._enter_call_locked()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        """Exits the block: no exception → success; counted exception → failure."""
        with self._lock:
            if exc_type is None:
                self._record_success_locked()
            elif issubclass(exc_type, self.expected_exception):
                self._record_failure_locked()
            elif self._state is CircuitState.HALF_OPEN:
                self._release_half_open_slot_locked()
        return False  # don't swallow exceptions, the caller handles them

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(name={self.name!r}, state={self._state.value}, "
            f"failures={self._failure_count}, successes={self._success_count})"
        )
