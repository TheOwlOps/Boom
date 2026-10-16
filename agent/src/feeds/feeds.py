"""Ready-made feeds, free, no API key needed.

- BinancePriceFeed: realtime crypto prices from Binance's public stream.
- RSSNewsFeed: RSS polling (NOT WebSocket — RSS has no push, so it fetches
  periodically). Uses httpx, standard XML parsing, regex fallback.
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
    """Realtime prices via Binance's public WebSocket. No key needed.

    Default URL: wss://stream.binance.com:9443/ws/btcusdt@ticker
    (24h miniTicker: latest price `c`, symbol `s`, time `E`).

    The callback receives a dict: {"symbol": str, "price": float, "timestamp": float (seconds)}.
    """

    DEFAULT_URL = "wss://stream.binance.com:9443/ws/btcusdt@ticker"

    def __init__(self, url: str = DEFAULT_URL,
                 on_message: Optional[Callable[[Dict], None]] = None,
                 **kwargs):
        super().__init__(url, on_message=on_message, **kwargs)

    @staticmethod
    def parse_ticker(raw: str) -> Optional[Dict]:
        """Parses one Binance ticker message. Returns None on invalid messages."""
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
        """Overrides: WS raw text -> parse ticker -> then emit the price dict.

        Overrides _emit (instead of wrapping on_message) so FeedManager
        reassigning feed.on_message in add_feed still gets the parsed dict.
        """
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return
        parsed = self.parse_ticker(raw)
        if parsed is None:
            return  # ping / non-ticker message -> silently skipped
        super()._emit(parsed)


# ---------------------------------------------------------------- RSS
# Regex fallback for broken XML (real-world feeds often miss closing tags).
_TAG_RE = re.compile(r"<(\w+)[^>]*>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)


def _clean(text: Optional[str]) -> str:
    if not text:
        return ""
    text = _CDATA_RE.sub(r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)  # strips leftover HTML tags in descriptions
    return text.strip()


def _element_text(el) -> str:
    """Gets an element's full text, including text inside nested child tags.

    (ElementTree: <description>Description <b>item 1</b></description> -> .text only
    gives "Description ", while "item 1" lives in <b>.text — so use itertext().)
    """
    return _clean("".join(el.itertext()))


def parse_rss(xml_text: str, source: str = "") -> List[Dict]:
    """Parses RSS/Atom XML -> list of dicts {title, link, published, summary, source}.

    Tries the standard XML parser first; falls back to per-<item> regex on
    failure. Never raises on broken feeds — returns an empty list.
    """
    items: List[Dict] = []
    if not xml_text or not xml_text.strip():
        return items
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return _parse_rss_regex(xml_text, source)
    # RSS 2.0: channel/item ; Atom: entry (with namespace)
    for entry in root.iter():
        tag = entry.tag.split("}")[-1].lower()
        if tag not in ("item", "entry"):
            continue
        fields = {}
        for child in entry:
            ctag = child.tag.split("}")[-1].lower()
            fields[ctag] = _element_text(child)
        # Atom uses <link href="..."/> instead of text
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
    """Regex fallback when XML is too broken to parse.

    Splits on the opening <item> tag (instead of matching <item>...</item>
    pairs) to tolerate missing closing tags or nesting — pair matching would
    swallow the next item by mistake.
    """
    items = []
    parts = re.split(r"<item\b[^>]*>", xml_text, flags=re.IGNORECASE)
    for part in parts[1:]:
        chunk = part.split("</item>", 1)[0]
        fields: Dict[str, str] = {}
        for k, v in _TAG_RE.findall(chunk):
            kl = k.lower()
            if kl not in fields:  # keeps the first value, nested tags don't override
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
    """Fetches 1 RSS URL with httpx -> parses -> item list. Network errors return []."""
    try:
        resp = httpx.get(url, timeout=timeout,
                         headers={"User-Agent": "BoomBot/1.0 RSSNewsFeed"})
        resp.raise_for_status()
    except Exception:
        return []
    return parse_rss(resp.text, source=source or url)


class RSSNewsFeed(Feed):
    """Periodic RSS polling in its own thread. NOT a WebSocket.

    Each new item (unseen link/guid) triggers on_message once with a dict
    {title, link, published, summary, source}. Dedupes by link.
    """

    def __init__(self, urls, on_message: Optional[Callable[[Dict], None]] = None,
                 interval: float = 300.0):
        super().__init__(on_message)
        self.urls = [urls] if isinstance(urls, str) else list(urls)
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen = set()  # already-sent links -> dedupe
        self._seen_lock = threading.Lock()

    def _is_new(self, item: Dict) -> bool:
        key = item.get("link") or item.get("title")
        if not key:
            return True  # no key, can't dedupe, still send
        with self._seen_lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            if len(self._seen) > 5000:  # bounds the set on long runs
                self._seen = set(list(self._seen)[-2500:])
            return True

    def fetch_once(self) -> List[Dict]:
        """Fetches all URLs once, returns only new items."""
        fresh = []
        for url in self.urls:
            for item in fetch_rss(url, source=url):
                if self._is_new(item):
                    fresh.append(item)
        return fresh

    def poll(self, interval: float = 300.0) -> None:
        """Runs the poll loop in its own (daemon) thread. One call is enough."""
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
        # Fetch immediately on the first round so users don't wait out the interval.
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
                pass  # one bad poll round never kills the feed
        self._running = False

    def close(self) -> None:
        self._stop.set()
        th = self._thread
        if th is not None and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=10)
        self._running = False
