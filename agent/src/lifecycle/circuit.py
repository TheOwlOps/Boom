"""Circuit breaker cho Boom agent.

Mục đích: chặn (fail fast) các call tới dịch vụ ngoài (Telegram API, LLM
provider, webhook...) khi dịch vụ đang chết, tránh treo thread pool của
gateway và tránh dội bom request vào dịch vụ đang lỗi.

Vòng đời trạng thái:
    CLOSED  ──(fail liên tiếp >= threshold)──▶ OPEN
      ▲                                        │
      │                                        │ (chờ hết recovery_timeout)
      │                                        ▼
      └────(half-open call OK, reset đếm)◀── HALF_OPEN
              (half-open call fail → OPEN lại, đếm thời gian chờ tiếp)

Thread-safe: mọi đọc/ghi trạng thái đều giữ `threading.Lock`, vì gateway
dùng ThreadingHTTPServer (nhiều thread cùng bọc 1 breaker).
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import threading
import time
from enum import Enum


class CircuitState(str, Enum):
    """Ba trạng thái của breaker."""

    CLOSED = "closed"       # bình thường: cho mọi call qua, đếm số fail liên tiếp
    OPEN = "open"           # fail fast: không gọi fn, raise CircuitOpenError ngay
    HALF_OPEN = "half_open"  # thăm dò: cho tối đa half_open_max_calls call thử qua


class CircuitOpenError(Exception):
    """Raise khi breaker đang OPEN (fail fast) hoặc HALF_OPEN đã hết lượt thăm dò."""

    def __init__(self, message: str = "circuit is open"):
        super().__init__(message)


class CircuitBreaker:
    """Circuit breaker thread-safe bọc các call tới dịch vụ ngoài.

    API chính: :meth:`call` (sync) và :meth:`acall` (async).
    Cũng dùng được như decorator ``@breaker`` hoặc context manager ``with breaker:``.
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
        """Khởi tạo breaker.

        :param name: tên định danh (để log/health check phân biệt từng dịch vụ)
        :param failure_threshold: số fail LIÊN TIẾP để mở mạch (state CLOSED → OPEN)
        :param recovery_timeout: số giây chờ (OPEN) trước khi cho thăm dò lại
        :param half_open_max_calls: số call thăm dò tối đa khi HALF_OPEN
        :param expected_exception: exception nào thì tính là fail (mặc định mọi Exception;
            BaseException như KeyboardInterrupt không tính)
        :param clock: hàm lấy thời gian (monotonic) — test truyền fake clock để không phải sleep thật
        """
        if failure_threshold < 1:
            raise ValueError("failure_threshold phải >= 1")
        if recovery_timeout < 0:
            raise ValueError("recovery_timeout phải >= 0")
        if half_open_max_calls < 1:
            raise ValueError("half_open_max_calls phải >= 1")

        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self.expected_exception = expected_exception
        self._clock = clock

        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failure_count = 0      # số fail liên tiếp (reset khi có 1 success ở CLOSED)
        self._success_count = 0      # tổng số success qua breaker (thống kê health)
        self._last_failure_time: float | None = None  # thời điểm fail cuối (để tính timeout)
        self._half_open_in_flight = 0  # số call thăm dò đang chạy khi HALF_OPEN

    # ------------------------------------------------------------------
    # Trạng thái công khai (đọc để health check) — mỗi getter đều giữ lock
    # ------------------------------------------------------------------
    @property
    def state(self) -> CircuitState:
        """Trạng thái hiện tại. Nếu đang OPEN nhưng đã hết recovery_timeout,
        tự chuyển sang HALF_OPEN (lazy transition) rồi mới trả về."""
        with self._lock:
            self._maybe_half_open_locked()
            return self._state

    @property
    def failure_count(self) -> int:
        """Số fail liên tiếp hiện tại (chưa tính các lượt fail fast khi OPEN)."""
        with self._lock:
            return self._failure_count

    @property
    def last_failure_time(self) -> float | None:
        """Thời điểm (theo clock) của lần fail cuối; None nếu chưa từng fail."""
        with self._lock:
            return self._last_failure_time

    @property
    def success_count(self) -> int:
        """Tổng số call thành công qua breaker kể từ khi tạo / reset."""
        with self._lock:
            return self._success_count

    def reset(self) -> None:
        """Đưa breaker về CLOSED như mới, xóa mọi thống kê."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None
            self._half_open_in_flight = 0

    # ------------------------------------------------------------------
    # Logic nội bộ (luôn gọi khi đã giữ lock)
    # ------------------------------------------------------------------
    def _maybe_half_open_locked(self) -> None:
        """Nếu đang OPEN và đã quá recovery_timeout kể từ lần fail cuối,
        chuyển sang HALF_OPEN để cho phép thăm dò. Đây là lazy transition:
        chỉ đổi trạng thái khi có người đọc state / gọi call."""
        if (
            self._state is CircuitState.OPEN
            and self._last_failure_time is not None
            and (self._clock() - self._last_failure_time) >= self.recovery_timeout
        ):
            # Mở thăm dò: reset số lượt thăm dò đang chạy
            self._state = CircuitState.HALF_OPEN
            self._half_open_in_flight = 0

    def _enter_call_locked(self) -> None:
        """Kiểm tra trước khi gọi fn. Raise CircuitOpenError nếu không được phép.

        - CLOSED: luôn cho qua.
        - OPEN: raise ngay (fail fast), không chạm tới fn.
        - HALF_OPEN: cho qua tối đa half_open_max_calls lượt đồng thời;
          quá giới hạn → raise như OPEN.
        """
        self._maybe_half_open_locked()
        if self._state is CircuitState.CLOSED:
            return
        if self._state is CircuitState.OPEN:
            raise CircuitOpenError(f"circuit '{self.name}' is OPEN (fail fast)")
        # HALF_OPEN: giới hạn số lượt thăm dò đồng thời
        if self._half_open_in_flight >= self.half_open_max_calls:
            raise CircuitOpenError(
                f"circuit '{self.name}' is HALF_OPEN and trial slots are full"
            )
        self._half_open_in_flight += 1

    def _record_success_locked(self) -> None:
        """Call thành công:
        - Ở HALF_OPEN → đóng mạch về CLOSED, reset mọi đếm.
        - Ở CLOSED → reset chuỗi fail liên tiếp (vì fail phải LIÊN TIẾP mới mở mạch).
        """
        self._success_count += 1
        if self._state is CircuitState.HALF_OPEN:
            # Thăm dò thành công → dịch vụ đã hồi phục, đóng mạch
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = None
            self._half_open_in_flight = 0
        else:
            self._failure_count = 0

    def _record_failure_locked(self) -> None:
        """Call thất bại:
        - Ở CLOSED: tăng failure_count; đủ ngưỡng → mở mạch (OPEN).
        - Ở HALF_OPEN: dịch vụ vẫn lỗi → mở mạch lại, đặt lại đồng hồ chờ.
        """
        self._failure_count += 1
        self._last_failure_time = self._clock()
        if self._state is CircuitState.HALF_OPEN:
            # Thăm dò thất bại → dịch vụ chưa hồi, mở lại, chờ tiếp
            self._state = CircuitState.OPEN
            self._half_open_in_flight = 0
        elif (
            self._state is CircuitState.CLOSED
            and self._failure_count >= self.failure_threshold
        ):
            # Đủ số fail liên tiếp → mở mạch
            self._state = CircuitState.OPEN

    def _release_half_open_slot_locked(self) -> None:
        """Giải phóng 1 slot thăm dò (dùng khi fn bị cancel/interrupt mà
        không đi qua đường success/failure thông thường)."""
        if self._half_open_in_flight > 0:
            self._half_open_in_flight -= 1

    # ------------------------------------------------------------------
    # API chính
    # ------------------------------------------------------------------
    def call(self, fn, *args, **kwargs):
        """Chạy `fn` (sync) có breaker bảo vệ.

        Raise CircuitOpenError ngay nếu không được phép gọi (fail fast).
        Exception từ fn (đúng loại expected_exception) được đếm là fail rồi
        raise lại cho caller — breaker không nuốt lỗi.
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
            # Lỗi không thuộc diện tính fail (vd KeyboardInterrupt) → chỉ nhả slot
            with self._lock:
                if self._state is CircuitState.HALF_OPEN:
                    self._release_half_open_slot_locked()
            raise
        with self._lock:
            self._record_success_locked()
        return result

    async def acall(self, fn, *args, **kwargs):
        """Chạy `fn` (sync hoặc async callable) có breaker bảo vệ.

        `fn` có thể là hàm thường hoặc coroutine function; nếu kết quả là
        awaitable thì sẽ được await. Logic chuyển trạng thái giống `call`.
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
        """Dùng như decorator: ``@breaker`` bọc hàm sync hoặc async.

        - Hàm async (async def / trả về coroutine) → gọi qua `acall`.
        - Hàm sync → gọi qua `call`.
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
        """Context manager: kiểm tra quyền gọi khi vào block, fail fast ở đây."""
        with self._lock:
            self._enter_call_locked()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        """Thoát block: không exception → success; exception thuộc diện fail → đếm fail."""
        with self._lock:
            if exc_type is None:
                self._record_success_locked()
            elif issubclass(exc_type, self.expected_exception):
                self._record_failure_locked()
            elif self._state is CircuitState.HALF_OPEN:
                self._release_half_open_slot_locked()
        return False  # không nuốt exception, caller tự xử lý

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(name={self.name!r}, state={self._state.value}, "
            f"failures={self._failure_count}, successes={self._success_count})"
        )
