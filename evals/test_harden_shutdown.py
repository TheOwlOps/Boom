#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test graceful shutdown (agent/src/lifecycle/shutdown.py).

Mỗi test spawn 1 subprocess chạy script mẫu, gửi signal thật bằng os.kill(),
rồi verify: cleanup có chạy không, process exit code có = 0 không,
timeout có cắt cleanup chậm không.

Chạy:  python3 test_harden_shutdown.py
"""

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

MODDIR = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.gettempdir()

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def write_script(name, body):
    path = os.path.join(TMP, f"boom_shutdown_{name}_{os.getpid()}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))
    return path


def wait_ready(proc, timeout=10):
    """Đợi dòng READY từ stdout của child (đảm bảo handler đã cài xong)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line.strip() == "READY":
            return True
        if proc.poll() is not None:
            return False
    return False


def test_sigterm_cleanup_runs_and_exit_0():
    """SIGTERM -> cleanup sync chạy, process exit code 0."""
    print("Test 1: SIGTERM -> cleanup chạy, exit 0")
    marker = os.path.join(TMP, f"boom_shutdown_marker1_{os.getpid()}.txt")
    if os.path.exists(marker):
        os.remove(marker)

    script = write_script(
        "t1",
        f"""
        import sys
        sys.path.insert(0, {MODDIR!r})
        from shutdown import install_shutdown_handlers, register_cleanup
        import threading

        def close_db():
            with open({marker!r}, "w") as f:
                f.write("db-closed")

        register_cleanup("db", close_db)
        install_shutdown_handlers(graceful_timeout=10)
        print("READY", flush=True)
        threading.Event().wait()  # block cho tới khi signal tới
        """,
    )
    proc = subprocess.Popen(
        [sys.executable, script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = wait_ready(proc)
        check("child sẵn sàng (READY)", ready)
        if not ready:
            proc.kill()
            return
        os.kill(proc.pid, signal.SIGTERM)
        rc = proc.wait(timeout=15)
        out, _ = proc.communicate()
        check("exit code == 0", rc == 0, f"(nhận {rc})")
        check("cleanup callback đã chạy (marker file tồn tại)",
              os.path.exists(marker))
        if os.path.exists(marker):
            with open(marker) as f:
                check("marker ghi đúng nội dung", f.read() == "db-closed")
    except subprocess.TimeoutExpired:
        proc.kill()
        check("process thoát kịp thời", False, "(treo quá 15s)")
    finally:
        for p in (script, marker):
            if os.path.exists(p):
                os.remove(p)


def test_sigterm_timeout_cuts_slow_cleanup():
    """SIGTERM + cleanup chậm hơn timeout -> bị cắt, process vẫn exit 0 nhanh."""
    print("Test 2: cleanup chậm hơn timeout -> bị cắt, vẫn exit 0")
    started = os.path.join(TMP, f"boom_shutdown_started2_{os.getpid()}.txt")
    finished = os.path.join(TMP, f"boom_shutdown_finished2_{os.getpid()}.txt")
    for p in (started, finished):
        if os.path.exists(p):
            os.remove(p)

    script = write_script(
        "t2",
        f"""
        import sys, time
        sys.path.insert(0, {MODDIR!r})
        from shutdown import install_shutdown_handlers, register_cleanup
        import threading

        def slow_cleanup():
            with open({started!r}, "w") as f:
                f.write("started")
            time.sleep(60)  # chậm hơn timeout rất nhiều
            with open({finished!r}, "w") as f:
                f.write("finished")

        register_cleanup("slow", slow_cleanup)
        install_shutdown_handlers(graceful_timeout=2)
        print("READY", flush=True)
        threading.Event().wait()
        """,
    )
    proc = subprocess.Popen(
        [sys.executable, script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = wait_ready(proc)
        check("child sẵn sàng (READY)", ready)
        if not ready:
            proc.kill()
            return
        t0 = time.time()
        os.kill(proc.pid, signal.SIGTERM)
        rc = proc.wait(timeout=20)
        elapsed = time.time() - t0
        proc.communicate()
        check("exit code == 0", rc == 0, f"(nhận {rc})")
        check("thoát nhanh dù cleanup chậm (<%ss)" % 12, elapsed < 12,
              f"(mất {elapsed:.1f}s)")
        check("cleanup chậm đã BẮT ĐẦU (chứng tỏ timeout cắt giữa chừng)",
              os.path.exists(started))
        check("cleanup chậm KHÔNG chạy xong (bị cắt bởi timeout)",
              not os.path.exists(finished))
    except subprocess.TimeoutExpired:
        proc.kill()
        check("process thoát kịp thời", False, "(treo quá 20s)")
    finally:
        for p in (script, started, finished):
            if os.path.exists(p):
                os.remove(p)


def test_sigint_cleanup_runs_and_exit_0():
    """SIGINT (Ctrl+C) -> cleanup chạy, exit 0 (không phải KeyboardInterrupt traceback)."""
    print("Test 3: SIGINT -> cleanup chạy, exit 0")
    marker = os.path.join(TMP, f"boom_shutdown_marker3_{os.getpid()}.txt")
    if os.path.exists(marker):
        os.remove(marker)

    script = write_script(
        "t3",
        f"""
        import sys
        sys.path.insert(0, {MODDIR!r})
        from shutdown import install_shutdown_handlers, register_cleanup
        import threading

        def flush_cache():
            with open({marker!r}, "w") as f:
                f.write("cache-flushed")

        register_cleanup("cache", flush_cache)
        install_shutdown_handlers(graceful_timeout=10)
        print("READY", flush=True)
        threading.Event().wait()
        """,
    )
    proc = subprocess.Popen(
        [sys.executable, script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = wait_ready(proc)
        check("child sẵn sàng (READY)", ready)
        if not ready:
            proc.kill()
            return
        os.kill(proc.pid, signal.SIGINT)
        rc = proc.wait(timeout=15)
        _, err = proc.communicate()
        check("exit code == 0", rc == 0, f"(nhận {rc})")
        check("cleanup callback đã chạy", os.path.exists(marker))
        check("không có KeyboardInterrupt traceback",
              "KeyboardInterrupt" not in err)
    except subprocess.TimeoutExpired:
        proc.kill()
        check("process thoát kịp thời", False, "(treo quá 15s)")
    finally:
        for p in (script, marker):
            if os.path.exists(p):
                os.remove(p)


def test_asyncio_loop_sigterm():
    """SIGTERM khi đang trong asyncio.run() -> async cleanup chạy, loop dừng, exit 0."""
    print("Test 4: SIGTERM trong asyncio.run() -> async cleanup chạy, exit 0")
    marker = os.path.join(TMP, f"boom_shutdown_marker4_{os.getpid()}.txt")
    if os.path.exists(marker):
        os.remove(marker)

    script = write_script(
        "t4",
        f"""
        import sys, asyncio
        sys.path.insert(0, {MODDIR!r})
        from shutdown import install_shutdown_handlers, register_cleanup, is_shutting_down

        async def close_pool():
            await asyncio.sleep(0.1)  # giả lập đóng pool bất đồng bộ
            with open({marker!r}, "w") as f:
                f.write("pool-closed")

        async def daemon():
            print("READY", flush=True)
            while not is_shutting_down():
                await asyncio.sleep(0.2)

        def main():
            install_shutdown_handlers(graceful_timeout=10)
            register_cleanup("pool", close_pool)
            asyncio.run(daemon())

        main()
        """,
    )
    proc = subprocess.Popen(
        [sys.executable, script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = wait_ready(proc)
        check("child sẵn sàng (READY)", ready)
        if not ready:
            proc.kill()
            return
        time.sleep(0.5)  # để loop vào guồng
        os.kill(proc.pid, signal.SIGTERM)
        rc = proc.wait(timeout=15)
        proc.communicate()
        check("exit code == 0", rc == 0, f"(nhận {rc})")
        check("async cleanup đã chạy", os.path.exists(marker))
    except subprocess.TimeoutExpired:
        proc.kill()
        check("process thoát kịp thời", False, "(treo quá 15s)")
    finally:
        for p in (script, marker):
            if os.path.exists(p):
                os.remove(p)


def main():
    tests = [
        test_sigterm_cleanup_runs_and_exit_0,
        test_sigterm_timeout_cuts_slow_cleanup,
        test_sigint_cleanup_runs_and_exit_0,
        test_asyncio_loop_sigterm,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001 — test harness không được crash
            check(t.__name__, False, f"(exception: {e!r})")
        print()
    print(f"KẾT QUẢ: {PASS} pass, {FAIL} fail")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
