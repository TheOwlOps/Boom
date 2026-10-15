"""Test thật cho agent/src/business/reports.py (Auto Report).

Chạy:  python3 test_reports.py
Sinh ~50 messages mẫu (nhiều sender, nhiều giờ, có stopwords Việt/Anh),
rồi verify summary đúng số liệu.
"""

import csv
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reports import (
    ReportGenerator,
    cron_daily_job,
    cron_weekly_job,
    format_daily_text,
    format_weekly_text,
)

# -- dữ liệu mẫu: 50 messages, 4 sender, rải qua nhiều giờ --------------------
_SENDERS = [
    ("u1", "An"),
    ("u2", "Bình"),
    ("u3", "Chi"),
    ("u4", "Dũng"),
]
# (giờ, sender_idx, text) — text có stopwords Việt/Anh + từ ngắn để test lọc
_SAMPLE = [
    (7, 0, "Chào mọi người, hôm nay có báo cáo doanh thu không?"),
    (7, 1, "Có đây, mình gửi file báo cáo doanh thu tháng này"),
    (8, 0, "Báo cáo này rất hay, cảm ơn bạn nhé"),
    (8, 2, "the report is good and very detailed"),
    (9, 1, "Doanh thu tăng mạnh, cần họp bàn kế hoạch"),
    (9, 3, "Tôi đồng ý với kế hoạch này"),
    (10, 2, "Mọi người nhớ gửi báo cáo trước 5 giờ chiều"),
    (10, 0, "OK mình sẽ gửi sớm"),
    (12, 3, "Ăn trưa xong họp nhé"),
    (12, 1, "Doanh thu quý này vượt chỉ tiêu rồi"),
    (14, 0, "Báo cáo tài chính đã xong, mọi người xem giúp"),
    (14, 2, "Tài chính ổn, không có vấn đề gì"),
    (15, 1, "Cần thêm dữ liệu doanh thu chi tiết hơn"),
    (15, 3, "Mình sẽ bổ sung dữ liệu ngay"),
    (16, 0, "Kế hoạch marketing tháng tới thế nào?"),
    (16, 2, "Marketing cần tăng ngân sách quảng cáo"),
    (18, 1, "Tối nay ai rảnh họp online không"),
    (20, 3, "Báo cáo cuối ngày: doanh thu đạt 120 triệu"),
    (20, 0, "Tuyệt vời, chúc mừng cả team"),
    (21, 2, "Ngày mai tiếp tục cố gắng nhé mọi người"),
    (7, 1, "Sáng nay họp lúc 9 giờ"),
    (8, 3, "Nhớ mang theo báo cáo doanh thu"),
    (9, 0, "Đã chuẩn bị xong hết rồi"),
    (10, 1, "Khách hàng phản hồi rất tốt về sản phẩm"),
    (11, 2, "Sản phẩm mới cần thêm thời gian thử nghiệm"),
    (11, 0, "Đồng ý, chất lượng là trên hết"),
    (13, 3, "Buổi chiều có lịch gặp đối tác"),
    (14, 1, "Đối tác hỏi về báo giá sản phẩm"),
    (15, 0, "Báo giá đã gửi qua email rồi"),
    (16, 2, "Doanh thu từ sản phẩm mới rất khả quan"),
    (17, 3, "Team cố gắng thêm chút nữa nhé"),
    (17, 0, "Mọi người vất vả rồi"),
    (19, 1, "Tối nay tổng hợp báo cáo ngày"),
    (19, 2, "Mình làm phần doanh thu, bạn làm phần chi phí"),
    (20, 3, "Chi phí tháng này giảm 10 phần trăm"),
    (21, 0, "Tốt quá, lợi nhuận sẽ tăng"),
    (21, 1, "Ngày mai báo cáo tiếp"),
    (8, 2, "Nhắc lại: họp 9 giờ sáng mai"),
    (9, 3, "Đã rõ, mình sẽ tham gia"),
    (10, 0, "Cần chuẩn bị slide thuyết trình"),
    (12, 1, "Slide về doanh thu mình làm xong rồi"),
    (13, 2, "Gửi mình xem trước nhé"),
    (15, 3, "OK để mình gửi file"),
    (18, 0, "Hết giờ làm, mai gặp lại"),
    (18, 2, "Bye mọi người, the end of the day"),
    (22, 1, "Nhớ tắt điện trước khi về"),
    (22, 3, "Đã tắt hết rồi yên tâm"),
    (23, 0, "Chúc ngủ ngon cả nhà"),
    (23, 2, "Ngủ ngon, mai là một ngày mới"),
    (6, 1, "Sáng sớm đã có tin vui về doanh thu"),
]


def _ts(day: date, hour: int, minute: int = 0) -> float:
    return datetime(day.year, day.month, day.day, hour, minute).timestamp()


class TestAutoReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "reports.db")
        self.gen = ReportGenerator(self.db)
        self.today = date.today()
        # 50 messages hôm nay
        for hour, sidx, text in _SAMPLE:
            sid, sname = _SENDERS[sidx]
            self.gen.log_message("telegram", "group_sales", sid, sname, text,
                                 _ts(self.today, hour))
        # thêm vài messages hôm qua + 1 group khác để test weekly
        y = self.today - timedelta(days=1)
        self.gen.log_message("telegram", "group_sales", "u1", "An",
                             "Báo cáo hôm qua vẫn tốt", _ts(y, 9))
        self.gen.log_message("zalo", "group_ops", "u9", "Hùng",
                             "Vận hành ổn định", _ts(self.today, 10))

    def tearDown(self):
        self.tmp.cleanup()

    # -- log + đếm -----------------------------------------------------------
    def test_log_message_returns_id(self):
        mid = self.gen.log_message("telegram", "group_sales", "u1", "An", "hi")
        self.assertIsInstance(mid, int)
        self.assertGreater(mid, 0)

    def test_log_message_validation(self):
        with self.assertRaises(ValueError):
            self.gen.log_message("", "group_sales", "u1", "An", "hi")
        with self.assertRaises(ValueError):
            self.gen.log_message("telegram", "", "u1", "An", "hi")

    def test_count(self):
        # 50 mẫu + 1 hôm qua + 1 zalo = 52
        self.assertEqual(self.gen.count(), 52)

    # -- daily_summary -------------------------------------------------------
    def test_daily_summary_totals(self):
        s = self.gen.daily_summary("telegram", "group_sales", self.today)
        self.assertEqual(s["date"], self.today.isoformat())
        self.assertEqual(s["total_messages"], 50)  # đúng 50 tin hôm nay
        self.assertEqual(s["unique_senders"], 4)  # 4 sender
        self.assertEqual(len(s["hour_histogram"]), 24)
        self.assertEqual(sum(s["hour_histogram"]), 50)

    def test_daily_summary_top_senders(self):
        s = self.gen.daily_summary("telegram", "group_sales", self.today)
        top = s["top_senders"]
        self.assertTrue(top)
        # kỳ vọng tính độc lập từ dữ liệu mẫu
        from collections import Counter
        exp = Counter()
        for _h, sidx, _t in _SAMPLE:
            exp[_SENDERS[sidx][1]] += 1
        self.assertEqual([(t["sender_name"], t["count"]) for t in top],
                         list(exp.most_common(5)))
        self.assertEqual(sum(t["count"] for t in top), 50)

    def test_daily_summary_keywords(self):
        s = self.gen.daily_summary("telegram", "group_sales", self.today)
        words = [k["word"] for k in s["top_keywords"]]
        self.assertTrue(words)
        # 'doanh'/'thu'/'báo'/'cáo' là từ nổi bật thật của dữ liệu mẫu
        self.assertIn("doanh", words)
        self.assertIn("báo", words)
        # stopwords Việt/Anh bị loại
        for bad in ("của", "và", "có", "the", "and", "is", "rất", "này"):
            self.assertNotIn(bad, words)
        # từ < 3 ký tự bị loại
        for w in words:
            self.assertGreaterEqual(len(w), 3)

    def test_daily_summary_hour_histogram(self):
        s = self.gen.daily_summary("telegram", "group_sales", self.today)
        hist = s["hour_histogram"]
        # giờ 7 có đúng 3 tin trong dữ liệu mẫu
        self.assertEqual(hist[7], 3)
        # giờ 3 không có tin nào
        self.assertEqual(hist[3], 0)

    def test_daily_summary_date_str(self):
        s = self.gen.daily_summary("telegram", "group_sales",
                                   self.today.strftime("%Y-%m-%d"))
        self.assertEqual(s["total_messages"], 50)

    def test_daily_summary_yesterday(self):
        y = self.today - timedelta(days=1)
        s = self.gen.daily_summary("telegram", "group_sales", y)
        self.assertEqual(s["total_messages"], 1)
        self.assertEqual(s["unique_senders"], 1)

    def test_daily_summary_empty_day(self):
        old = self.today - timedelta(days=30)
        s = self.gen.daily_summary("telegram", "group_sales", old)
        self.assertEqual(s["total_messages"], 0)
        self.assertEqual(s["unique_senders"], 0)
        self.assertEqual(s["top_senders"], [])
        self.assertEqual(s["hour_histogram"], [0] * 24)

    # -- weekly_digest -------------------------------------------------------
    def test_weekly_digest_all_channels(self):
        d = self.gen.weekly_digest()
        self.assertEqual(d["total_messages"], 52)  # 50 + 1 + 1
        self.assertEqual(d["active_groups"], 2)  # group_sales + group_ops
        self.assertEqual(d["total_senders"], 5)  # u1..u4 + u9
        self.assertEqual(d["week_start"], (self.today - timedelta(days=6)).isoformat())
        self.assertEqual(d["week_end"], self.today.isoformat())
        # group sôi nổi nhất là telegram/group_sales
        top = d["top_groups"][0]
        self.assertEqual(top["channel"], "telegram")
        self.assertEqual(top["group_id"], "group_sales")
        self.assertEqual(top["count"], 51)

    def test_weekly_digest_channel_filter(self):
        d = self.gen.weekly_digest(channel="zalo")
        self.assertEqual(d["total_messages"], 1)
        self.assertEqual(d["active_groups"], 1)
        self.assertEqual(d["total_senders"], 1)

    # -- export_csv ----------------------------------------------------------
    def test_export_csv(self):
        path = os.path.join(self.tmp.name, "out.csv")
        start = _ts(self.today, 0)
        end = _ts(self.today + timedelta(days=1), 0)
        n = self.gen.export_csv("telegram", "group_sales", start, end, path)
        self.assertEqual(n, 50)
        with open(path, encoding="utf-8") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["timestamp", "sender_name", "text"])
        self.assertEqual(len(rows), 51)  # header + 50
        # tin sớm nhất là 6h của Bình
        self.assertEqual(rows[1][1], "Bình")
        self.assertIn("doanh thu", rows[1][2])

    def test_export_csv_empty(self):
        path = os.path.join(self.tmp.name, "empty.csv")
        old = self.today - timedelta(days=60)
        n = self.gen.export_csv("telegram", "group_sales",
                                _ts(old, 0), _ts(old + timedelta(days=1), 0), path)
        self.assertEqual(n, 0)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(list(csv.reader(f))[0],
                             ["timestamp", "sender_name", "text"])

    # -- format text ---------------------------------------------------------
    def test_format_daily_text(self):
        s = self.gen.daily_summary("telegram", "group_sales", self.today)
        text = format_daily_text(s)
        self.assertIn("📊", text)
        self.assertIn("BÁO CÁO NGÀY", text)
        self.assertIn("Tổng tin nhắn: 50", text)
        self.assertIn("Bình", text)  # top sender
        self.assertIn("━━━━", text)  # dòng kẻ
        # format ngày dd/mm/yyyy
        self.assertIn(self.today.strftime("%d/%m/%Y"), text)

    def test_format_weekly_text(self):
        d = self.gen.weekly_digest()
        text = format_weekly_text(d)
        self.assertIn("🗓️", text)
        self.assertIn("TỔNG KẾT TUẦN", text)
        self.assertIn("Groups hoạt động: 2", text)
        self.assertIn("group_sales", text)
        self.assertIn("━━━━", text)

    # -- prune_old ------------------------------------------------------------
    def test_prune_old(self):
        old_ts = (datetime.now() - timedelta(days=100)).timestamp()
        self.gen.log_message("telegram", "group_sales", "u1", "An",
                             "tin rất cũ", old_ts)
        self.assertEqual(self.gen.count(), 53)
        deleted = self.gen.prune_old(days=90)
        self.assertEqual(deleted, 1)
        self.assertEqual(self.gen.count(), 52)

    # -- cron jobs ------------------------------------------------------------
    def _mock_send(self, box):
        # send_fn mẫu: lưu text và trả True như daemon.send_to thành công
        return lambda text: box.append(text) or True

    def test_cron_daily_job_sends_yesterday(self):
        sent = []
        result = cron_daily_job("telegram", "group_sales", self._mock_send(sent), self.db)
        self.assertTrue(result["ok"])
        self.assertTrue(result["sent"])
        self.assertEqual(len(sent), 1)
        self.assertIn("BÁO CÁO NGÀY", sent[0])
        self.assertIn("Tổng tin nhắn: 1", sent[0])  # hôm qua đúng 1 tin
        self.assertEqual(result["summary"]["date"],
                         (self.today - timedelta(days=1)).isoformat())

    def test_cron_weekly_job(self):
        sent = []
        result = cron_weekly_job(self._mock_send(sent), self.db)
        self.assertTrue(result["ok"])
        self.assertTrue(result["sent"])
        self.assertEqual(len(sent), 1)
        self.assertIn("TỔNG KẾT TUẦN", sent[0])
        self.assertIn("Tổng tin nhắn: 52", sent[0])

    def test_cron_job_send_failure_does_not_raise(self):
        def boom(_text):
            raise RuntimeError("telegram down")
        result = cron_daily_job("telegram", "group_sales", boom, self.db)
        self.assertFalse(result["ok"])
        self.assertIn("RuntimeError", result["error"])  # lỗi được ghi, không raise

    def test_cron_daily_job_empty_yesterday(self):
        # group không có tin hôm qua -> vẫn gửi text, total = 0
        sent = []
        result = cron_daily_job("zalo", "group_ops", self._mock_send(sent), self.db)
        self.assertTrue(result["ok"])
        self.assertIn("Tổng tin nhắn: 0", sent[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
