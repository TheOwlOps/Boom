#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Graceful shutdown for Boom — stdlib only (signal, asyncio, threading, logging, atexit).

Purpose: when the process receives SIGTERM (docker stop, systemd, k8s) or
SIGINT (Ctrl+C), Boom cleans up in an orderly way instead of dying mid-flight:
  1. Marks shutting down (``is_shutting_down()`` -> True).
  2. Exits the main loop with exit code 0 (even inside ``asyncio.run()`` —
     tasks are cancelled cleanly before the loop closes).
  3. Runs registered cleanup callbacks (close DB, flush files...) — with timeout.
  4. Flushes logs (``logging.shutdown()``).

API:
    install_shutdown_handlers(graceful_timeout=30)
    register_cleanup(name, fn)      # fn: sync callable or ``async def``
    is_shutting_down() -> bool
    await_shutdown(timeout=30)       # coroutine -> True/False (see docstring)

Quick usage::

    from agent.src.lifecycle.shutdown import (
        install_shutdown_handlers, register_cleanup, is_shutting_down,
    )

    def main():
        install_shutdown_handlers()          # first line of main(), on the main thread
        db = open_db()
        register_cleanup("db", db.close)     # sync fn...
        register_cleanup("pool", pool.aclose)  # ...or async fn, both fine
        asyncio.run(run_daemon())            # SIGTERM -> cleanup runs, exit 0
