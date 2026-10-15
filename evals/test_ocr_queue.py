"""Tests for OCRQueue (bounded concurrency, cache, backpressure).

Offline: _run_tesseract is mocked. The original sync functions are untouched.
"""
import asyncio
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent", "src"))
from tools import ocr_tool

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL:", name)


def make_png(path, seed=b"\x00"):
    # .png extension passes _looks_like_image; content is irrelevant (mocked).
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + seed * 100)


class ConcurrencyMock:
    """Mock _run_tesseract tracking max simultaneous calls."""

    def __init__(self, delay=0.2):
        self.delay = delay
        self.lock = threading.Lock()
        self.current = 0
        self.max_seen = 0
        self.calls = 0

    def __call__(self, image_path, lang):
        with self.lock:
            self.current += 1
            self.calls += 1
            self.max_seen = max(self.max_seen, self.current)
        try:
            time.sleep(self.delay)
        finally:
            with self.lock:
                self.current -= 1
        return {"text": "mocked " + os.path.basename(image_path),
                "lang": lang, "char_count": 7}


async def test_concurrency_limit(tmp):
    mock = ConcurrencyMock(delay=0.15)
    orig = ocr_tool._run_tesseract
    ocr_tool._run_tesseract = mock
    try:
        q = ocr_tool.OCRQueue(max_workers=2)
        paths = []
        for i in range(10):
            p = os.path.join(tmp, "c%d.png" % i)
            make_png(p, bytes([i]))
            paths.append(p)
        ids = [q.submit(p) for p in paths]
        check("concurrency: all submits return job ids",
              all(isinstance(i, str) for i in ids))
        results = await asyncio.gather(*[q.get_result(i, timeout=30) for i in ids])
        check("concurrency: all done", all(r["status"] == "done" for r in results))
        check("concurrency: max simultaneous <= 2 (got %d)" % mock.max_seen,
              mock.max_seen <= 2)
        check("concurrency: max simultaneous >= 2 (actually parallel)",
              mock.max_seen >= 2)
        s = q.stats()
        check("concurrency: stats done == 10", s["done"] == 10)
    finally:
        ocr_tool._run_tesseract = orig
        try:
            await q.aclose()
        except Exception:
            pass


async def test_cache_hit(tmp):
    mock = ConcurrencyMock(delay=0.05)
    orig = ocr_tool._run_tesseract
    ocr_tool._run_tesseract = mock
    try:
        q = ocr_tool.OCRQueue(max_workers=2, cache_size=10)
        p = os.path.join(tmp, "cache.png")
        make_png(p, b"CACHE")
        id1 = q.submit(p)
        r1 = await q.get_result(id1, timeout=10)
        check("cache: first done", r1["status"] == "done")
        calls_after_first = mock.calls
        id2 = q.submit(p)  # identical file -> cache hit
        r2 = await q.get_result(id2, timeout=10)
        check("cache: second done", r2["status"] == "done")
        check("cache: same text", r2["text"] == r1["text"])
        check("cache: tesseract called once (got %d)" % mock.calls,
              mock.calls == calls_after_first == 1)
        check("cache: stats cache_hits == 1", q.stats()["cache_hits"] == 1)
    finally:
        ocr_tool._run_tesseract = orig
        try:
            await q.aclose()
        except Exception:
            pass


async def test_inflight_dedupe(tmp):
    mock = ConcurrencyMock(delay=0.3)
    orig = ocr_tool._run_tesseract
    ocr_tool._run_tesseract = mock
    try:
        q = ocr_tool.OCRQueue(max_workers=2)
        p = os.path.join(tmp, "dedupe.png")
        make_png(p, b"DEDUP")
        id1 = q.submit(p)
        id2 = q.submit(p)  # still inflight -> same job id
        check("dedupe: same job id returned", id1 == id2)
        r = await q.get_result(id1, timeout=10)
        check("dedupe: done", r["status"] == "done")
        check("dedupe: tesseract called once", mock.calls == 1)
    finally:
        ocr_tool._run_tesseract = orig
        try:
            await q.aclose()
        except Exception:
            pass


async def test_queue_full(tmp):
    mock = ConcurrencyMock(delay=0.5)
    orig = ocr_tool._run_tesseract
    ocr_tool._run_tesseract = mock
    try:
        q = ocr_tool.OCRQueue(max_workers=1, max_pending=2)
        p1 = os.path.join(tmp, "q1.png"); make_png(p1, b"Q1")
        p2 = os.path.join(tmp, "q2.png"); make_png(p2, b"Q2")
        p3 = os.path.join(tmp, "q3.png"); make_png(p3, b"Q3")
        q.submit(p1)
        q.submit(p2)
        await asyncio.sleep(0.1)  # let worker pick up job 1
        r3 = q.submit(p3)
        check("backpressure: third submit refused",
              isinstance(r3, dict) and "queue full" in r3.get("error", ""))
        # drain
        await asyncio.sleep(1.5)
    finally:
        ocr_tool._run_tesseract = orig
        try:
            await q.aclose()
        except Exception:
            pass


