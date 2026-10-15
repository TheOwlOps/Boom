"""Test cho module self-healing của agent loop Boom.

Chạy:  python3 test_selfheal.py
Yêu cầu: stdlib + pydantic. Không ngủ thật (sleep được inject).
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src" / "loop"))

from selfheal import (
    ContextOverflowError,
    ErrorClassifier,
    FatalError,
    RetryConfig,
    RetryPolicy,
    SelfHealingFailed,
    SelfHealingLoop,
    TransientError,
)

PASSED = 0


def check(cond: bool, name: str) -> None:
    """Một assertion có tên; fail thì dừng ngay để thấy rõ lỗi."""
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


def make_loop(**kw):
    """SelfHealingLoop với sleep giả (không chờ thật) và jitter tắt để test ổn định."""
    sleeps = []
    kw.setdefault("sleep", sleeps.append)
    cfg = kw.pop("retry_config", None) or RetryConfig(jitter=0.0)
    return SelfHealingLoop(retry_config=cfg, rng=random.Random(42), **kw), sleeps


class FakeContextManager:
    """ContextManager giả: đếm số lần compress() được gọi."""

    def __init__(self):
        self.compress_calls = 0

    def compress(self, keep_recent=None):
        self.compress_calls += 1
        return "compressed"


def flaky(failures, exc_factory):
    """Tạo func lỗi `failures` lần rồi trả 'done'."""
    calls = {"n": 0}

    def f():
        calls["n"] += 1
        if calls["n"] <= failures:
            raise exc_factory()
        return "done"

    f.calls = calls
    return f


# ---------------------------------------------------------------- classify --
print("== ErrorClassifier ==")
c = ErrorClassifier()

r = c.classify(TimeoutError("request timed out"))
check(r.category == "transient" and r.retryable and r.backoff_sec > 0,
      "TimeoutError -> transient, retryable")

r = c.classify(Exception("429 Too Many Requests: rate limit exceeded"))
check(r.category == "transient" and r.retryable,
      "rate limit 429 -> transient, retryable")

r = c.classify(TransientError("socket broken pipe"))
check(r.category == "transient" and r.retryable,
      "marker TransientError -> transient")

r = c.classify(Exception("401 Unauthorized: invalid api key"))
check(r.category == "fatal" and not r.retryable and r.backoff_sec == 0,
      "401 invalid api key -> fatal, không retry")

r = c.classify(FatalError("missing config: PROVIDER_URL"))
check(r.category == "fatal" and not r.retryable,
      "marker FatalError / config error -> fatal")

r = c.classify(Exception("This model's maximum context length is 128000 tokens, "
                         "however you requested 200000 tokens"))
check(r.category == "context_overflow" and not r.retryable,
      "token limit exceeded -> context_overflow")

r = c.classify(ContextOverflowError("context too long"))
check(r.category == "context_overflow",
      "marker ContextOverflowError -> context_overflow")

r = c.classify(Exception("tool 'search' failed: no results"))
check(r.category == "tool_error" and r.retryable,
      "lỗi lạ mặc định -> tool_error, retryable 1 lần")

# ------------------------------------------------------------- RetryPolicy --
print("== RetryPolicy ==")
p = RetryPolicy(RetryConfig(base_delay=1.0, max_delay=60.0, jitter=0.0))

d0, d1, d2 = p.delay_for(0), p.delay_for(1), p.delay_for(2)
check(d0 == 1.0 and d1 == 2.0 and d2 == 4.0,
      f"backoff tăng dần theo cấp số nhân: {d0}, {d1}, {d2}")

check(p.delay_for(10) == 60.0,
      "delay bị chặn ở max_delay (không bùng nổ vô hạn)")

check(p.should_retry("transient", 0) and p.should_retry("transient", 2)
      and not p.should_retry("transient", 3),
      "transient: retry đúng max_attempts=3 lần rồi dừng")

check(not p.should_retry("fatal", 0) and not p.should_retry("fatal", 5),
      "fatal: không bao giờ retry")

check(p.should_retry("tool_error", 0) and not p.should_retry("tool_error", 1),
      "tool_error: retry đúng 1 lần rồi skip")

check(p.should_retry("context_overflow", 0)
      and not p.should_retry("context_overflow", 1),
      "context_overflow: retry 1 lần sau compress")

# -------------------------------------------------------- SelfHealingLoop ---
print("== SelfHealingLoop ==")

# transient: fail 2 lần rồi thành công
loop, sleeps = make_loop()
f = flaky(2, lambda: TimeoutError("timed out"))
check(loop.execute_with_recovery(f) == "done" and f.calls["n"] == 3,
      "transient: retry 2 lần rồi thành công (3 lần gọi)")
check(len(sleeps) == 2 and sleeps[1] > sleeps[0] > 0,
      f"backoff tăng dần giữa các lần retry: {sleeps}")
check(all(e.action == "retry" for e in loop.recovery_log)
      and len(loop.recovery_log) == 2,
      "mọi lần retry đều được log vào recovery_log")

# fatal: dừng ngay, không retry, không sleep
loop, sleeps = make_loop()
calls = {"n": 0}


def boom_auth():
    calls["n"] += 1
    raise Exception("403 Forbidden: invalid api key")


try:
    loop.execute_with_recovery(boom_auth)
    check(False, "fatal: phải raise SelfHealingFailed")
except SelfHealingFailed as e:
    check(calls["n"] == 1 and len(sleeps) == 0 and e.category == "fatal",
          "fatal: dừng ngay lần đầu, không retry, không backoff")

# context overflow: tự gọi compress() rồi retry thành công
loop, sleeps = make_loop()
cm = FakeContextManager()
calls = {"n": 0}


def boom_ctx():
    calls["n"] += 1
    if calls["n"] == 1:
        raise Exception("context_length_exceeded: max tokens")
    return "done"


check(loop.execute_with_recovery(boom_ctx, context_manager=cm) == "done",
      "context overflow: compress rồi retry thành công")
check(cm.compress_calls == 1,
      "context overflow: ContextManager.compress() được gọi đúng 1 lần")
check(any(e.action == "compress+retry" for e in loop.recovery_log),
      "context overflow: log ghi nhận hành động compress+retry")

# tool_error: retry 1 lần rồi skip (trả None, không raise)
loop, sleeps = make_loop()
f = flaky(99, lambda: Exception("tool exploded"))
check(loop.execute_with_recovery(f) is None and f.calls["n"] == 2,
      "tool_error: retry đúng 1 lần rồi skip (2 lần gọi, trả None)")
check(loop.recovery_log[-1].action == "skip",
      "tool_error: log ghi nhận skip sau khi hết retry")

# transient fail mãi: hết lượt thì raise SelfHealingFailed
loop, sleeps = make_loop(retry_config=RetryConfig(max_attempts=2, jitter=0.0))
f = flaky(99, lambda: ConnectionError("reset by peer"))
try:
    loop.execute_with_recovery(f)
    check(False, "transient hết lượt: phải raise")
except SelfHealingFailed as e:
    check(f.calls["n"] == 3 and e.attempts == 2 and isinstance(e.original, ConnectionError),
          "transient: raise SelfHealingFailed sau đúng max_attempts+1 lần gọi")

# on_recovery callback được gọi cho mỗi attempt
seen = []
loop, _ = make_loop(on_recovery=seen.append)
f = flaky(1, lambda: TimeoutError("x"))
loop.execute_with_recovery(f)
check(len(seen) == 1 and seen[0].category == "transient",
      "on_recovery callback nhận đủ mọi recovery attempt")

check(loop.recovery_summary()["total"] == 1
      and loop.recovery_summary()["by_action"] == {"retry": 1},
      "recovery_summary() thống kê đúng")

print(f"\nTẤT CẢ {PASSED} ASSERTIONS PASS ✔")
