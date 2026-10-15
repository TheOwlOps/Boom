"""Test cho gói performance: TTLCache, LazyLoader, timeit/ProfileReport.

Chạy:  python3 test_performance.py
"""

import sys
import time
from pathlib import Path

# Cho phép import gói perf khi chạy test trực tiếp từ thư mục perf/.
# File test nằm TRONG package perf -> cần thêm thư mục CHA của nó vào sys.path.
sys.path.insert(0, "/tmp/boom_git/agent/src")

from performance import TTLCache, LazyLoader, ProfileReport, lazy_import, timed, timeit  # noqa: E402

passed = 0


def check(cond: bool, label: str) -> None:
    """Một assertion có nhãn; fail thì raise ngay để thấy rõ."""
    global passed
    assert cond, f"FAIL: {label}"
    passed += 1
    print(f"  PASS: {label}")


def test_ttl_cache_basic():
    print("[TTLCache] set/get cơ bản")
    c: TTLCache[str, str] = TTLCache(default_ttl=60.0)
    c.set("skill_list", ["a", "b"])
    check(c.get("skill_list") == ["a", "b"], "get trả đúng giá trị đã set")
    check(c.get("khong_co", default="mac_dinh") == "mac_dinh", "get key thiếu trả default")


def test_ttl_cache_expire():
    print("[TTLCache] hết hạn theo TTL")
    c: TTLCache[str, int] = TTLCache(default_ttl=0.05)  # 50ms
    c.set("tam", 42)
    check(c.get("tam") == 42, "chưa hết TTL vẫn đọc được")
    time.sleep(0.08)  # chờ quá TTL
    check(c.get("tam") is None, "quá TTL -> get trả None (đã expire)")
    check("tam" not in c, "__contains__ cũng thấy key đã hết hạn")


def test_ttl_cache_delete_and_clear():
    print("[TTLCache] delete/clear")
    c: TTLCache[str, int] = TTLCache(default_ttl=60.0)
    c.set("x", 1)
    check(c.delete("x") is True, "delete key tồn tại trả True")
    check(c.get("x") is None, "sau delete không đọc được nữa")
    check(c.delete("x") is False, "delete key không tồn tại trả False")
    c.set("y", 2)
    c.clear()
    check(len(c) == 0, "clear xóa hết -> len == 0")


def test_ttl_cache_get_or_set():
    print("[TTLCache] get_or_set (cache-aside cho skill loading)")
    calls = {"n": 0}

    def expensive_scan():
        calls["n"] += 1
        return ["skill1", "skill2"]  # giả lập _scan_skills() đắt đỏ

    c: TTLCache[str, list] = TTLCache(default_ttl=60.0)
    r1 = c.get_or_set("list_skills", expensive_scan)
    r2 = c.get_or_set("list_skills", expensive_scan)
    check(r1 == r2 == ["skill1", "skill2"], "get_or_set trả đúng kết quả")
    check(calls["n"] == 1, "factory chỉ chạy 1 lần, lần 2 dùng cache")
    check(c.stats.hits == 1 and c.stats.misses == 1, "stats ghi đúng 1 hit + 1 miss")


def test_ttl_cache_maxsize_lru():
    print("[TTLCache] giới hạn maxsize đuổi LRU")
    c: TTLCache[str, int] = TTLCache(maxsize=2, default_ttl=60.0)
    c.set("a", 1)
    c.set("b", 2)
    c.get("a")  # chạm "a" -> "a" thành mới nhất, "b" thành cũ nhất
    c.set("c", 3)  # đầy -> đuổi "b"
    check(len(c) == 2, "vượt maxsize vẫn giữ đúng 2 mục")
    check("b" not in c and "a" in c and "c" in c, "mục ít dùng nhất (b) bị đuổi")


def test_lazy_loader():
    print("[LazyLoader] lazy import module nặng")
    proxy = lazy_import("json")  # json chuẩn, nhẹ, dùng để kiểm chứng cơ chế
    check(isinstance(proxy, LazyLoader), "lazy_import trả về LazyLoader")
    check(proxy.is_loaded is False, "chưa dùng -> module CHƯA được import (is_loaded=False)")
    # Lần dùng đầu tiên mới trigger import thật:
    data = proxy.loads('{"boom": true}')
    check(data == {"boom": True}, "sau lần dùng đầu, proxy hoạt động như module thật")
    check(proxy.is_loaded is True, "sau khi dùng -> is_loaded=True")
    check("dumps" in dir(proxy), "dir(proxy) liệt kê được thuộc tính module thật")


def test_lazy_loader_load_explicit():
    print("[LazyLoader] ép load sớm bằng .load()")
    proxy = lazy_import("math")
    check(proxy.is_loaded is False, "trước load() chưa import")
    real = proxy.load()
    check(proxy.is_loaded is True, "sau load() đã import")
    check(real is proxy.load(), "load() lần 2 trả cùng object (không import lại)")
    check(proxy.sqrt(16) == 4.0, "gọi hàm qua proxy cho kết quả đúng")


def test_timeit_decorator():
    print("[timeit] decorator đo thời gian")
    report = ProfileReport()

    @timeit("fake_skill_scan", report=report)
    def fake_skill_scan():
        time.sleep(0.02)
        return ["s1"]

    result = fake_skill_scan()
    check(result == ["s1"], "decorator giữ nguyên return value")
    check(fake_skill_scan.__name__ == "fake_skill_scan", "functools.wraps giữ nguyên tên hàm")
    counts = report.call_counts()
    check(counts.get("fake_skill_scan") == 1, "report ghi nhận đúng 1 lần gọi")
    totals = report.totals()
    check(totals.get("fake_skill_scan", 0) >= 0.02, "thời gian đo được >= 0.02s (đo đúng)")


def test_timeit_bare_and_timed_ctx():
    print("[timeit] dạng @timeit không ngoặc + context manager timed()")
    report = ProfileReport()

    @timeit(report=report)  # không ngoặc -> tên mặc định = tên hàm
    def search_catalog():
        time.sleep(0.01)

    search_catalog()
    search_catalog()
    with timed("tool_schema_build", report=report):
        time.sleep(0.01)
    counts = report.call_counts()
    check(counts.get("search_catalog") == 2, "@timeit trần đếm đúng 2 lần gọi")
    check(counts.get("tool_schema_build") == 1, "timed() ghi nhận 1 lần đo")
    summary = report.summary()
    check("search_catalog" in summary and "tool_schema_build" in summary,
          "summary() chứa tên các mục đã đo")
    report.reset()
    check(report.total_calls == 0, "reset() xóa sạch dữ liệu đo")


if __name__ == "__main__":
    test_ttl_cache_basic()
    test_ttl_cache_expire()
    test_ttl_cache_delete_and_clear()
    test_ttl_cache_get_or_set()
    test_ttl_cache_maxsize_lru()
    test_lazy_loader()
    test_lazy_loader_load_explicit()
    test_timeit_decorator()
    test_timeit_bare_and_timed_ctx()
    print(f"\nTẤT CẢ {passed} ASSERTIONS PASS ✔")
