"""Tests cho agent/src/lifecycle/ratelimit.py (harden batch).

Chạy:  python3 test_harden_ratelimit.py
"""

import os
import sys
from pathlib import Path
import threading
import time

# Import root is agent/src (repo convention); the modules under test live
# in subpackages there (they previously lived next to this file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from lifecycle.ratelimit import RateLimiter, rate_limit_middleware, RateLimitMixin


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    return cond


def main():
    passed = 0
    total = 0

    def t(name, cond):
        nonlocal passed, total
        total += 1
        if check(name, cond):
            passed += 1

    # --- 1. 100 request liên tiếp, limit 60/phút, burst 60 -> 60 pass / 40 từ chối
    lim = RateLimiter(rate=60, burst=60)
    results = [lim.allow("ip-1") for _ in range(100)]
    t("100 req liên tiếp -> đúng 60 pass", sum(results) == 60)
    t("100 req liên tiếp -> đúng 40 từ chối", results.count(False) == 40)
    # sau khi hết quota, retry_after > 0
    ra = lim.get_retry_after("ip-1")
    t("hết quota -> get_retry_after > 0 (got %.2f)" % ra, ra > 0)

    # --- 2. per-key độc lập: A hết quota, B vẫn pass
    lim2 = RateLimiter(rate=5, burst=5)
    for _ in range(5):
        lim2.allow("A")
    t("key A hết quota -> bị từ chối", lim2.allow("A") is False)
    t("key B chưa dùng -> vẫn pass", lim2.allow("B") is True)

    # --- 3. refill: rate cao + sleep ngắn -> bucket nạp lại
    lim3 = RateLimiter(rate=600, burst=2)  # 10 token/giây
    lim3.allow("r")
    lim3.allow("r")
    t("dùng hết 2 token -> từ chối", lim3.allow("r") is False)
    time.sleep(0.25)  # ~2.5 token nạp lại
    t("sau 0.25s (rate 600/phút) -> pass lại", lim3.allow("r") is True)
    t("retry_after = 0 khi đang cho phép", lim3.get_retry_after("new-key") == 0.0)

    # --- 4. thread-safety: 4 thread cùng gọi, tổng pass không vượt limit+burst
    lim4 = RateLimiter(rate=20, burst=20)
    ok_count = [0]
    lock = threading.Lock()

    def worker():
        local = 0
        for _ in range(50):
            if lim4.allow("shared"):
                local += 1
        with lock:
            ok_count[0] += local

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    t("4 thread x 50 req -> tổng pass <= 20 (got %d)" % ok_count[0],
      ok_count[0] <= 20)
    t("4 thread -> tổng pass đúng bằng 20 (không mất token)", ok_count[0] == 20)

    # --- 5. middleware trả 429 + Retry-After khi từ chối
    class FakeHandler:
        def __init__(self):
            self.status = None
            self.headers = {}
            self.body = b""
            self.wfile = self

        def write(self, data):
            self.body += data

        def send_response(self, code):
            self.status = code

        def send_header(self, k, v):
            self.headers[k] = v

        def end_headers(self):
            pass

        @property
        def client_address(self):
            return ("127.0.0.1", 1234)

    lim5 = RateLimiter(rate=2, burst=2)
    h = FakeHandler()
    t("middleware: req 1 pass", rate_limit_middleware(h, lim5, lambda x: "k") is True)
    rate_limit_middleware(h, lim5, lambda x: "k")
    h2 = FakeHandler()
    t("middleware: req 3 -> từ chối", rate_limit_middleware(h2, lim5, lambda x: "k") is False)
    t("middleware: status 429", h2.status == 429)
    t("middleware: có header Retry-After", "Retry-After" in h2.headers)
    t("middleware: body JSON có error", b"rate limit exceeded" in h2.body)

    # --- 6. mixin fail-open khi chưa cấu hình limiter
    class Bare(RateLimitMixin):
        pass

    t("mixin: chưa gán limiter -> cho qua (fail-open)",
      Bare()._check_rate_limit() is True)

    # --- 7. reset + constructor guard
    lim7 = RateLimiter(rate=3, burst=3)
    for _ in range(3):
        lim7.allow("z")
    lim7.reset("z")
    t("reset(key) -> pass lại được", lim7.allow("z") is True)
    try:
        RateLimiter(rate=0)
        t("rate=0 -> raise ValueError", False)
    except ValueError:
        t("rate=0 -> raise ValueError", True)

    # --- 8. lazy cleanup không xóa key đang bị throttle
    lim8 = RateLimiter(rate=60, burst=1, cleanup_interval=0.05)
    lim8.allow("victim")  # hết token
    time.sleep(0.1)
    lim8.allow("other")  # trigger cleanup
    t("cleanup không reset key đang bị throttle", lim8.allow("victim") is False)

    print("\n%d/%d tests passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
