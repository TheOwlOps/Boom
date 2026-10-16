"""Test cho agent/src/security/ratelimit.py — chạy: python3 test_ratelimit.py"""
import sys
import os
import threading
import time
from unittest import mock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_sys_src = os.path.join(_REPO, "agent", "src")
if _sys_src not in sys.path:
    sys.path.insert(0, _sys_src)

import security.ratelimit as ratelimit
from security.ratelimit import UserRateLimiter, RateLimitError, DEFAULT, STRICT, make_key

PASS = 0


def check(cond, name):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1
    print(f"  ok: {name}")


print("== 1. constructor validation ==")
for kwargs, label in [({"capacity": 0}, "capacity=0"),
                      ({"capacity": -1}, "capacity<0"),
                      ({"refill": 0}, "refill=0"),
                      ({"idle_ttl": -5}, "idle_ttl<0")]:
    try:
        UserRateLimiter(**kwargs)
        check(False, f"ValueError khi {label}")
    except ValueError:
        check(True, f"ValueError khi {label}")

print("== 2. preset ==")
check(DEFAULT == {"capacity": 30, "refill": 0.5}, "DEFAULT preset đúng")
check(STRICT == {"capacity": 5, "refill": 0.1}, "STRICT preset đúng")
lim = UserRateLimiter(**UserRateLimiter.PRESET_DEFAULT)
check(lim.capacity == 30 and lim.refill == 0.5, "preset DEFAULT gắn vào instance")
lim_s = UserRateLimiter(**UserRateLimiter.PRESET_STRICT)
check(lim_s.capacity == 5 and lim_s.refill == 0.1, "preset STRICT gắn vào instance")

print("== 3. burst hết -> bị chặn ==")
lim = UserRateLimiter(capacity=3, refill=1000.0)  # refill nhanh để test khác
check(lim.allow("u1"), "allow lần 1")
check(lim.allow("u1"), "allow lần 2")
check(lim.allow("u1"), "allow lần 3 (hết burst)")
check(not lim.allow("u1"), "allow lần 4 bị chặn")
check(not lim.allow("u1"), "allow lần 5 vẫn bị chặn")

print("== 4. refill theo thời gian (mock clock) ==")
lim = UserRateLimiter(capacity=2, refill=1.0)  # 1 token/giây
t0 = time.monotonic()
with mock.patch("time.monotonic", return_value=t0):
    check(lim.allow("u2"), "mock: allow 1")
    check(lim.allow("u2"), "mock: allow 2")
    check(not lim.allow("u2"), "mock: hết token -> chặn")
with mock.patch("time.monotonic", return_value=t0 + 1.5):  # nạp 1.5 token
    st = lim.get_state("u2")
    check(abs(st["tokens"] - 1.5) < 1e-9, "mock: get_state tokens=1.5 sau 1.5s")
    check(st["retry_after"] == 0.0, "mock: retry_after=0 khi còn token")
    check(lim.allow("u2"), "mock: sau refill lại được phép")
with mock.patch("time.monotonic", return_value=t0 + 1.6):  # còn 0.6 token
    st = lim.get_state("u2")
    check(st["retry_after"] > 0, "mock: retry_after>0 khi hết token")
    check(abs(st["retry_after"] - 0.4) < 1e-9, "mock: retry_after=0.4s (thiếu 0.4 token)")

print("== 5. allow_or_raise ==")
lim = UserRateLimiter(capacity=1, refill=0.001)
lim.allow("u3")  # tiêu hết
try:
    lim.allow_or_raise("u3")
    check(False, "allow_or_raise phải ném RateLimitError")
except RateLimitError as e:
    check(True, "allow_or_raise ném RateLimitError")
    check("try again in" in str(e), "message is English")
    check(e.retry_after > 0, "retry_after > 0 trong exception")
    check(e.key == "u3", "exception giữ key")
# allow_or_raise khi còn token thì không ném
lim2 = UserRateLimiter(capacity=2, refill=1.0)
lim2.allow_or_raise("ok")
check(True, "allow_or_raise không ném khi còn token")

print("== 6. get_state / reset ==")
lim = UserRateLimiter(capacity=4, refill=10.0)
st = lim.get_state("new_user")
check(st == {"tokens": 4, "capacity": 4, "retry_after": 0.0},
      "get_state user mới: full token")
lim.allow("u4"); lim.allow("u4")
st = lim.get_state("u4")
check(st["capacity"] == 4, "get_state capacity đúng")
check(st["tokens"] <= 2.01, "get_state tokens giảm sau 2 lần allow")
lim.reset("u4")
check(lim.get_state("u4")["tokens"] == 4, "reset(key) trả full token")
lim.allow("a"); lim.allow("b")
lim.reset()
check(lim.key_count() == 0, "reset() xóa toàn bộ")

print("== 7. 2 user độc lập ==")
lim = UserRateLimiter(capacity=1, refill=0.0001)
check(lim.allow("alice"), "alice allow lần 1")
check(not lim.allow("alice"), "alice bị chặn lần 2")
check(lim.allow("bob"), "bob độc lập, vẫn được phép")
check(make_key("telegram", 123) == "telegram:123", "make_key format chuẩn")

print("== 8. thread-safe: 10 threads x 20 calls, capacity=30 ==")
lim = UserRateLimiter(capacity=30, refill=0.0001)  # refill ~0 trong test
allowed = []
t_lock = threading.Lock()


def hammer():
    n = sum(1 for _ in range(20) if lim.allow("shared"))
    with t_lock:
        allowed.append(n)


threads = [threading.Thread(target=hammer) for _ in range(10)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check(sum(allowed) == 30, f"tổng allow đúng = capacity (được {sum(allowed)})")
check(not lim.allow("shared"), "sau race vẫn bị chặn đúng")

print("== 9. cleanup key cũ (idle_ttl) ==")
lim = UserRateLimiter(capacity=2, refill=1.0, idle_ttl=10.0)
t0 = time.monotonic()
with mock.patch("time.monotonic", return_value=t0):
    lim.allow("ghost")          # tạo key lúc t0
    check(lim.key_count() == 1, "key ghost tồn tại")
with mock.patch("time.monotonic", return_value=t0 + 11.0):  # quá idle_ttl
    lim.allow("fresh")          # trigger lazy cleanup
    check(lim.key_count() == 1, "key ghost >1h-idle bị xóa, chỉ còn fresh")
    check(lim.get_state("ghost")["tokens"] == 2, "ghost bị xóa -> state như user mới")

print("== 10. sleep thật ngắn: refill hoạt động ngoài mock ==")
lim = UserRateLimiter(capacity=1, refill=50.0)  # 1 token / 20ms
check(lim.allow("rt"), "realtime: allow 1")
check(not lim.allow("rt"), "realtime: chặn ngay sau burst")
time.sleep(0.05)  # 50ms -> nạp ~2.5 token, cap ở 1
check(lim.allow("rt"), "realtime: sau sleep ngắn lại được phép")

print(f"\nTỔNG: {PASS} assertions PASS")
