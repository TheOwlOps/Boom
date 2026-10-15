"""News connector for Boom: Vietnamese headlines via RSS, free, no API key.

Reads VnExpress RSS feeds (tin mới nhất, kinh doanh, thể thao) — or any custom
RSS URL — with stdlib only (urllib + xml.etree.ElementTree). No credential is
needed, so status() is always connected; a dead feed or bad XML is reported as
{"error": ...}, never faked.

Import root is agent/src, so this lives at agent/src/connectors/news.py and is
imported as `from connectors.news import NewsConnector`.
"""

import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional

from connectors.base import Connector, _schema

FEEDS: Dict[str, str] = {
    "tin-moi-nhat": "https://vnexpress.net/rss/tin-moi-nhat.rss",
    "kinh-doanh": "https://vnexpress.net/rss/kinh-doanh.rss",
    "the-thao": "https://vnexpress.net/rss/the-thao.rss",
}
DEFAULT_FEED = "tin-moi-nhat"
FETCH_TIMEOUT = 15.0
SUMMARY_CHARS = 200
MAX_LIMIT = 50
_USER_AGENT = "BoomNewsConnector/1.0 (+https://github.com/TheOwlOps/Boom)"


class _TextStripper(HTMLParser):
    """Collect visible text from an HTML fragment, dropping tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._parts)).strip()


def _strip_html(fragment: str) -> str:
    stripper = _TextStripper()
    try:
        stripper.feed(fragment or "")
    except Exception:
        pass
    return stripper.text()


def _urllib_fetch(url: str) -> bytes:
    """Fetch a URL, returning raw bytes. Raises on network/HTTP failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read()


def _text_of(item: ET.Element, tag: str) -> str:
    """Text of a child tag, tolerating namespaces (e.g. dc:date)."""
    child = item.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    for elem in item.iter():
        local = elem.tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]
        if local == tag and elem.text:
            return elem.text.strip()
    return ""


def _parse_rss(raw: bytes) -> List[Dict[str, str]]:
    """RSS 2.0 bytes -> [{title, link, pubDate, summary}]. ValueError on bad XML."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise ValueError(f"could not parse RSS feed: {e}")
    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall("item")
    if not items:
        items = root.iter("item")
    out: List[Dict[str, str]] = []
    for item in items:
        title = _text_of(item, "title")
        if not title:
            continue
        link = _text_of(item, "link")
        pub_date = _text_of(item, "pubDate") or _text_of(item, "date")
        summary = _strip_html(_text_of(item, "description"))[:SUMMARY_CHARS]
        out.append({"title": title, "link": link, "pubDate": pub_date,
                    "summary": summary})
    return out


class NewsConnector(Connector):
    """VnExpress RSS headlines. `fetcher` is injectable so tests never touch the network."""

    name = "news"

    def __init__(self, fetcher: Optional[Callable[[str], bytes]] = None):
        self._fetcher = fetcher or _urllib_fetch

    # ------------------------------------------------------------------ contract

    def status(self) -> Dict[str, Any]:
        return {"connected": True,
                "detail": "RSS feeds, no API key required"}

    def build_tools(self) -> Dict[str, Dict[str, Any]]:
        return {
            "news_headlines": {
                "name": "news_headlines",
                "func": self.news_headlines,
                "description": (
                    "Latest headlines from a VnExpress RSS feed "
                    "(tin-moi-nhat, kinh-doanh, the-thao) or any custom RSS URL. "
                    "Returns title/link/pubDate/summary per item."
                ),
                "schema": _schema(
                    {
                        "feed": {"type": "string",
                                 "description": "Feed key (tin-moi-nhat, kinh-doanh, "
                                                "the-thao) or a full RSS URL. "
                                                "Default: tin-moi-nhat."},
                        "limit": {"type": "integer",
                                  "description": "Max headlines (1-50, default 10)."},
                    },
                    [],
                ),
            },
            "news_search": {
                "name": "news_search",
                "func": self.news_search,
                "description": (
                    "Search the latest VnExpress headlines for a keyword "
                    "(case-insensitive match on the title)."
                ),
                "schema": _schema(
                    {
                        "keyword": {"type": "string",
                                    "description": "Keyword to find in headlines."},
                        "limit": {"type": "integer",
                                  "description": "Max results (1-50, default 10)."},
                    },
                    ["keyword"],
                ),
            },
        }

    # ------------------------------------------------------------------ helpers

    def _resolve_url(self, feed: Optional[str]) -> Any:
        key = (feed or "").strip() or DEFAULT_FEED
        if key in FEEDS:
            return FEEDS[key]
        if key.startswith("http://") or key.startswith("https://"):
            return key
        return {"error": f"unknown feed {key!r}: use one of "
                         f"{sorted(FEEDS)} or a full RSS URL"}

    def _clamp_limit(self, limit: Any) -> Any:
        try:
            n = int(limit)
        except (TypeError, ValueError):
            return {"error": "limit must be an integer"}
        return max(1, min(n, MAX_LIMIT))

    def _fetch_items(self, url: str) -> Any:
        try:
            raw = self._fetcher(url)
        except Exception as e:
            return {"error": f"could not fetch RSS feed: {type(e).__name__}: {e}"}
        try:
            return _parse_rss(raw)
        except ValueError as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------ tools

    def news_headlines(self, feed: Optional[str] = None,
                       limit: int = 10) -> Dict[str, Any]:
        """Latest headlines from a feed. Errors are {"error": ...}, never raised."""
        url = self._resolve_url(feed)
        if isinstance(url, dict):
            return url
        n = self._clamp_limit(limit)
        if isinstance(n, dict):
            return n
        items = self._fetch_items(url)
        if isinstance(items, dict):
            return items
        return {"feed": url, "headlines": items[:n], "total": min(n, len(items))}

    def news_search(self, keyword: str, limit: int = 10) -> Dict[str, Any]:
        """Headlines from tin-moi-nhat whose title contains keyword (case-insensitive)."""
        if not (keyword or "").strip():
            return {"error": "news_search needs a keyword."}
        n = self._clamp_limit(limit)
        if isinstance(n, dict):
            return n
        items = self._fetch_items(FEEDS[DEFAULT_FEED])
        if isinstance(items, dict):
            return items
        needle = keyword.strip().lower()
        hits = [it for it in items if needle in it["title"].lower()][:n]
        return {"keyword": keyword.strip(), "headlines": hits, "total": len(hits)}


# Module-level instance picked up by connectors.registry.load_default_registry().
CONNECTOR = NewsConnector()
