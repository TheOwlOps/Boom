"""Test thật cho LongTermMemory: chạy trên SQLite file tạm, không mock DB.

Chạy:  python3 test_longterm.py   (từ thư mục chứa file này)
"""

import os
import tempfile
import threading
import time
import unittest

from longterm import LongTermMemory

DAY = 86400


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # để sqlite tạo file mới sạch
    return LongTermMemory(path), path


class TestLongTermMemory(unittest.TestCase):
    def setUp(self):
        self.mem, self.path = fresh_db()

    def tearDown(self):
        self.mem.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    # ------------------------------------------------------------ FTS5

    def test_fts5_available(self):
        # Máy này phải hỗ trợ FTS5 (đã verify); test này chốt lại trong report
        self.assertTrue(self.mem._fts5, "sqlite build này không có FTS5")

    # ------------------------------------------------------------ remember

    def test_remember_and_recall_basic(self):
        self.mem.remember("fact1", "Boom là agent AI đa kênh", 0.9, ["intro"])
        hits = self.mem.recall("Boom agent")
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual(h["key"], "fact1")
        self.assertEqual(h["value"], "Boom là agent AI đa kênh")
        self.assertAlmostEqual(h["importance"], 0.9)
        self.assertEqual(h["tags"], ["intro"])
        self.assertIn("score", h)

    def test_remember_overwrite_keeps_created_at(self):
        self.mem.remember("k", "v1", 0.5, ["a"], created_at=1000.0)
        self.mem.remember("k", "v2", 0.8, ["b"])
        hits = self.mem.recall("v2")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["created_at"], 1000.0)  # giữ created_at gốc
        self.assertEqual(hits[0]["tags"], ["b"])

    def test_importance_clamped(self):
        self.mem.remember("low", "x low", -5.0)
        self.mem.remember("high", "x high", 99.0)
        by_key = {h["key"]: h for h in self.mem.recall("x", limit=10)}
        self.assertEqual(by_key["low"]["importance"], 0.0)
        self.assertEqual(by_key["high"]["importance"], 1.0)

    # ------------------------------------------------------------ recall order

    def test_recall_orders_by_importance_when_equally_relevant(self):
        # 9 memories cùng độ dài, cùng chứa "deploy" đúng 1 lần
        # -> relevance FTS5 như nhau -> thứ tự phải theo importance giảm dần
        specs = [("k1", 1.0), ("k2", 0.9), ("k3", 0.8), ("k4", 0.7),
                 ("k5", 0.5), ("k6", 0.4), ("k7", 0.3), ("k8", 0.2),
                 ("k9", 0.1)]
        for key, imp in specs:
            suffix = key[1]  # '1'..'9' — giữ mọi text dài bằng nhau
            self.mem.remember(key, f"deploy svc-{suffix}", imp, ["ops"])
        # Memory importance cao NHẤT nhưng không liên quan -> phải bị loại
        self.mem.remember("k10", "lunch pizza", 1.0, ["food"])

        hits = self.mem.recall("deploy", limit=10)
        keys = [h["key"] for h in hits]
        self.assertEqual(keys, [f"k{i}" for i in range(1, 10)],
                         f"thứ tự sai: {keys}")
        self.assertNotIn("k10", keys, "memory không liên quan bị lọt vào")
        scores = [h["score"] for h in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_recall_limit_and_empty_query(self):
        for i in range(5):
            self.mem.remember(f"m{i}", f"deploy item {i}", 0.5)
        self.assertEqual(len(self.mem.recall("deploy", limit=3)), 3)
        self.assertEqual(self.mem.recall(""), [])
        self.assertEqual(self.mem.recall("   "), [])
        self.assertEqual(self.mem.recall("không-tồn-tại-xyz"), [])

    def test_recall_like_fallback_same_order(self):
        # Ép tắt FTS5 để test nhánh LIKE fallback
        for i, imp in enumerate([0.2, 0.9, 0.5]):
            self.mem.remember(f"f{i}", f"backup db nightly {i}", imp)
        self.mem._fts5 = False
        hits = self.mem.recall("backup db", limit=10)
        keys = [h["key"] for h in hits]
        self.assertEqual(keys, ["f1", "f2", "f0"],
                         f"fallback LIKE sai thứ tự: {keys}")

    def test_recall_special_chars_dont_crash(self):
        self.mem.remember("s1", "deploy production", 1.0)
        # Ký tự đặc biệt FTS5 phải thành literal, không raise
        hits = self.mem.recall('deploy "production" -*', limit=5)
        self.assertTrue(any(h["key"] == "s1" for h in hits))

    # ------------------------------------------------------------ summarize

    def test_summarize_template_and_idempotent(self):
        old = time.time() - 40 * DAY
        self.mem.remember("old1", "server prod IP 10.0.0.1", 0.8, ["ops"],
                          created_at=old)
        self.mem.remember("old2", "restart cron lúc 3h sáng", 0.6, ["ops"],
                          created_at=old)
        self.mem.remember("new1", "vừa deploy hôm nay", 0.9, ["ops"])
        # Memory không tag -> nhóm "untagged"
        self.mem.remember("old3", "ghi chú cũ không tag", 0.5,
                          created_at=old)

        out = self.mem.summarize()
        self.assertIn("## ops", out)
        self.assertIn("old1", out)
        self.assertIn("old2", out)
        self.assertIn("## untagged", out)
        self.assertIn("old3", out)
        self.assertNotIn("new1", out, "memory mới (<30 ngày) bị tóm tắt nhầm")

        # Lần 2: không còn gì để tóm tắt -> chuỗi rỗng (idempotent)
        self.assertEqual(self.mem.summarize(), "")
        st = self.mem.get_stats()
        self.assertEqual(st["summarized"], 3)

    def test_summarize_with_llm_fn(self):
        old = time.time() - 40 * DAY
        self.mem.remember("o1", "nội dung cần tóm tắt", 0.7, ["memo"],
                          created_at=old)
        calls = []

        def fake_llm(prompt):
            calls.append(prompt)
            return "TÓM TẮT GIẢ"

        out = self.mem.summarize(llm_fn=fake_llm)
        self.assertEqual(len(calls), 1)
        self.assertIn("nội dung cần tóm tắt", calls[0])
        self.assertIn("memo", calls[0])
        self.assertIn("TÓM TẮT GIẢ", out)
        # Memory đã tóm tắt bằng LLM cũng bị đánh dấu, không tóm tắt lại
        self.assertEqual(self.mem.summarize(llm_fn=fake_llm), "")

    def test_summarize_nothing_old(self):
        self.mem.remember("fresh", "mới tinh", 1.0)
        self.assertEqual(self.mem.summarize(), "")

    # ------------------------------------------------------------ forget_old

    def test_forget_old_deletes_only_old_and_unimportant(self):
        old = time.time() - 100 * DAY
        self.mem.remember("old_low", "rác cũ", 0.1, created_at=old)
        self.mem.remember("old_high", "kiến thức cũ quan trọng", 0.9,
                          created_at=old)
        self.mem.remember("recent_low", "rác mới", 0.1)  # mới -> giữ
        # biên: importance đúng bằng ngưỡng thì KHÔNG xóa (< chứ không <=)
        self.mem.remember("old_edge", "biên ngưỡng", 0.3, created_at=old)

        deleted = self.mem.forget_old(days=90, min_importance=0.3)
        self.assertEqual(deleted, 1)
        remaining = {h["key"] for h in self.mem.recall("rác", limit=10)}
        # "rác cũ" chứa từ rác và đã bị xóa -> không còn
        self.assertNotIn("old_low", remaining)
        self.assertIn("recent_low", remaining)
        # kiểm tra trực tiếp 2 key còn lại qua stats
        st = self.mem.get_stats()
        self.assertEqual(st["total"], 3)

    def test_forget_old_nothing_to_delete(self):
        self.mem.remember("a", "mới", 0.1)
        self.assertEqual(self.mem.forget_old(days=90, min_importance=0.3), 0)

    # ------------------------------------------------------------ stats

    def test_get_stats(self):
        t0 = time.time() - 10 * DAY
        self.mem.remember("a", "va", 0.9, ["ops", "db"], created_at=t0)
        self.mem.remember("b", "vb", 0.2, ["ops"])
        self.mem.remember("c", "vc", 0.5)  # không tag
        st = self.mem.get_stats()
        self.assertEqual(st["total"], 3)
        self.assertEqual(st["by_tag"], {"ops": 2, "db": 1, "untagged": 1})
        self.assertAlmostEqual(st["oldest"], t0, delta=1)
        self.assertGreaterEqual(st["newest"], st["oldest"])
        self.assertEqual(st["summarized"], 0)
        self.assertTrue(st["fts5"])

    def test_get_stats_empty(self):
        st = self.mem.get_stats()
        self.assertEqual(st["total"], 0)
        self.assertEqual(st["by_tag"], {})
        self.assertIsNone(st["oldest"])
        self.assertIsNone(st["newest"])

    # ------------------------------------------------------------ thread-safe

    def test_thread_safety(self):
        errors = []

        def worker(n):
            try:
                for i in range(25):
                    self.mem.remember(f"t{n}-{i}", f"deploy data {n} {i}",
                                      (i % 10) / 10.0, ["stress"])
                    self.mem.recall("deploy", limit=5)
                self.mem.get_stats()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], f"lỗi thread: {errors}")
        self.assertEqual(self.mem.get_stats()["total"], 100)

    # ------------------------------------------------------------ FTS sync sau delete/update

    def test_fts_stays_in_sync_after_forget(self):
        old = time.time() - 100 * DAY
        self.mem.remember("gone", "deploy secret cũ", 0.1, created_at=old)
        self.mem.forget_old(days=90, min_importance=0.3)
        self.assertEqual(self.mem.recall("deploy secret"), [],
                         "FTS index còn sót sau DELETE")

    def test_fts_stays_in_sync_after_update(self):
        self.mem.remember("u", "alpha beta", 0.5)
        self.mem.remember("u", "gamma delta", 0.5)  # update cùng key
        self.assertEqual(self.mem.recall("alpha"), [])
        self.assertEqual(len(self.mem.recall("gamma")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
