#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Graceful shutdown cho Boom — stdlib only (signal, asyncio, threading, logging, atexit).

Mục đích: khi process nhận SIGTERM (docker stop, systemd, k8s) hoặc SIGINT
(Ctrl+C), Boom dọn dẹp có trật tự thay vì chết giữa chừng:
  1. Đánh dấu đang shutdown (``is_shutting_down()`` -> True).
  2. Thoát khỏi vòng lặp chính với exit code 0 (kể cả đang ở trong
     ``asyncio.run()`` — các task bị cancel sạch rồi loop mới đóng).
  3. Chạy các cleanup callback đã đăng ký (đóng DB, flush file...) — có timeout.
  4. Flush log (``logging.shutdown()``).

API:
    install_shutdown_handlers(graceful_timeout=30)
    register_cleanup(name, fn)      # fn: sync callable hoặc ``async def``
    is_shutting_down() -> bool
    await_shutdown(timeout=30)       # coroutine -> True/False (xem docstring)

Cách dùng nhanh::

    from agent.src.lifecycle.shutdown import (
        install_shutdown_handlers, register_cleanup, is_shutting_down,
    )

    def main():
        install_shutdown_handlers()          # dòng đầu main(), trên main thread
        db = open_db()
        register_cleanup("db", db.close)     # sync fn...
        register_cleanup("pool", pool.aclose)  # ...hoặc async fn, đều được
        asyncio.run(run_daemon())            # SIGTERM -> cleanup tự chạy, exit 0
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
    """State dùng chung, truy cập từ cả signal handler nên phải thread-safe."""

    def __init__(self) -> None:
        # threading.Event: set() an toàn ngay cả trong signal handler.
        self.event = threading.Event()
        self.lock = threading.Lock()
        # (name, fn) — chạy theo thứ tự NGƯỢC lúc đăng ký (LIFO, giống atexit):
        # tài nguyên mở sau thường phụ thuộc tài nguyên mở trước.
        self.cleanups: list = []
        self.timeout = DEFAULT_GRACEFUL_TIMEOUT
        self.installed = False
        self.cleanup_done = False


_state = _ShutdownState()


def register_cleanup(name: str, fn) -> None:
    """Đăng ký hàm dọn dẹp chạy khi shutdown (kể cả khi thoát bình thường).

    ``fn`` có thể là sync callable hoặc ``async def`` (coroutine function).
    Mỗi callback được bọc try/except riêng: một callback hỏng không chặn
    các callback còn lại.
    """
    if not callable(fn):
        raise TypeError(f"cleanup '{name}' phải là callable, nhận {type(fn).__name__}")
    with _state.lock:
        _state.cleanups.append((name, fn))
    log.debug("Đã đăng ký cleanup: %s", name)


def is_shutting_down() -> bool:
    """True nếu process đang shutdown (đã nhận signal hoặc đang thoát)."""
    return _state.event.is_set()


async def await_shutdown(timeout: float = 30) -> bool:
    """Block (async) cho tới khi shutdown được kích hoạt.

    Trả về True nếu event shutdown được set một cách "êm" (VD: set thủ công
    trong test), False nếu hết ``timeout`` giây.

    LƯU Ý: khi signal THẬT (SIGTERM/SIGINT) tới, handler gọi ``sys.exit(0)``
    ngay nên coroutine đang await sẽ nhận SystemExit thay vì return True —
    process đang thoát có trật tự nên không cần giá trị trả về nữa. Pattern
    khuyên dùng trong daemon loop là poll ``is_shutting_down()``, và đăng ký
    mọi dọn dẹp qua ``register_cleanup()``.
    """
    loop = asyncio.get_running_loop()
    # Poll thay vì run_in_executor: tránh sinh thread non-daemon — thread đó
    # mà kẹt trong event.wait() thì lúc interpreter shutdown sẽ bị join theo,
    # làm SIGTERM treo thêm tới `timeout` giây.
    deadline = loop.time() + timeout
    while not _state.event.is_set():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def install_shutdown_handlers(graceful_timeout: float = DEFAULT_GRACEFUL_TIMEOUT) -> None:
    """Cài signal handler cho SIGTERM và SIGINT + hook cleanup lúc thoát.

    PHẢI gọi từ main thread (``signal.signal`` chỉ cho phép main thread) —
    thường là dòng đầu tiên trong ``main()``. Gọi nhiều lần cũng an toàn
    (chỉ cập nhật timeout, không đăng ký atexit trùng).
    """
    # signal.signal() ném ValueError ngoài main thread; check trước để báo
    # lỗi rõ ràng thay vì traceback khó hiểu.
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError(
            "install_shutdown_handlers() phải được gọi từ main thread "
            "(CPython chỉ giao signal cho main thread)"
        )
    _state.timeout = float(graceful_timeout)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _signal_handler)
    if not _state.installed:
        # Đăng ký 1 lần duy nhất: cleanup chạy ở đây, SAU KHI stack đã unwind
        # (task bị cancel, event loop đã đóng) — đúng thứ tự graceful:
        # dừng việc đang làm trước, rồi mới giải phóng tài nguyên.
        # atexit chạy cả khi thoát bình thường (không có signal).
        atexit.register(_run_exit_cleanup)
        _state.installed = True
    log.info(
        "Đã cài graceful shutdown handlers (SIGTERM/SIGINT, timeout=%ss)",
        graceful_timeout,
    )


