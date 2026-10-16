"""Structured JSON logging for the Boom agent.

This module wraps stdlib logging so every log record has a fixed JSON shape:
    {"timestamp", "level", "name", "message", "request_id", "context"}

- timestamp: ISO8601 UTC (with the Z suffix)
- request_id: attached via bind()/contextvar, so async-safe
  (each coroutine keeps its own contextvar copy, no leaks between requests)
- context: arbitrary dict attached to each log call
- Never crashes on non-JSON-serializable objects (falls back to str())

Stdlib only: logging, json, contextvars, datetime, sys.
"""

from __future__ import annotations

import contextvars
import datetime
import json
import logging
import sys

# Context var holding the current request_id.
# Why a contextvar instead of thread-local: in asyncio, many requests share
# one thread but run in different tasks — thread-local would leak request_id
# across tasks, while a contextvar gives each task its own context copy.
_request_id_var: contextvars.ContextVar = contextvars.ContextVar(
    "boom_request_id", default=None
)

# Default context dict of the current task (each task gets a copy, not shared).
_context_var: contextvars.ContextVar = contextvars.ContextVar(
    "boom_log_context", default=None
)

# Remembers that setup_logging already ran, so handlers aren't duplicated.
_configured = False


def _safe_json(value):
    """Converts any value to JSON-serializable form.

    Recurses through dict/list/tuple; unknown objects fall back to str(value).
    Unexpected stringify errors are caught too — the logger must never crash.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        # sets/frozensets aren't directly JSON-able: convert to list.
        return [_safe_json(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except Exception:  # undecodable bytes fall back to str
            return str(value)
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - stringifying must never crash logging
        return "<unserializable>"


class JSONFormatter(logging.Formatter):
    """One-line-per-record JSON formatter.

    Takes request_id from the contextvar (when the record doesn't carry its
    own), merges context from the contextvar + the record's extra "context",
    and safe-json-ifies everything.
    """

    def format(self, record: logging.LogRecord) -> str:
        request_id = getattr(record, "request_id", None) or _request_id_var.get()
        record_ctx = getattr(record, "context", None) or {}
        ctx = dict(_context_var.get() or {})
        if isinstance(record_ctx, dict):
            ctx.update(record_ctx)

        payload = {
            "timestamp": datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
            "request_id": request_id,
            "context": _safe_json(ctx),
        }
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception:  # noqa: BLE001 - last-resort fallback, never crash
            payload["context"] = "<unserializable>"
            payload["message"] = str(payload["message"])
            return json.dumps(payload, ensure_ascii=False)


class _BoundLogger:
    """Adapter that pins default request_id/context onto a logger.

    Every log method (debug/info/warning/error/exception) injects request_id +
    context into the record's extra. Doesn't subclass logging.Logger so it can
    wrap any existing logger; attributes are exposed via __getattr__.
    """

    def __init__(self, logger: logging.Logger, request_id=None, context=None):
        self._logger = logger
        self._request_id = request_id
        self._context = dict(context or {})

    def __getattr__(self, item):
        return getattr(self._logger, item)

    def _extra(self, context=None):
        merged = dict(self._context)
        if context:
            merged.update(context)
        return {"request_id": self._request_id, "context": merged}

    def debug(self, msg, *args, context=None, **kwargs):
        self._logger.debug(msg, *args, extra=self._extra(context), **kwargs)

    def info(self, msg, *args, context=None, **kwargs):
        self._logger.info(msg, *args, extra=self._extra(context), **kwargs)

    def warning(self, msg, *args, context=None, **kwargs):
        self._logger.warning(msg, *args, extra=self._extra(context), **kwargs)

    def error(self, msg, *args, context=None, **kwargs):
        self._logger.error(msg, *args, extra=self._extra(context), **kwargs)

    def exception(self, msg, *args, context=None, **kwargs):
        self._logger.exception(msg, *args, extra=self._extra(context), **kwargs)

    def bind(self, request_id=None, **context):
        """Binds more request_id/context, returns a new adapter (old one untouched)."""
        new_ctx = dict(self._context)
        new_ctx.update(context)
        return _BoundLogger(
            self._logger,
            request_id if request_id is not None else self._request_id,
            new_ctx,
        )


def get_logger(name: str) -> logging.Logger:
    """Returns the stdlib logger for a module name.

    Call setup_logging() once beforehand for JSON output; without setup the
    logger still works with stdlib's default (lastResort) handler.
    """
    return logging.getLogger(name)


def bind(request_id=None, _logger=None, **context) -> _BoundLogger:
    """Returns an adapter with request_id + context bound.

    Two ways to use it:
      1. logger = bind(request_id="abc", logger=get_logger(__name__))
      2. logger = bind(request_id="abc")   # uses the root logger as base

    Way 2 (no logger) doesn't attach to a specific logger but SETs the
    contextvars — every log record emitted from the current task (from any
    logger) automatically carries this request_id/context, async-safe style.
    """
    if _logger is not None:
        return _BoundLogger(_logger, request_id, context)
    # Why copy-then-set: contextvars are immutable-by-convention; each task
    # sees its own copy, so setting a new dict here doesn't affect other tasks.
    base = dict(_context_var.get() or {})
    base.update(context)
    _context_var.set(base)
    if request_id is not None:
        _request_id_var.set(request_id)
    return _BoundLogger(logging.getLogger(), request_id, context)


def reset_bind():
    """Clears the bound request_id/context for the current task (end of request)."""
    _request_id_var.set(None)
    _context_var.set(None)


def setup_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Configures the root logger, exactly once.

    - json_output=True: StreamHandler(sys.stdout) + JSONFormatter,
      one JSON line per record.
    - json_output=False: short text format for devs.
    - Calling repeatedly doesn't duplicate handlers (guarded by _configured).
    """
    global _configured
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s %(message)s"
            )
        )
    root.addHandler(handler)
    # Don't let records propagate to the interpreter's default handler (avoids dupes).
    root.propagate = True
    _configured = True