"""

from __future__ import annotations

import asyncio
import atexit
import inspect
import logging
import os
import signal
import sys
import threading

log = logging.getLogger("boom.lifecycle.shutdown")

DEFAULT_GRACEFUL_TIMEOUT = 30.0


class _ShutdownState:
    """Shared state, accessed from the signal handler too — must be thread-safe."""

    def __init__(self) -> None:
        # threading.Event: set() is safe even inside a signal handler.
        self.event = threading.Event()
        self.lock = threading.Lock()
        # (name, fn) — run in REVERSE registration order (LIFO, like atexit):
        # resources opened later usually depend on resources opened earlier.
        self.cleanups: list = []
        self.timeout = DEFAULT_GRACEFUL_TIMEOUT
        self.installed = False
        self.cleanup_done = False


_state = _ShutdownState()


def register_cleanup(name: str, fn) -> None:
    """Register a cleanup function to run at shutdown (including normal exit).

    ``fn`` can be a sync callable or an ``async def`` (coroutine function).
    Each callback is wrapped in its own try/except: one failing callback must
    not block the rest.
    """
    if not callable(fn):
        raise TypeError(f"cleanup '{name}' must be callable, got {type(fn).__name__}")
    with _state.lock:
        _state.cleanups.append((name, fn))
    log.debug("Registered cleanup: %s", name)


def is_shutting_down() -> bool:
    """True if the process is shutting down (signal received or exiting)."""
    return _state.event.is_set()


async def await_shutdown(timeout: float = 30) -> bool:
    """Block (async) until shutdown is triggered.

    Returns True if the shutdown event was set "gracefully" (e.g. set manually
    in tests), False after ``timeout`` seconds.

    NOTE: when a REAL signal (SIGTERM/SIGINT) arrives, the handler calls
    ``sys.exit(0)`` immediately, so an awaiting coroutine gets SystemExit
    instead of returning True — the process is already exiting in an orderly
    way, so the return value no longer matters. The recommended pattern in a
    daemon loop is to poll ``is_shutting_down()``, and register all cleanup
    through ``register_cleanup()``.
    """
    loop = asyncio.get_running_loop()
    # Poll instead of run_in_executor: avoids spawning a non-daemon thread — a
    # thread stuck in event.wait() would be joined during interpreter shutdown,
    # hanging SIGTERM for up to `timeout` extra seconds.
    deadline = loop.time() + timeout
    while not _state.event.is_set():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def install_shutdown_handlers(graceful_timeout: float = DEFAULT_GRACEFUL_TIMEOUT) -> None:
    """Install signal handlers for SIGTERM and SIGINT + an exit cleanup hook.

    MUST be called from the main thread (``signal.signal`` only allows the
    main thread) — typically the first line of ``main()``. Calling repeatedly
    is safe (only updates the timeout, no duplicate atexit registration).
    """
    # signal.signal() raises ValueError outside the main thread; check early
    # for a clear error instead of a confusing traceback.
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError(
            "install_shutdown_handlers() must be called from the main thread "
            "(CPython only delivers signals to the main thread)"
        )
    _state.timeout = float(graceful_timeout)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _signal_handler)
    if not _state.installed:
        # Register exactly once: cleanup runs here, AFTER the stack has unwound
        # (tasks cancelled, event loop closed) — the correct graceful order:
        # stop the work in progress first, then release resources.
        # atexit also runs on normal exits (no signal).
        atexit.register(_run_exit_cleanup)
        _state.installed = True
    log.info(
        "Installed graceful shutdown handlers (SIGTERM/SIGINT, timeout=%ss)",
        graceful_timeout,
    )


def _signal_handler(signum, frame):
    """Runs SYNCHRONOUSLY on the main thread when a signal arrives — must be FAST and safe.

    CPython only delivers signals to the main thread, so this handler can
    interrupt anywhere (even mid ``loop._run_once()``). That's why it does NOT
    run cleanup here; it only:
      1. sets the event (marks shutting down),
      2. calls ``sys.exit(0)`` -> SystemExit unwinds the main-thread stack:
         - sync code (Event.wait, serve_forever): unblocks, runs finally.
         - ``asyncio.run(...)``: Runner.__exit__ cancels tasks + closes the loop
           cleanly (the loop is NOT stopped mid-flight, so no
           "Event loop stopped before Future completed").
      3. the interpreter runs atexit -> ``_run_exit_cleanup()`` does the real cleanup.

    Deliberately does NOT cancel tasks / stop the loop from the handler:
    cancelling the "main task" that ``asyncio.run`` is awaiting would raise
    CancelledError (exit 1), and ``loop.stop()`` with pending tasks would make
    ``Runner.close()`` raise RuntimeError. sys.exit(0) is the only path to
    exit 0 in every context.

    A second signal (impatient user hitting Ctrl+C again, or a signal arriving
    mid-cleanup): hard-exit immediately via os._exit, no more cleanup.
    """
    try:
        sig_name = signal.Signals(signum).name
    except ValueError:
        sig_name = str(signum)

    if _state.event.is_set():
        log.error("Received %s during shutdown — hard exiting now", sig_name)
        # Shell convention: 128 + signal number (130 = SIGINT, 143 = SIGTERM).
        os._exit(128 + signum)

    _state.event.set()
    log.warning(
        "Received %s — starting graceful shutdown (cleanup timeout %ss)",
        sig_name,
        _state.timeout,
    )
    sys.exit(0)


def _run_exit_cleanup() -> None:
    """Runs all registered cleanups. Invoked via atexit (exactly once).

    Runs AFTER: the stack has unwound, asyncio tasks were cancelled, the event
    loop closed — so this is the safe point to close DBs/pools/files. Runs on
    a daemon worker thread + join(timeout): a callback hanging past the
    timeout is abandoned, the process still exits on time.
    """
    with _state.lock:
        if _state.cleanup_done:
            return  # atexit + sys.exit never run cleanup twice
        _state.cleanup_done = True
        cleanups = list(reversed(_state.cleanups))
        timeout = _state.timeout
    # Set the event here too: if a signal arrives RIGHT IN THE MIDDLE of
    # cleanup (e.g. a normal exit interrupted by the user's Ctrl+C), the
    # handler sees the event set -> os._exit instead of a nested sys.exit
    # confusing interpreter shutdown.
    _state.event.set()

    if not cleanups:
        logging.shutdown()
        return

    worker = threading.Thread(
        target=_run_all_sync, args=(cleanups,), name="boom-shutdown", daemon=True
    )
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        log.warning(
            "Cleanup timed out after %ss — skipping unfinished cleanups, exiting",
            timeout,
        )
    logging.shutdown()  # flush all handlers (file, stderr...)


def _run_all_sync(cleanups: list) -> None:
    for name, fn in cleanups:
        try:
            log.info("Cleanup: %s ...", name)
            if inspect.iscoroutinefunction(fn):
                # On a worker thread, outside every event loop -> asyncio.run is safe.
                asyncio.run(fn())
            else:
                fn()  # sync fn: assumed quick and non-blocking (close DB, flush file)
            log.info("Cleanup: %s OK", name)
        except Exception:
            # One failing callback must not block the remaining ones.
            log.exception("Cleanup %s FAILED (skipped, continuing)", name)


def _reset_for_tests() -> None:
    """Tests only: reset state so tests are independent (never use in production)."""
    with _state.lock:
        _state.cleanups.clear()
        _state.cleanup_done = False
    _state.event.clear()
    _state.installed = False
    _state.timeout = DEFAULT_GRACEFUL_TIMEOUT
