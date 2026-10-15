"""Lazada connector for Boom: product search (best-effort, scraping-based).

HONEST LIMITATIONS — read before relying on this:
- Lazada has NO public buyer API and its catalog pages are JS-rendered shells:
  a plain HTTP fetch of https://www.lazada.vn/catalog/?q=... returns HTML with
  no embedded product data (verified 2026-10-15). Products only appear after
  JavaScript execution, which this stdlib-only connector does not do.
- This connector fetches the real pages and extracts product data WHEN the
  markup contains it (embedded JSON, JSON-LD, or product anchors). When the
  page is an empty JS shell, tools return {"error": ...} explaining so — they
  never fake products and never attempt to bypass anti-bot measures.
- For reliable personal price tracking, Tiki (see tiki.py, public API v2) is
  the better choice; Lazada data here is opportunistic.

Rate limit: a delay between requests; personal use only, not bulk crawling.
All failures are {"error": ...}, never raised.

Import root is agent/src, so this lives at agent/src/connectors/lazada.py and
is imported as `from connectors.lazada import LazadaConnector`.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional

from connectors.base import Connector, _schema

SITE_BASE = "https://www.lazada.vn"
FETCH_TIMEOUT = 15.0
RATE_DELAY = 1.0
MAX_LIMIT = 20
BLOCKED_MARKERS = ("captcha", "are you a robot", "access denied")
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _urllib_fetch(url: str) -> bytes:
    """Fetch a URL, returning raw bytes. Raises on network/HTTP failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                               "Accept-Language": "vi-VN,vi;q=0.9"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read()


class _LazPrice(HTMLParser):
    """Best-effort extractor for product anchors in Lazada markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.products: List[Dict[str, str]] = []
        self._cur: Optional[Dict[str, str]] = None
        self._in_name = False

    def handle_starttag(self, tag: str, attrs: List) -> None:
        attrs_d = dict(attrs)
        href = attrs_d.get("href", "")
        # Lazada product links look like /products/<slug>-<id>.html
        if tag == "a" and re.search(r"/products/.+-\d+\.html", href):
            self._cur = {"url": href if href.startswith("http") else SITE_BASE + href,
                         "name": ""}
        cls = attrs_d.get("class", "")
        if self._cur is not None and tag in ("div", "span") and "title" in cls:
            self._in_name = True

    def handle_data(self, data: str) -> None:
        if self._cur is not None and self._in_name:
            self._cur["name"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._cur is not None:
            name = re.sub(r"\s+", " ", self._cur["name"]).strip()
            if name:
                self._cur["name"] = name
                self.products.append(self._cur)
            self._cur = None
            self._in_name = False
        if tag in ("div", "span"):
            self._in_name = False


def _extract_products(html: str) -> List[Dict[str, Any]]:
    """Pull product-like entries out of Lazada HTML. May be empty (JS shell)."""
    out: List[Dict[str, Any]] = []
    # 1. Embedded JSON blobs that sometimes carry the listing.
    for m in re.finditer(r'"products"\s*:\s*(\[.*?\])\s*[,}]', html, re.S):
        try:
            for p in json.loads(m.group(1)):
                if isinstance(p, dict) and p.get("name"):
                    out.append(_norm_json_product(p))
        except (ValueError, TypeError):
            continue
    # 2. Product anchors in the markup.
    parser = _LazPrice()
    try:
        parser.feed(html)
    except Exception:
        pass
    seen = {p["url"] for p in out}
    for p in parser.products:
        if p["url"] not in seen:
            out.append({"name": p["name"], "url": p["url"],
                        "price": None, "original_price": None})
            seen.add(p["url"])
    return out


def _norm_json_product(p: Dict[str, Any]) -> Dict[str, Any]:
    price = p.get("price") or p.get("salePrice")
    url = p.get("productUrl") or p.get("url") or ""
    if url and not url.startswith("http"):
        url = SITE_BASE + url
    return {"name": str(p.get("name", "")).strip(),
            "price": price, "original_price": p.get("originalPrice"),
            "url": url}


class LazadaConnector(Connector):
    """Lazada search over the catalog pages (best-effort scraping).

    `fetcher`/`sleeper` are injectable so tests never touch the network.
    """

    name = "lazada"

    def __init__(self,
                 fetcher: Optional[Callable[[str], bytes]] = None,
                 sleeper: Optional[Callable[[float], None]] = None):
        self._fetcher = fetcher or _urllib_fetch
        self._sleeper = sleeper or time.sleep

    # ------------------------------------------------------------------ contract

    def status(self) -> Dict[str, Any]:
        return {"connected": True,
                "detail": ("Scraping-based, no public API; Lazada pages are "
                           "JS-rendered so simple HTTP often finds no data "
                           "(reported honestly)")}

    def build_tools(self) -> Dict[str, Dict[str, Any]]:
        return {
            "lazada_search": {
                "name": "lazada_search",
                "func": self.lazada_search,
                "description": (
                    "Search Lazada products by keyword (best-effort: pages are "
                    "JS-rendered, so a plain fetch often finds no product data; "
                    "reported as an error dict). Returns name/url/price when found."
                ),
                "schema": _schema(
                    {
                        "keyword": {"type": "string",
                                    "description": "Search keyword, e.g. 'tai nghe bluetooth'."},
                        "limit": {"type": "integer",
                                  "description": "Max results (1-20, default 10)."},
                    },
                    ["keyword"],
                ),
            },
        }

    # ------------------------------------------------------------------ helpers

    def _clamp_limit(self, limit: Any) -> Any:
        try:
            n = int(limit)
        except (TypeError, ValueError):
            return {"error": "limit must be an integer"}
        return max(1, min(n, MAX_LIMIT))

    def _fetch_html(self, url: str) -> Any:
        """GET a page. Returns text, or {"error": ...} (incl. anti-bot)."""
        self._sleeper(RATE_DELAY)
        try:
            raw = self._fetcher(url)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                return {"error": (f"blocked by Lazada anti-bot (HTTP {e.code})")}
            return {"error": f"Lazada HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"error": f"Lazada request failed: {type(e).__name__}: {e}"}
        text = raw.decode("utf-8", errors="replace")
        lowered = text.lower()
        if any(m in lowered for m in BLOCKED_MARKERS):
            return {"error": "blocked by Lazada anti-bot (challenge page)"}
        return text

    # ------------------------------------------------------------------ tools

    def lazada_search(self, keyword: str, limit: int = 10) -> Dict[str, Any]:
        """Search products. Errors are {"error": ...}, never raised."""
        if not (keyword or "").strip():
            return {"error": "lazada_search needs a keyword."}
        n = self._clamp_limit(limit)
        if isinstance(n, dict):
            return n
        q = urllib.parse.quote_plus(keyword.strip())
        html = self._fetch_html(f"{SITE_BASE}/catalog/?q={q}")
        if isinstance(html, dict):
            return html
        products = _extract_products(html)[:n]
        if not products:
            return {"error": ("no product data in Lazada response: the catalog "
                              "page is JS-rendered and a plain HTTP fetch sees "
                              "an empty shell (no embedded product JSON)")}
        return {"keyword": keyword.strip(), "products": products,
                "total": len(products)}


# Module-level instance picked up by connectors.registry.load_default_registry().
CONNECTOR = LazadaConnector()