async def test_get_result_timeout(tmp):
    mock = ConcurrencyMock(delay=2.0)
    orig = ocr_tool._run_tesseract
    ocr_tool._run_tesseract = mock
    try:
        q = ocr_tool.OCRQueue(max_workers=1)
        p = os.path.join(tmp, "slow.png"); make_png(p, b"SLOW")
        jid = q.submit(p)
        r = await q.get_result(jid, timeout=0.2)
        check("timeout: returns pending/running, not done",
              r["status"] in ("pending", "running"))
        r2 = await q.get_result(jid, timeout=10)
        check("timeout: eventually done", r2["status"] == "done")
    finally:
        ocr_tool._run_tesseract = orig
        try:
            await q.aclose()
        except Exception:
            pass


async def test_unknown_job():
    q = ocr_tool.OCRQueue()
    r = await q.get_result("ocr_nope", timeout=1)
    check("unknown job -> error",
          r["status"] == "error" and "unknown job_id" in r["error"])
    await q.aclose()


async def test_validation(tmp):
    q = ocr_tool.OCRQueue()
    r = q.submit("")
    check("empty path -> error", isinstance(r, dict) and "error" in r)
    r = q.submit("/nonexistent/x.png")
    check("missing file -> error", isinstance(r, dict) and "error" in r)
    txt = os.path.join(tmp, "note.txt")
    with open(txt, "w") as f:
        f.write("hello")
    r = q.submit(txt)
    check("non-image -> error", isinstance(r, dict) and "error" in r)
    p = os.path.join(tmp, "v.png"); make_png(p, b"V")
    r = q.submit(p, lang="  ")
    check("empty lang -> error", isinstance(r, dict) and "error" in r)


async def test_error_result(tmp):
    def boom(path, lang):
        return {"error": "tesseract exploded"}
    orig = ocr_tool._run_tesseract
    ocr_tool._run_tesseract = boom
    try:
        q = ocr_tool.OCRQueue()
        p = os.path.join(tmp, "err.png"); make_png(p, b"E")
        jid = q.submit(p)
        r = await q.get_result(jid, timeout=10)
        check("tesseract error -> status error",
              r["status"] == "error" and "exploded" in r["error"])
    finally:
        ocr_tool._run_tesseract = orig
        try:
            await q.aclose()
        except Exception:
            pass


def test_sync_wrapper_no_loop(tmp):
    mock = ConcurrencyMock(delay=0.05)
    orig = ocr_tool._run_tesseract
    ocr_tool._run_tesseract = mock
    try:
        p = os.path.join(tmp, "w.png"); make_png(p, b"W")
        r = ocr_tool.ocr_image_queued(p, timeout=10)
        check("sync wrapper (no loop) -> text",
              "text" in r and r["text"].startswith("mocked"))
        r2 = ocr_tool.ocr_image_queued("/nonexistent.png", timeout=5)
        check("sync wrapper bad file -> error", "error" in r2)
    finally:
        ocr_tool._run_tesseract = orig


async def test_sync_wrapper_in_loop(tmp):
    mock = ConcurrencyMock(delay=0.05)
    orig = ocr_tool._run_tesseract
    ocr_tool._run_tesseract = mock
    try:
        p = os.path.join(tmp, "wl.png"); make_png(p, b"WL")
        # called from inside a running loop -> helper-thread path
        r = ocr_tool.ocr_image_queued(p, timeout=15)
        check("sync wrapper (in loop) -> text",
              "text" in r and r["text"].startswith("mocked"))
    finally:
        ocr_tool._run_tesseract = orig
        try:
            await q.aclose()
        except Exception:
            pass


def test_build_tools():
    tools = ocr_tool.build_tools()
    check("build_tools has ocr_image", "ocr_image" in tools)
    check("build_tools has ocr_image_url", "ocr_image_url" in tools)
    check("build_tools has ocr_image_queued", "ocr_image_queued" in tools)
    check("queued tool callable", callable(tools["ocr_image_queued"]["func"]))


async def amain(tmp):
    await test_concurrency_limit(tmp)
    await test_cache_hit(tmp)
    await test_inflight_dedupe(tmp)
    await test_queue_full(tmp)
    await test_get_result_timeout(tmp)
    await test_unknown_job()
    await test_validation(tmp)
    await test_error_result(tmp)
    await test_sync_wrapper_in_loop(tmp)


def main():
    tmp = tempfile.mkdtemp(prefix="ocrq_test_")
    try:
        test_sync_wrapper_no_loop(tmp)  # no running loop here
        test_build_tools()
        asyncio.run(amain(tmp))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("=" * 40)
    print("PASS: %d  FAIL: %d" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
