"""Test cho realtime-feed. KHÔNG kết nối internet thật.

Mock WS server chạy trên socket.socketpair(): phía server tự nói frame RFC6455
(không mask, đúng chuẩn server), phía client dùng tools/ws_client.py THẬT của repo.
"""
import json
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

# Module test nằm cùng thư mục với 3 module feed.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# tools/ws_client.py của repo Boom (read-only) để test backend thật.
sys.path.insert(0, "/tmp/boom_git/agent/src")

from tools.ws_client import (  # noqa: E402
    OP_CLOSE, OP_TEXT, WebSocket, WSError, decode_frame, encode_frame, FrameReader,
)
from websocket_feed import (  # noqa: E402
    Feed, WebSocketFeed, WebSocketFeedError, _require_backend,
)
from feeds import (  # noqa: E402
    BinancePriceFeed, RSSNewsFeed, fetch_rss, parse_rss,
)
from feed_manager import FeedManager  # noqa: E402


def make_pair():
    """Tạo socketpair: trả (client_sock, server_sock)."""
    return socket.socketpair()


def server_send_text(server_sock, text: str):
    server_sock.sendall(encode_frame(text, OP_TEXT, mask=False))


def server_close(server_sock):
    try:
        server_sock.sendall(encode_frame(b"", OP_CLOSE, mask=False))
    except OSError:
        pass


def server_read_frame(server_sock, timeout=5.0):
    server_sock.settimeout(timeout)
    return decode_frame(FrameReader(server_sock, timeout).read)


class WebSocketFeedTest(unittest.TestCase):
    def test_connect_and_receive(self):
        """connect() + nhận message text qua ws_client thật của repo."""
        client_sock, server_sock = make_pair()
        got = threading.Event()
        received = []
        feed = WebSocketFeed(
            "ws://mock/test",
            on_message=lambda m: (received.append(m), got.set()),
            _connect_factory=lambda url: WebSocket(client_sock),
        )
        feed.start()
        try:
            server_send_text(server_sock, '{"hello": 1}')
            self.assertTrue(got.wait(timeout=5), "không nhận được message")
            self.assertEqual(received[0], '{"hello": 1}')
        finally:
            feed.close()
            server_sock.close()

    def test_auto_reconnect(self):
        """Server đóng kết nối -> feed tự reconnect (backoff), nhận tiếp message."""
        pairs = []
        lock = threading.Lock()

        def factory(url):
            c, s = make_pair()
            with lock:
                pairs.append((c, s))
            return WebSocket(c)

        received = []
        feed = WebSocketFeed(
            "ws://mock/test",
            on_message=received.append,
            backoff_base=0.05, backoff_max=0.2,
            _connect_factory=factory,
        )
        feed.start()
        try:
            # Đợi kết nối đầu xong
            deadline = time.time() + 5
            while time.time() < deadline and not pairs:
                time.sleep(0.02)
            self.assertTrue(pairs, "feed không connect lần 1")
            server_send_text(pairs[0][1], "msg-1")
            deadline = time.time() + 5
            while time.time() < deadline and len(received) < 1:
                time.sleep(0.02)
            self.assertEqual(received, ["msg-1"])
            # Đóng kết nối 1 từ phía server -> feed phải reconnect
            server_close(pairs[0][1])
            pairs[0][1].close()
            deadline = time.time() + 10
            while time.time() < deadline and len(pairs) < 2:
                time.sleep(0.02)
            self.assertGreaterEqual(len(pairs), 2, "feed không reconnect")
            self.assertGreaterEqual(feed.reconnect_count, 1)
            server_send_text(pairs[1][1], "msg-2")
            deadline = time.time() + 5
            while time.time() < deadline and len(received) < 2:
                time.sleep(0.02)
            self.assertEqual(received, ["msg-1", "msg-2"])
        finally:
            feed.close()
            for _, s in pairs:
                try:
                    s.close()
                except OSError:
                    pass

    def test_max_retries_stops(self):
        """max_retries=2 -> sau 3 lần lỗi thì dừng, on_error được gọi."""
        errors = []

        def bad_factory(url):
            raise WSError("mock connect fail")

        feed = WebSocketFeed(
            "ws://mock/test",
            on_error=errors.append,
            max_retries=2, backoff_base=0.02, backoff_max=0.05,
            _connect_factory=bad_factory,
        )
        feed.start()
        deadline = time.time() + 10
        while time.time() < deadline and feed.is_running:
            time.sleep(0.05)
        feed.close()
        self.assertFalse(feed.is_running, "feed phải dừng sau khi hết retry")
        self.assertTrue(errors, "on_error phải được gọi")

    def test_subscribe_sends_frame(self):
        """subscribe() gửi đúng frame text (masked) tới server."""
        client_sock, server_sock = make_pair()
        feed = WebSocketFeed(
            "ws://mock/test",
            _connect_factory=lambda url: WebSocket(client_sock),
        )
        try:
            feed.connect()  # connect trực tiếp, không cần thread
            feed.subscribe({"method": "SUBSCRIBE", "params": ["x"]})
            fin, opcode, payload, was_masked = server_read_frame(server_sock)
            self.assertEqual(opcode, OP_TEXT)
            self.assertTrue(was_masked, "client phải mask frame theo RFC6455")
            self.assertEqual(json.loads(payload.decode()),
                             {"method": "SUBSCRIBE", "params": ["x"]})
        finally:
            feed.close()
            server_sock.close()

    def test_subscribe_before_connect_queued(self):
        """subscribe() trước khi connect -> gửi ngay khi connect xong."""
        client_sock, server_sock = make_pair()
        feed = WebSocketFeed(
            "ws://mock/test",
            _connect_factory=lambda url: WebSocket(client_sock),
        )
        try:
            feed.subscribe("hello-pending")
            feed.connect()
            _, opcode, payload, _ = server_read_frame(server_sock)
            self.assertEqual(opcode, OP_TEXT)
            self.assertEqual(payload.decode(), "hello-pending")
        finally:
            feed.close()
            server_sock.close()

    def test_no_backend_raises_clear_error(self):
        """Không có backend WS nào -> lỗi rõ ràng, không traceback khó hiểu."""
        import websocket_feed as wf
        with mock.patch.object(wf, "_WS_BACKEND", None), \
             mock.patch.object(wf, "_WS_IMPORT_ERROR",
                               ImportError("No module named 'tools'")):
            with self.assertRaises(WebSocketFeedError) as ctx:
                wf._require_backend()
            self.assertIn("tools/ws_client.py", str(ctx.exception))