def _signal_handler(signum, frame):
    """Chạy ĐỒNG BỘ trên main thread khi signal tới — phải NHANH và an toàn.

    CPython chỉ giao signal cho main thread nên handler này có thể chen ngang
    bất kỳ đâu (kể cả giữa ``loop._run_once()``). Vì vậy nó KHÔNG tự chạy
    cleanup ở đây mà chỉ:
      1. set event (đánh dấu shutting down),
      2. ``sys.exit(0)`` -> SystemExit unwind stack main thread:
         - sync code (Event.wait, serve_forever): thoát block, chạy finally.
         - ``asyncio.run(...)``: Runner.__exit__ cancel các task + đóng loop
           sạch (loop KHÔNG bị stop giữa chừng nên không gặp
           "Event loop stopped before Future completed").
      3. interpreter chạy atexit -> ``_run_exit_cleanup()`` làm cleanup thật.

    Cố tình KHÔNG cancel task / stop loop từ trong handler: cancel "main task"
    mà ``asyncio.run`` đang đợi sẽ làm nó nổ CancelledError (exit 1), còn
    ``loop.stop()`` khi vẫn còn task pending sẽ làm ``Runner.close()`` nổ
    RuntimeError. sys.exit(0) là đường duy nhất cho exit 0 trong mọi context.

    Signal lần 2 (user sốt ruột Ctrl+C tiếp, hoặc signal tới giữa lúc đang
    cleanup): thoát cứng luôn bằng os._exit, không cleanup nữa.
    """
    try:
        sig_name = signal.Signals(signum).name
    except ValueError:
        sig_name = str(signum)

    if _state.event.is_set():
        log.error("Nhận %s trong lúc đang shutdown — thoát cứng ngay", sig_name)
        # Quy ước shell: 128 + số signal (130 = SIGINT, 143 = SIGTERM).
        os._exit(128 + signum)

    _state.event.set()
    log.warning(
        "Nhận %s — bắt đầu graceful shutdown (timeout cleanup %ss)",
        sig_name,
        _state.timeout,
    )
    sys.exit(0)


def _run_exit_cleanup() -> None:
    """Chạy toàn bộ cleanup đã đăng ký. Được gọi qua atexit (1 lần duy nhất).

    Được gọi SAU KHI: stack đã unwind, các asyncio task đã bị cancel, event
    loop đã đóng — nên đây là lúc an toàn để đóng DB/pool/file. Chạy trên
    worker thread daemon + join(timeout): callback nào treo quá timeout thì
    bị bỏ lại, process vẫn thoát đúng hạn.
    """
    with _state.lock:
        if _state.cleanup_done:
            return  # atexit + sys.exit không bao giờ chạy cleanup 2 lần
        _state.cleanup_done = True
        cleanups = list(reversed(_state.cleanups))
        timeout = _state.timeout
    # Set event ở đây nữa: nếu signal tới NGAY GIỮA lúc đang cleanup (VD: thoát
    # bình thường rồi user Ctrl+C), handler sẽ thấy event đã set -> os._exit
    # thay vì sys.exit lồng nhau làm rối interpreter shutdown.
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
            "Hết timeout %ss — bỏ qua cleanup chưa xong, thoát", timeout
        )
    logging.shutdown()  # flush mọi handler (file, stderr...)


def _run_all_sync(cleanups: list) -> None:
    for name, fn in cleanups:
        try:
            log.info("Cleanup: %s ...", name)
            if inspect.iscoroutinefunction(fn):
                # Đang ở worker thread, NGOÀI mọi event loop -> asyncio.run an toàn.
                asyncio.run(fn())
            else:
                fn()  # sync fn: giả định nhanh, không block (đóng DB, flush file)
            log.info("Cleanup: %s OK", name)
        except Exception:
            # Một callback hỏng không được chặn các callback còn lại.
            log.exception("Cleanup %s LỖI (bỏ qua, chạy tiếp)", name)


def _reset_for_tests() -> None:
    """Chỉ dùng cho test: xóa state để test độc lập (không dùng trong production)."""
    with _state.lock:
        _state.cleanups.clear()
        _state.cleanup_done = False
    _state.event.clear()
    _state.installed = False
    _state.timeout = DEFAULT_GRACEFUL_TIMEOUT
