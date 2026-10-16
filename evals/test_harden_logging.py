"""Eval tests cho module structured logging (agent/src/lifecycle/log.py).

Chạy:  python3 test_harden_logging.py
Mỗi test ghi log vào stdout rồi parse dòng JSON và assert các field.
"""

import asyncio
import io
import json
import logging
import sys
from pathlib import Path
import traceback

import os

# Import root is agent/src (repo convention); the modules under test live
# in subpackages there (they previously lived next to this file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from lifecycle.log import (  # noqa: E402
    bind,
    get_logger,
    reset_bind,
    setup_logging,
)

PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("PASS: %s" % name)
    else:
        FAILED += 1
        print("FAIL: %s %s" % (name, detail))


def fresh_setup(json_output=True):
    """Reset root logger + setup lại để mỗi test bắt đầu sạch."""
    from lifecycle import log as logmod

    logmod._configured = False
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    reset_bind()
    buf = io.StringIO()
    setup_logging(level="INFO", json_output=json_output)
    # Thay stream của handler vừa tạo bằng StringIO để hứng output test.
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler):
            h.setStream(buf)
    return buf


def read_records(buf):
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


# --- Test 1: 1 message với request_id + context, đủ field ---
def test_basic_fields():
    buf = fresh_setup()
    logger = get_logger("boom.test")
    logger.info("hello", extra={"request_id": "req-123", "context": {"a": 1}})
    recs = read_records(buf)
    check("test_basic_fields: có đúng 1 record", len(recs) == 1, repr(recs))
    r = recs[0]
    for field in ("timestamp", "level", "name", "message", "request_id", "context"):
        check("test_basic_fields: field '%s'" % field, field in r, repr(r))
    check("test_basic_fields: level INFO", r.get("level") == "INFO")
    check("test_basic_fields: name", r.get("name") == "boom.test")
    check("test_basic_fields: message", r.get("message") == "hello")
    check("test_basic_fields: request_id", r.get("request_id") == "req-123")
    check("test_basic_fields: context", r.get("context") == {"a": 1})
    check(
        "test_basic_fields: timestamp ISO8601",
        "T" in str(r.get("timestamp")) and str(r.get("timestamp")).endswith("+00:00"),
        repr(r.get("timestamp")),
    )


# --- Test 2: bind() lan truyền request_id qua các lần log ---
def test_bind_propagation():
    buf = fresh_setup()
    logger = get_logger("boom.bind")
    bound = bind("req-bind-1", _logger=logger)
    bound.info("first")
    bound.info("second", context={"step": 2})
    recs = read_records(buf)
    check("test_bind: 2 records", len(recs) == 2, repr(recs))
    check(
        "test_bind: request_id lan truyền cả 2 lần",
        all(r.get("request_id") == "req-bind-1" for r in recs),
        repr(recs),
    )
    check(
        "test_bind: context lần 2",
        recs[1].get("context", {}).get("step") == 2,
        repr(recs),
    )
    # bind nối tiếp không làm mất request_id cũ
    chained = bound.bind(user="son")
    chained.info("third")
    recs = read_records(buf)
    check(
        "test_bind: chained giữ request_id",
        recs[2].get("request_id") == "req-bind-1",
        repr(recs[2]),
    )
    check(
        "test_bind: chained có context mới",
        recs[2].get("context", {}).get("user") == "son",
        repr(recs[2]),
    )


# --- Test 3: bind() không logger -> contextvar, async-safe ---
def test_bind_contextvar_async():
    buf = fresh_setup()

    async def worker(rid, results):
        bound = bind(rid)  # set contextvar của task này
        await asyncio.sleep(0)  # nhường scheduler, task khác chen vào
        get_logger("boom.async").info("from %s" % rid)
        results.append(bound)

    async def main():
        results = []
        await asyncio.gather(
            worker("req-A", results), worker("req-B", results)
        )

    asyncio.run(main())
    recs = read_records(buf)
    got = sorted(r.get("request_id") for r in recs)
    check(
        "test_async: mỗi task giữ đúng request_id của mình",
        got == ["req-A", "req-B"],
        repr(got),
    )
    check("test_async: đúng 2 records", len(recs) == 2, repr(recs))


# --- Test 4: object không JSON-serializable không crash ---
def test_unserializable_no_crash():
    buf = fresh_setup()
    logger = get_logger("boom.unser")

    class Weird:
        def __str__(self):
            return "weird-obj"

    crashed = False
    try:
        logger.info(
            "payload",
            extra={
                "request_id": "req-x",
                "context": {"obj": Weird(), "set": {1, 2}, "fn": len},
            },
        )
    except Exception:
        crashed = True
    check("test_unser: không crash", not crashed)
    recs = read_records(buf)
    check("test_unser: vẫn ra 1 record JSON hợp lệ", len(recs) == 1, repr(buf.getvalue()))
    ctx = recs[0].get("context", {})
    check("test_unser: object -> str", ctx.get("obj") == "weird-obj", repr(ctx))
    check("test_unser: set -> list", ctx.get("set") == [1, 2], repr(ctx))

    # Cả object có __str__ cũng raise
    class EvilStr:
        def __str__(self):
            raise RuntimeError("boom")

    crashed2 = False
    try:
        logger.info("evil", extra={"context": {"e": EvilStr()}})
    except Exception:
        crashed2 = True
    check("test_unser: __str__ raise cũng không crash", not crashed2)
    recs = read_records(buf)
    check(
        "test_unser: fallback <unserializable>",
        recs[-1].get("context", {}).get("e") == "<unserializable>",
        repr(recs[-1]),
    )


# --- Test 5: json_output=False ra text, không phải JSON ---
def test_text_mode():
    buf = fresh_setup(json_output=False)
    get_logger("boom.text").warning("plain mode")
    out = buf.getvalue().strip()
    try:
        json.loads(out)
        is_json = True
    except Exception:
        is_json = False
    check("test_text: output không phải JSON", not is_json, out)
    check("test_text: chứa message", "plain mode" in out, out)
    check("test_text: chứa WARNING", "WARNING" in out, out)


# --- Test 6: setup_logging gọi 2 lần không nhân đôi handler ---
def test_idempotent_setup():
    from lifecycle import log as logmod

    logmod._configured = False
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    setup_logging()
    setup_logging()
    n = len(root.handlers)
    check("test_idempotent: chỉ 1 handler", n == 1, "handlers=%d" % n)
    # dọn sạch để test khác không ảnh hưởng
    for h in list(root.handlers):
        root.removeHandler(h)


if __name__ == "__main__":
    try:
        test_basic_fields()
        test_bind_propagation()
        test_bind_contextvar_async()
        test_unserializable_no_crash()
        test_text_mode()
        test_idempotent_setup()
    except Exception:
        FAILED += 1
        print("EXCEPTION trong test run:")
        traceback.print_exc()
    print("----")
    print("PASSED: %d, FAILED: %d" % (PASSED, FAILED))
    sys.exit(1 if FAILED else 0)
