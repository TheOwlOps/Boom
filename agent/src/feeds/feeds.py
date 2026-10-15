"""Các feed có sẵn, miễn phí, không cần API key.

- BinancePriceFeed: WebSocket giá crypto realtime từ Binance public stream.
- RSSNewsFeed: polling RSS (KHÔNG phải WebSocket — RSS không có push,
  phải fetch định kỳ). Dùng httpx, parse XML chuẩn, regex dự phòng.
"""
from __future__ import annotations

import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Callable, Dict, List, Optional

import httpx

from websocket_feed import Feed, WebSocketFeed


# ---------------------------------------------------------------- Binance
class BinancePriceFeed(WebSocketFeed):
    """Giá realtime qua Binance public WebSocket. Không cần key.

    URL mặc định: wss://stream.binance.com:9443/ws/btcusdt@ticker
    (miniTicker 24h: giá mới nhất `c`, symbol `s`, thời gian `E`).

    Callback nhận dict: {"symbol": str, "price": float, "timestamp": float (giây)}.
    """

    DEFAULT_URL = "wss://stream.binance.com:9443/ws/btcusdt@ticker"

    def __init__(self, url: str = DEFAULT_URL,
                 on_message: Optional[Callable[[Dict], None]] = None,
                 **kwargs):
        super().__init__(url, on_message=on_message, **kwargs)

    @staticmethod
    def parse_ticker(raw: str) -> Optional[Dict]:
        """Parse 1 message ticker của Binance. Trả None nếu message không hợp lệ."""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        symbol = data.get("s")
        price = data.get("c")
        if symbol is None or price is None:
            return None
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            return None
        ts = data.get("E")
        timestamp = float(ts) / 1000.0 if ts else time.time()
        return {"symbol": str(symbol), "price": price_f, "timestamp": timestamp}

    def _emit(self, raw) -> None:
        """Ghi đè: raw text từ WS -> parse ticker -> mới emit dict giá.

        Ghi đè _emit (thay vì bọc on_message) để FeedManager gán lại
        feed.on_message lúc add_feed vẫn nhận đúng dict đã parse.
        """
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return
        parsed = self.parse_ticker(raw)
        if parsed is None:
            return  # message ping/không phải ticker -> bỏ qua lặng lẽ
        super()._emit(parsed)


