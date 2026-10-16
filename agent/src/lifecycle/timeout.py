"""Timeout guard for the Boom lifecycle (agent/src/lifecycle/timeout.py).

Wraps external calls (Telegram API, LLM providers, ...) with a @with_timeout
decorator so a non-responsive outside dependency can't hang forever.

Stdlib only: asyncio, functools, concurrent.futures, inspect.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import inspect
from typing import Any, Callable, TypeVar

__all__ = ["with_timeout"]

F = TypeVar("F", bound=Callable[..., Any])

# Shared pool for every wrapped sync function: bounded threads, reused
# instead of spawning new ones per call.
#
# WHY NOT signal.alarm:
#   - signals only work on the process's MAIN thread. Boom runs the agent loop
#     on worker threads / async tasks → signal.alarm there raises ValueError
#     ("signal only works in main thread"), or worse: this thread's alarm
#     fires into another thread.
#   - signal.alarm is process-global state: 2 stacked decorators would
#     overwrite each other's alarm.
# ThreadPoolExecutor + concurrent.futures.wait(timeout) is safe on every
# thread, touches no global state, and still works when called from async code.
_SYNC_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=16,
    thread_name_prefix="boom-timeout-guard",
)


def _timeout_message(seconds: float, func_name: str) -> str:
    return f"Timed out after {seconds}s: {func_name}"


def with_timeout(seconds: float) -> Callable[[F], F]:
    """Decorator limiting a function's runtime.

    Works for BOTH sync and async functions:
      - async: asyncio.wait_for
      - sync:  submits to a ThreadPoolExecutor + concurrent.futures.wait(timeout)

    On timeout → raises TimeoutError (builtin) with a clear message.
    Exceptions raised by the function itself propagate untouched (never swallowed).

    Raises:
        ValueError: when timeout <= 0 (at decoration time).
    """
    if seconds <= 0:
        raise ValueError(f"timeout must be a positive number, got: {seconds!r}")

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await asyncio.wait_for(func(*args, **kwargs), timeout=seconds)
                except asyncio.TimeoutError as exc:
                    # Python 3.11+: asyncio.TimeoutError IS the builtin
                    # TimeoutError. When the function ITSELF raised TimeoutError
                    # with its own message (wait_for never generates one itself),
                    # keep it as-is, don't rewrap.
                    if str(exc):
                        raise
                    raise TimeoutError(
                        _timeout_message(seconds, func.__name__)
                    ) from None

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            future = _SYNC_POOL.submit(func, *args, **kwargs)
            done, _not_done = concurrent.futures.wait([future], timeout=seconds)
            if not done:
                # The thread keeps running in the background until done (Python
                # can't kill threads), but the caller is released on time. The
                # pool caps max_workers so there's no unbounded leak.
                raise TimeoutError(_timeout_message(seconds, func.__name__))
            # future.result() re-raises the function's ORIGINAL exception.
            return future.result()

        return sync_wrapper  # type: ignore[return-value]

    return decorator