class BinanceFeedTest(unittest.TestCase):
    TICKER = json.dumps({
        "e": "24hrTicker", "E": 1728971234567, "s": "BTCUSDT",
        "c": "97500.50", "o": "96000.00", "h": "98000.00", "l": "95500.00",
    })

    def test_parse_ticker_ok(self):
        parsed = BinancePriceFeed.parse_ticker(self.TICKER)
        self.assertEqual(parsed, {
            "symbol": "BTCUSDT",
            "price": 97500.50,
            "timestamp": 1728971234.567,
        })

    def test_parse_ticker_bad_inputs(self):
        self.assertIsNone(BinancePriceFeed.parse_ticker("not json"))
        self.assertIsNone(BinancePriceFeed.parse_ticker("{}"))
        self.assertIsNone(BinancePriceFeed.parse_ticker('{"s": "BTCUSDT"}'))
        self.assertIsNone(BinancePriceFeed.parse_ticker('{"s": "BTCUSDT", "c": "abc"}'))
        self.assertIsNone(BinancePriceFeed.parse_ticker(""))
        self.assertIsNone(BinancePriceFeed.parse_ticker("[1,2]"))

    def test_feed_emits_parsed_dict(self):
        """Feed nhận raw text WS -> callback nhận dict đã parse."""
        client_sock, server_sock = make_pair()
        got = threading.Event()
        received = []
        feed = BinancePriceFeed(
            on_message=lambda d: (received.append(d), got.set()),
            backoff_base=0.05, backoff_max=0.2,
            _connect_factory=lambda url: WebSocket(client_sock),
        )
        self.assertEqual(feed.url, BinancePriceFeed.DEFAULT_URL)
        feed.start()
        try:
            server_send_text(server_sock, self.TICKER)
            self.assertTrue(got.wait(timeout=5))
            self.assertEqual(received[0]["symbol"], "BTCUSDT")
            self.assertEqual(received[0]["price"], 97500.50)
            # message rác -> không gọi callback
            received.clear()
            server_send_text(server_sock, "ping-garbage")
            time.sleep(0.5)
            self.assertEqual(received, [])
        finally:
            feed.close()
            server_sock.close()

    def test_on_message_settable_for_manager(self):
        """FeedManager cần gán lại on_message sau add_feed."""
        cb1_calls, cb2_calls = [], []
        feed = BinancePriceFeed(on_message=cb1_calls.append)
        feed.on_message = cb2_calls.append  # manager làm thế này
        feed._emit(self.TICKER)  # raw text -> parse -> emit dict
        self.assertEqual(cb1_calls, [])
        self.assertEqual(len(cb2_calls), 1)
        self.assertEqual(cb2_calls[0]["symbol"], "BTCUSDT")


SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Test News</title>
<item><title><![CDATA[Tin nóng 1]]></title>
<link>https://example.com/1</link>
<pubDate>Fri, 16 Oct 2026 00:00:00 +0700</pubDate>
<description>Mô tả <b>tin 1</b></description></item>
<item><title>Tin 2</title><link>https://example.com/2</link>
<pubDate>Fri, 16 Oct 2026 01:00:00 +0700</pubDate>
<description>Mô tả tin 2</description></item>
</channel></rss>"""

BROKEN_RSS = ("<rss><channel>"
              "<item><title>Hỏng 1</title><link>https://x/1</link>"
              "<pubDate>today</pubDate><description>d1</description>"
              # cố tình thiếu </item> -> XML parser chết, regex fallback cứu
              "<item><title>Hỏng 2</title><link>https://x/2</link></item>"
              "</channel>")


class RSSTest(unittest.TestCase):
    def test_parse_rss_ok(self):
        items = parse_rss(SAMPLE_RSS, source="test-src")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Tin nóng 1")  # CDATA đã lột
        self.assertEqual(items[0]["link"], "https://example.com/1")
        self.assertIn("16 Oct 2026", items[0]["published"])
        self.assertEqual(items[0]["summary"], "Mô tả tin 1")  # tag <b> đã lột
        self.assertEqual(items[0]["source"], "test-src")
        self.assertEqual(items[1]["title"], "Tin 2")

    def test_parse_rss_regex_fallback(self):
        items = parse_rss(BROKEN_RSS)
        self.assertGreaterEqual(len(items), 1)
        self.assertTrue(any(i["title"] == "Hỏng 2" for i in items))

    def test_parse_rss_empty(self):
        self.assertEqual(parse_rss(""), [])
        self.assertEqual(parse_rss("   "), [])

    def test_fetch_rss_no_network(self):
        """fetch_rss dùng httpx nhưng test mock httpx.get -> không chạm mạng thật."""

        class FakeResp:
            text = SAMPLE_RSS

            def raise_for_status(self):
                pass

        with mock.patch("feeds.httpx.get", return_value=FakeResp()) as m:
            items = fetch_rss("https://fake.example/rss")
        m.assert_called_once()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Tin nóng 1")

    def test_fetch_rss_network_error_returns_empty(self):
        with mock.patch("feeds.httpx.get",
                        side_effect=Exception("mock network down")):
            self.assertEqual(fetch_rss("https://fake.example/rss"), [])

    def test_rss_feed_dedupe(self):
        """fetch_once lần 2 không trả lại item đã thấy."""
        with mock.patch("feeds.fetch_rss",
                        return_value=parse_rss(SAMPLE_RSS, source="u")):
            feed = RSSNewsFeed("https://fake.example/rss")
            first = feed.fetch_once()
            self.assertEqual(len(first), 2)
            second = feed.fetch_once()
            self.assertEqual(second, [], "phải dedupe theo link")

    def test_rss_feed_poll_loop(self):
        """poll() chạy thread, emit item mới qua on_message."""
        got = threading.Event()
        received = []
        with mock.patch("feeds.fetch_rss",
                        return_value=parse_rss(SAMPLE_RSS, source="u")):
            feed = RSSNewsFeed("https://fake.example/rss",
                               on_message=lambda d: (received.append(d),
                                                     got.set()))
            feed.poll(interval=60)
            try:
                self.assertTrue(got.wait(timeout=5))
                self.assertTrue(feed.is_running)
                self.assertEqual(len(received), 2)
            finally:
                feed.close()
            self.assertFalse(feed.is_running)


class FeedManagerTest(unittest.TestCase):
    def _make_ws_feed(self, on_message=None):
        c, s = make_pair()
        feed = WebSocketFeed("ws://mock/x", on_message=on_message,
                             backoff_base=0.05, backoff_max=0.2,
                             _connect_factory=lambda url: WebSocket(c))
        feed._test_server_sock = s  # để test đóng sau
        return feed

    def test_fanout(self):
        """1 message -> tất cả callback của feed đều nhận."""
        feed = self._make_ws_feed()
        mgr = FeedManager()
        mgr.add_feed("prices", feed)
        a, b = [], []
        mgr.on("prices", a.append)
        mgr.on("prices", b.append)
        try:
            feed.on_message("tick-1")  # giả lập message từ feed
            self.assertEqual(a, ["tick-1"])
            self.assertEqual(b, ["tick-1"])
        finally:
            mgr.stop_all()
            feed._test_server_sock.close()

    def test_fanout_keeps_original_callback(self):
        orig = []
        feed = self._make_ws_feed(on_message=orig.append)
        mgr = FeedManager()
        mgr.add_feed("prices", feed)
        extra = []
        mgr.on("prices", extra.append)
        try:
            feed.on_message("tick-1")
            self.assertEqual(orig, ["tick-1"])
            self.assertEqual(extra, ["tick-1"])
        finally:
            mgr.stop_all()
            feed._test_server_sock.close()

    def test_fanout_bad_callback_does_not_block_others(self):
        def boom(msg):
            raise RuntimeError("callback hỏng")

        feed = self._make_ws_feed()
        mgr = FeedManager()
        mgr.add_feed("x", feed)
        ok = []
        mgr.on("x", boom)
        mgr.on("x", ok.append)
        try:
            feed.on_message("m")
            self.assertEqual(ok, ["m"])
        finally:
            mgr.stop_all()
            feed._test_server_sock.close()

    def test_on_unknown_feed_raises(self):
        mgr = FeedManager()
        with self.assertRaises(KeyError):
            mgr.on("nope", lambda m: None)

    def test_add_duplicate_raises(self):
        feed = self._make_ws_feed()
        mgr = FeedManager()
        try:
            mgr.add_feed("x", feed)
            with self.assertRaises(ValueError):
                mgr.add_feed("x", feed)
        finally:
            mgr.stop_all()
            feed._test_server_sock.close()

    def test_off_removes_callback(self):
        feed = self._make_ws_feed()
        mgr = FeedManager()
        mgr.add_feed("x", feed)
        cb = []
        mgr.on("x", cb.append)
        self.assertTrue(mgr.off("x", cb.append))
        self.assertFalse(mgr.off("x", cb.append))
        try:
            feed.on_message("m")
            self.assertEqual(cb, [])
        finally:
            mgr.stop_all()
            feed._test_server_sock.close()

    def test_get_status(self):
        feed = self._make_ws_feed()
        mgr = FeedManager()
        mgr.add_feed("x", feed)
        try:
            self.assertEqual(mgr.get_status(), {"x": "stopped"})
            mgr.start_all()
            deadline = time.time() + 5
            while time.time() < deadline and not feed.is_running:
                time.sleep(0.02)
            self.assertEqual(mgr.get_status(), {"x": "running"})
            mgr.stop_all()
            self.assertEqual(mgr.get_status(), {"x": "stopped"})
        finally:
            feed._test_server_sock.close()

    def test_remove_feed_stops_it(self):
        feed = self._make_ws_feed()
        mgr = FeedManager()
        mgr.add_feed("x", feed)
        mgr.start_all()
        deadline = time.time() + 5
        while time.time() < deadline and not feed.is_running:
            time.sleep(0.02)
        try:
            mgr.remove_feed("x")
            self.assertNotIn("x", mgr)
            self.assertFalse(feed.is_running)
            self.assertEqual(mgr.get_status(), {})
        finally:
            feed._test_server_sock.close()

    def test_start_all_tolerates_broken_feed(self):
        class Broken(Feed):
            def start(self):
                raise RuntimeError("start hỏng")

            def close(self):
                pass

        feed = self._make_ws_feed()
        mgr = FeedManager()
        mgr.add_feed("broken", Broken())
        mgr.add_feed("ok", feed)
        try:
            mgr.start_all()  # không được raise
            deadline = time.time() + 5
            while time.time() < deadline and not feed.is_running:
                time.sleep(0.02)
            self.assertTrue(feed.is_running)
        finally:
            mgr.stop_all()
            feed._test_server_sock.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