# ---------------------------------------------------------------- RSS
# Regex dự phòng khi XML bị hỏng (feed thực tế ngoài đời hay thiếu tag đóng).
_TAG_RE = re.compile(r"<(\w+)[^>]*>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)


def _clean(text: Optional[str]) -> str:
    if not text:
        return ""
    text = _CDATA_RE.sub(r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)  # lột tag HTML còn sót trong description
    return text.strip()


def _element_text(el) -> str:
    """Lấy toàn bộ text của element kể cả text trong tag con lồng nhau.

    (ElementTree: <description>Mô tả <b>tin 1</b></description> -> .text chỉ
    cho "Mô tả ", còn "tin 1" nằm trong <b>.text -> phải dùng itertext().)
    """
    return _clean("".join(el.itertext()))


def parse_rss(xml_text: str, source: str = "") -> List[Dict]:
    """Parse RSS/Atom XML -> list dict {title, link, published, summary, source}.

    Thử XML parser chuẩn trước; hỏng thì fallback regex từng <item>.
    Không raise với feed hỏng — trả list rỗng.
    """
    items: List[Dict] = []
    if not xml_text or not xml_text.strip():
        return items
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return _parse_rss_regex(xml_text, source)
    # RSS 2.0: channel/item ; Atom: entry (có namespace)
    for entry in root.iter():
        tag = entry.tag.split("}")[-1].lower()
        if tag not in ("item", "entry"):
            continue
        fields = {}
        for child in entry:
            ctag = child.tag.split("}")[-1].lower()
            fields[ctag] = _element_text(child)
        # Atom dùng <link href="..."/> thay vì text
        link = fields.get("link", "")
        if not link:
            for child in entry:
                if child.tag.split("}")[-1].lower() == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        items.append({
            "title": fields.get("title", ""),
            "link": link,
            "published": fields.get("pubdate", "") or fields.get("published", "")
                       or fields.get("updated", ""),
            "summary": fields.get("description", "") or fields.get("summary", ""),
            "source": source,
        })
    return items


def _parse_rss_regex(xml_text: str, source: str = "") -> List[Dict]:
    """Fallback regex khi XML hỏng không parse được.

    Tách theo thẻ <item> mở (thay vì match cặp <item>...</item>) để chịu được
    item thiếu thẻ đóng hoặc lồng nhau — trường hợp match cặp sẽ nuốt nhầm
    item kế tiếp vào trong.
    """
    items = []
    parts = re.split(r"<item\b[^>]*>", xml_text, flags=re.IGNORECASE)
    for part in parts[1:]:
        chunk = part.split("</item>", 1)[0]
        fields: Dict[str, str] = {}
        for k, v in _TAG_RE.findall(chunk):
            kl = k.lower()
            if kl not in fields:  # giữ giá trị đầu, tránh tag lồng đè lên
                fields[kl] = _clean(v)
        if not any(fields.get(k) for k in ("title", "link", "description", "pubdate")):
            continue
        items.append({
            "title": fields.get("title", ""),
            "link": fields.get("link", ""),
            "published": fields.get("pubdate", ""),
            "summary": fields.get("description", ""),
            "source": source,
        })
    return items


def fetch_rss(url: str, timeout: float = 20.0, source: str = "") -> List[Dict]:
    """Fetch 1 RSS URL bằng httpx -> parse -> list item. Lỗi mạng trả list rỗng."""
    try:
        resp = httpx.get(url, timeout=timeout,
                         headers={"User-Agent": "BoomBot/1.0 RSSNewsFeed"})
        resp.raise_for_status()
    except Exception:
        return []
    return parse_rss(resp.text, source=source or url)


class RSSNewsFeed(Feed):
    """Poll RSS định kỳ trong thread riêng. KHÔNG phải WebSocket.

    Mỗi item mới (chưa thấy link/guid) gọi on_message 1 lần với dict
    {title, link, published, summary, source}. Có dedupe theo link.
    """

    def __init__(self, urls, on_message: Optional[Callable[[Dict], None]] = None,
                 interval: float = 300.0):
        super().__init__(on_message)
        self.urls = [urls] if isinstance(urls, str) else list(urls)
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen = set()  # link đã gửi -> dedupe
        self._seen_lock = threading.Lock()

    def _is_new(self, item: Dict) -> bool:
        key = item.get("link") or item.get("title")
        if not key:
            return True  # không có key thì vẫn gửi (không dedupe được)
        with self._seen_lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            if len(self._seen) > 5000:  # chống phình set khi chạy lâu
                self._seen = set(list(self._seen)[-2500:])
            return True

    def fetch_once(self) -> List[Dict]:
        """Fetch tất cả URL 1 lượt, chỉ trả item mới."""
        fresh = []
        for url in self.urls:
            for item in fetch_rss(url, source=url):
                if self._is_new(item):
                    fresh.append(item)
        return fresh

    def poll(self, interval: float = 300.0) -> None:
        """Chạy vòng poll trong thread riêng (daemon). Gọi 1 lần là đủ."""
        self.interval = interval
        self.start()

    def start(self) -> None:
        if self._running:
            return
        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, name=f"rssfeed-{id(self)}", daemon=True
        )
        self._thread.start()

    def _poll_loop(self) -> None:
        # Fetch ngay lượt đầu để user không phải chờ hết interval.
        first = True
        while not self._stop.is_set():
            if not first:
                if self._stop.wait(timeout=self.interval):
                    break
            first = False
            try:
                for item in self.fetch_once():
                    if self._stop.is_set():
                        break
                    self._emit(item)
            except Exception:
                pass  # lỗi 1 vòng poll không giết feed
        self._running = False

    def close(self) -> None:
        self._stop.set()
        th = self._thread
        if th is not None and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=10)
        self._running = False
