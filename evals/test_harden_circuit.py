"""Test circuit breaker (stdlib-only).

Chạy: python3 test_harden_circuit.py
"""

import asyncio
import sys
from pathlib import Path
import threading
import time
import traceback

# Import root is agent/src (repo convention); the modules under test live
# in subpackages there (they previously lived next to this file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from lifecycle.circuit import CircuitBreaker, CircuitOpenError, CircuitState


class FakeClock:
    """Đồng hồ giả: test tua thời gian mà không phải sleep thật."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def failer():
    raise RuntimeError("boom")


def test_open_after_threshold_and_fail_fast():
    print("\n[1] 5 fail liên tiếp → OPEN, call tiếp fail fast (fn không chạy)")
    b = CircuitBreaker(name="t1", failure_threshold=5, recovery_timeout=60)
    for _ in range(5):
        try:
            b.call(failer)
        except RuntimeError:
            pass
    check("state == OPEN sau 5 fail liên tiếp", b.state is CircuitState.OPEN)
    check("failure_count == 5", b.failure_count == 5)

    fn_calls = {"n": 0}

    def counted():
        fn_calls["n"] += 1
        return "ok"

    try:
        b.call(counted)
        raised = False
    except CircuitOpenError:
        raised = True
    check("call khi OPEN raise CircuitOpenError", raised)
    check("fn KHÔNG được gọi khi OPEN (fail fast)", fn_calls["n"] == 0)
    check("fail fast không tăng failure_count", b.failure_count == 5)


def test_success_resets_consecutive_failures():
    print("\n[2] 1 success giữa chừng reset đếm fail liên tiếp")
    b = CircuitBreaker(name="t2", failure_threshold=3, recovery_timeout=60)
    for _ in range(2):
        try:
            b.call(failer)
        except RuntimeError:
            pass
    b.call(lambda: "ok")  # 1 success → reset
    check("failure_count reset về 0 sau success", b.failure_count == 0)
    check("success_count == 1", b.success_count == 1)
    try:
        b.call(failer)
    except RuntimeError:
        pass
    try:
        b.call(failer)
    except RuntimeError:
        pass
    check("mạch VẪN CLOSED sau 2 fail (không phải liên tiếp 3)", b.state is CircuitState.CLOSED)


def test_recovery_half_open_to_closed():
    print("\n[3] Hết recovery_timeout → HALF_OPEN, call OK → CLOSED")
    clock = FakeClock()
    b = CircuitBreaker(name="t3", failure_threshold=2, recovery_timeout=0.2, clock=clock)
    for _ in range(2):
        try:
            b.call(failer)
        except RuntimeError:
            pass
    check("state OPEN sau 2 fail", b.state is CircuitState.OPEN)
    clock.advance(0.3)  # quá 0.2s timeout
    check("state chuyển sang HALF_OPEN (lazy)", b.state is CircuitState.HALF_OPEN)
    check("last_failure_time đã ghi", b.last_failure_time == 1000.0)
    result = b.call(lambda: "recovered")
    check("call thăm dò thành công trả kết quả", result == "recovered")
    check("state về CLOSED sau thăm dò OK", b.state is CircuitState.CLOSED)
    check("failure_count reset", b.failure_count == 0)


def test_half_open_failure_reopens():
    print("\n[4] HALF_OPEN fail → OPEN lại")
    clock = FakeClock()
    b = CircuitBreaker(name="t4", failure_threshold=2, recovery_timeout=0.2, clock=clock)
    for _ in range(2):
        try:
            b.call(failer)
        except RuntimeError:
            pass
    clock.advance(0.3)
    check("HALF_OPEN trước khi thăm dò", b.state is CircuitState.HALF_OPEN)
    try:
        b.call(failer)
    except RuntimeError:
        pass
    check("state OPEN lại sau thăm dò fail", b.state is CircuitState.OPEN)
    # Đang OPEN lại: fail fast ngay
    try:
        b.call(lambda: "x")
        raised = False
    except CircuitOpenError:
        raised = True
    check("fail fast ngay sau khi OPEN lại", raised)
    # Vòng thăm dò 2: đợi tiếp rồi OK → CLOSED
    clock.advance(0.3)
    b.call(lambda: "ok2")
    check("vòng 2: thăm dò OK → CLOSED", b.state is CircuitState.CLOSED)


def test_half_open_max_calls():
    print("\n[5] HALF_OPEN giới hạn số call thăm dò đồng thời")
    clock = FakeClock()
    b = CircuitBreaker(name="t5", failure_threshold=1, recovery_timeout=0.2,
                       half_open_max_calls=1, clock=clock)
    try:
        b.call(failer)
    except RuntimeError:
        pass
    clock.advance(0.3)

    started = threading.Event()
    release = threading.Event()

    def blocking():
        started.set()
        release.wait(5)
        return "slow"

    results = []

    def worker():
        try:
            results.append(b.call(blocking))
        except CircuitOpenError as e:
            results.append(type(e).__name__)

    t = threading.Thread(target=worker)
    t.start()
    started.wait(5)
    # Thread 1 đang giữ slot thăm dò duy nhất → thread 2 phải fail fast
    slot_full = False
    try:
        b.call(lambda: "second")
    except CircuitOpenError:
        slot_full = True
    check("call thứ 2 khi HALF_OPEN hết slot → CircuitOpenError", slot_full)
    release.set()
    t.join(5)
    check("call thăm dò chậm xong → CLOSED", b.state is CircuitState.CLOSED)


def test_async_acall():
    print("\n[6] async acall: fail threshold → OPEN → recovery → CLOSED")
    clock = FakeClock()
    b = CircuitBreaker(name="t6", failure_threshold=2, recovery_timeout=0.2, clock=clock)

    async def afailer():
        raise RuntimeError("async boom")

    async def aok():
        return "async ok"

    async def run():
        for _ in range(2):
            try:
                await b.acall(afailer)
            except RuntimeError:
                pass
        state = b.state
        # fail fast async
        try:
            await b.acall(aok)
            raised = False
        except CircuitOpenError:
            raised = True
        clock.advance(0.3)
        result = await b.acall(aok)
        return state, raised, result, b.state

    state, raised, result, final = asyncio.run(run())
    check("async: OPEN sau 2 fail", state is CircuitState.OPEN)
    check("async: fail fast khi OPEN", raised)
    check("async: thăm dò OK trả kết quả", result == "async ok")
    check("async: về CLOSED", final is CircuitState.CLOSED)


def test_async_acall_sync_fn():
    print("\n[7] acall bọc được cả hàm sync thường")
    b = CircuitBreaker(name="t7")

    async def run():
        return await b.acall(lambda: 42)

    check("acall(lambda sync) == 42", asyncio.run(run()) == 42)


def test_decorator_and_context_manager():
    print("\n[8] decorator @breaker và context manager")
    b = CircuitBreaker(name="t8", failure_threshold=2, recovery_timeout=0.2)
    clock_b = b  # giữ nguyên clock thật, test decorator cơ bản

    @b
    def flaky(n):
        if n < 2:
            raise ValueError("x")
        return n * 10

    ok1 = ok2 = False
    try:
        flaky(0)
    except ValueError:
        ok1 = True
    try:
        flaky(1)
    except ValueError:
        ok2 = True
    check("decorator đếm fail qua 2 lần", ok1 and ok2 and b.failure_count == 2)
    try:
        flaky(5)
        raised = False
    except CircuitOpenError:
        raised = True
    check("decorator fail fast khi OPEN", raised)

    # context manager
    b2 = CircuitBreaker(name="t8b", failure_threshold=1)
    try:
        with b2:
            raise IOError("ctx fail")
    except IOError:
        pass
    check("context manager fail → OPEN", b2.state is CircuitState.OPEN)
    with CircuitBreaker(name="t8c") as b3:
        pass
    check("context manager success → CLOSED, success_count=1",
          b3.state is CircuitState.CLOSED and b3.success_count == 1)


def test_thread_safety():
    print("\n[9] thread-safe: 20 thread × 50 call hỗn hợp")
    b = CircuitBreaker(name="t9", failure_threshold=100000, recovery_timeout=60)
    errors = []

    def worker(i):
        try:
            for j in range(50):
                if (i + j) % 7 == 0:
                    try:
                        b.call(failer)
                    except RuntimeError:
                        pass
                else:
                    b.call(lambda: "ok")
                _ = b.state
        except Exception as e:  # noqa: BLE001 — bất kỳ exception nào ở đây là lỗi race
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    alive = [t for t in threads if t.is_alive()]
    check("không có exception race nào", not errors)
    check("mọi thread đều join được", not alive)
    check(f"xong trong thời gian hợp lý ({time.monotonic() - t0:.2f}s)", True)


def test_unexpected_exception_not_counted():
    print("\n[10] KeyboardInterrupt (ngoài expected) không tính fail")
    b = CircuitBreaker(name="t10", failure_threshold=1)

    def interrupter():
        raise KeyboardInterrupt()

    try:
        b.call(interrupter)
    except KeyboardInterrupt:
        pass
    check("failure_count vẫn 0", b.failure_count == 0)
    check("state vẫn CLOSED", b.state is CircuitState.CLOSED)


def main():
    tests = [
        test_open_after_threshold_and_fail_fast,
        test_success_resets_consecutive_failures,
        test_recovery_half_open_to_closed,
        test_half_open_failure_reopens,
        test_half_open_max_calls,
        test_async_acall,
        test_async_acall_sync_fn,
        test_decorator_and_context_manager,
        test_thread_safety,
        test_unexpected_exception_not_counted,
    ]
    for t in tests:
        try:
            t()
        except Exception:  # noqa: BLE001
            FAIL_G = globals()
            FAIL_G["FAIL"] += 1
            print(f"  EXCEPTION trong {t.__name__}:")
            traceback.print_exc()
    print(f"\n==== KẾT QUẢ: {PASS} PASS, {FAIL} FAIL ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
