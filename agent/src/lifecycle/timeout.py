"""Timeout guard cho Boom lifecycle (agent/src/lifecycle/timeout.py).

Bọc các external call (Telegram API, LLM provider, ...) bằng decorator
@with_timeout để tránh treo vô hạn khi dependency bên ngoài không phản hồi.

Chỉ dùng stdlib: asyncio, functools, concurrent.futures, inspect.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import inspect
from typing import Any, Callable, TypeVar

__all__ = ["with_timeout"]

F = TypeVar("F", bound=Callable[..., Any])

# Pool dùng chung cho mọi sync function được bọc: giới hạn số thread, tái sử
# dụng thread thay vì spawn mới mỗi lần gọi.
#
# LÝ DO KHÔNG DÙNG signal.alarm:
#   - signal chỉ chạy được trên MAIN thread của process. Boom chạy agent loop
#     trên worker thread / async task → đặt signal.alarm ở đó sẽ raise
#     ValueError ("signal only works in main thread"), hoặc tệ hơn: alarm của
#     thread này bắn vào thread khác.
#   - signal.alarm là global state của process: 2 decorator chồng lên nhau sẽ
#     ghi đè alarm của nhau.
# ThreadPoolExecutor + concurrent.futures.wait(timeout) an toàn trên mọi
# thread, không can thiệp global state, và vẫn dùng được khi gọi từ async code.
_SYNC_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=16,
    thread_name_prefix="boom-timeout-guard",
)


def _timeout_message(seconds: float, func_name: str) -> str:
    return f"Timed out after {seconds}s: {func_name}"


def with_timeout(seconds: float) -> Callable[[F], F]:
    """Decorator giới hạn thời gian chạy của function.

    Dùng cho CẢ sync và async function:
      - async: asyncio.wait_for
      - sync:  submit vào ThreadPoolExecutor + concurrent.futures.wait(timeout)

    Hết timeout → raise TimeoutError (builtin) với message rõ ràng.
    Exception do function tự raise thì lan truyền nguyên vẹn (không bị nuốt).

    Raises:
        ValueError: nếu timeout <= 0 (ngay lúc decorate).
    """
    if seconds <= 0:
        raise ValueError(f"timeout phải là số dương, nhận được: {seconds!r}")

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await asyncio.wait_for(func(*args, **kwargs), timeout=seconds)
                except asyncio.TimeoutError as exc:
                    # Python 3.11+: asyncio.TimeoutError CHÍNH LÀ builtin
                    # TimeoutError. Nếu function TỰ raise TimeoutError có message
                    # riêng (wait_for không bao giờ tự sinh message) thì giữ
                    # nguyên, không bọc lại.
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
                # Thread vẫn chạy nền cho tới khi xong (Python không kill được
                # thread), nhưng caller đã được giải phóng đúng hạn. Pool giới
                # hạn max_workers nên không leak vô hạn.
                raise TimeoutError(_timeout_message(seconds, func.__name__))
            # future.result() raise lại ĐÚNG exception gốc của function.
            return future.result()

        return sync_wrapper  # type: ignore[return-value]

    return decorator
