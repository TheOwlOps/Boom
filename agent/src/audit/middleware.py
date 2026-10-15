"""Audit middleware: wrap a callable so its invocations are logged.

The wrapper never breaks the wrapped function: if the audit store itself
fails, the error is swallowed and the original result is returned as-is.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from typing import Any, Callable, Optional

from audit.models import AuditEvent


def _safe_details(value: Any) -> Any:
    """Best-effort JSON-safe conversion for details."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe_details(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_details(v) for v in value]
    return repr(value)


def audit_wrap(
    fn: Callable,
    action_name: str,
    store_getter: Callable[[], Any],
    actor_getter: Callable[[], str] = lambda: "system",
    target_arg: Optional[str] = None,
    channel_arg: Optional[str] = None,
) -> Callable:
    """Wrap fn so each call appends an AuditEvent.

    - action_name: e.g. "channel.send".
    - store_getter: zero-arg callable returning an AuditStore (lazy, so the
      store can be created after wrapping).
    - actor_getter: zero-arg callable returning the actor string.
    - target_arg / channel_arg: names of fn kwargs to copy into the event.
    Works with both sync and async functions. Never raises from logging.
    """
    is_coro = inspect.iscoroutinefunction(fn)

    def _build_event(bound_args: dict, result: str, error: Optional[str]) -> AuditEvent:
        details = {k: _safe_details(v) for k, v in bound_args.items()}
        if error:
            details["error"] = error
        return AuditEvent(
            actor=actor_getter() or "system",
            action=action_name,
            target=str(bound_args.get(target_arg, "")) if target_arg else "",
            channel=str(bound_args.get(channel_arg, "")) if channel_arg else "",
            details=details,
            result=result,
        )

    def _emit(event: AuditEvent) -> None:
        try:
            store = store_getter()
            if store is not None:
                store.log(event)
        except Exception:
            pass  # audit must never break the wrapped function

    def _bind_kwargs(args: tuple, kwargs: dict) -> dict:
        try:
            sig = inspect.signature(fn)
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            return dict(bound.arguments)
        except Exception:
            return dict(kwargs)

    if is_coro:
        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            bound = _bind_kwargs(args, kwargs)
            try:
                out = await fn(*args, **kwargs)
            except Exception as exc:
                _emit(_build_event(bound, "error", f"{type(exc).__name__}: {exc}"))
                raise
            _emit(_build_event(bound, "ok", None))
            return out
        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        bound = _bind_kwargs(args, kwargs)
        try:
            out = fn(*args, **kwargs)
        except Exception as exc:
            _emit(_build_event(bound, "error", f"{type(exc).__name__}: {exc}"))
            raise
        _emit(_build_event(bound, "ok", None))
        return out
    return sync_wrapper
