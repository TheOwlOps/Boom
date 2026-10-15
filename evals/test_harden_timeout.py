"""Test cho timeout guard (agent/src/lifecycle/timeout.py).

Chạy:  python3 test_harden_timeout.py
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from timeout import with_timeout  # noqa: E402

PASSED = 0
FAILED = 0


def check(name, fn):
    global PASSED, FAILED
    try:
        fn()
        PASSED += 1
        print(f"  PASS: {name}")
    except Exception as e:  # noqa: BLE001
        FAILED += 1
        print(f"  FAIL: {name}: {type(e).__name__}: {e}")


def test_sync_timeout():
    @with_timeout(2)
    def slow():
        time.sleep(10)

    t0 = time.monotonic()
    try:
        slow()
    except TimeoutError as e:
        elapsed = time.monotonic() - t0
        assert elapsed < 5, f"chờ quá lâu: {elapsed:.1f}s (phải < 5s)"
        assert "Timed out after 2s" in str(e), str(e)
        assert "slow" in str(e), str(e)
        return
    raise AssertionError("không raise TimeoutError")


def test_async_timeout():
    @with_timeout(2)
    async def slow():
        await asyncio.sleep(10)

    async def main():
        t0 = time.monotonic()
        try:
            await slow()
        except TimeoutError as e:
            elapsed = time.monotonic() - t0
            assert elapsed < 5, f"chờ quá lâu: {elapsed:.1f}s (phải < 5s)"
            assert "Timed out after 2s" in str(e), str(e)
            assert "slow" in str(e), str(e)
            return
        raise AssertionError("không raise TimeoutError")

    asyncio.run(main())


def test_fast_sync_ok():
    @with_timeout(5)
    def fast():
        time.sleep(0.1)
        return 42

    assert fast() == 42, "function nhanh phải trả kết quả bình thường"


def test_fast_async_ok():
    @with_timeout(5)
    async def fast():
        await asyncio.sleep(0.1)
        return "ok"

    assert asyncio.run(fast()) == "ok", "async function nhanh phải trả kết quả bình thường"


def test_sync_valueerror_propagates():
    @with_timeout(5)
    def boom():
        raise ValueError("lỗi gốc")

    try:
        boom()
    except ValueError as e:
        assert str(e) == "lỗi gốc", f"message bị đổi: {e}"
        return
    raise AssertionError("ValueError bị nuốt / biến thành TimeoutError")


def test_async_valueerror_propagates():
    @with_timeout(5)
    async def boom():
        raise ValueError("lỗi gốc async")

    async def main():
        try:
            await boom()
        except ValueError as e:
            assert str(e) == "lỗi gốc async", f"message bị đổi: {e}"
            return
        raise AssertionError("ValueError bị nuốt / biến thành TimeoutError")

    asyncio.run(main())


def test_wraps_keeps_name():
    @with_timeout(5)
    def my_named_function():
        """docstring gốc."""
        return 1

    assert my_named_function.__name__ == "my_named_function", my_named_function.__name__
    assert my_named_function.__doc__ == "docstring gốc.", "functools.wraps mất docstring"

    @with_timeout(5)
    async def my_async_function():
        return 1

    assert my_async_function.__name__ == "my_async_function", my_async_function.__name__


def test_invalid_timeout_rejected():
    for bad in (0, -1, -0.5):
        try:
            with_timeout(bad)(lambda: None)
        except ValueError:
            continue
        raise AssertionError(f"timeout={bad} không raise ValueError lúc decorate")


if __name__ == "__main__":
    print("test_harden_timeout.py")
    check("sync sleep 10s, timeout 2s -> TimeoutError trong ~2s", test_sync_timeout)
    check("async sleep 10s, timeout 2s -> TimeoutError", test_async_timeout)
    check("sync chạy nhanh (0.1s) -> trả kết quả bình thường", test_fast_sync_ok)
    check("async chạy nhanh (0.1s) -> trả kết quả bình thường", test_fast_async_ok)
    check("sync raise ValueError -> lan truyền, không thành TimeoutError", test_sync_valueerror_propagates)
    check("async raise ValueError -> lan truyền, không thành TimeoutError", test_async_valueerror_propagates)
    check("functools.wraps giữ __name__ (sync + async)", test_wraps_keeps_name)
    check("timeout <= 0 -> ValueError lúc decorate", test_invalid_timeout_rejected)
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)
