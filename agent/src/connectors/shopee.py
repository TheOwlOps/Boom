"""Shopee connector for Boom: product search and detail (best-effort, scraping-based).

HONEST LIMITATIONS — read before relying on this:
- Shopee has NO public buyer API. The endpoints below are Shopee's own
  internal web APIs (api/v4), which aggressively block non-browser clients
  (error 90309999, captcha pages, HTTP 403/429). Verified 2026-10-15: a plain
  HTTP client is served a captcha challenge, not product data.
- This connector tries the real endpoints and parses real responses WHEN they
  come through. When Shopee blocks, tools return
  {"error": "blocked by Shopee anti-bot ..."} — they never fake products and
  never attempt to bypass the block (no CAPTCHA solving, no token forgery).
- For reliable personal use, Tiki (see tiki.py, public API v2) is the better
  choice; Shopee data here is opportunistic.

Endpoints attempted:
- GET /api/v4/search/search_items?by=relevancy&keyword=...&limit=...
- GET /api/v4/item/get?itemid=...&shopid=...

Shopee prices in the API are integers in 1/100000 VND; they are converted to
VND here. Product URL: https://shopee.vn/<slug>-i.<shopid>.<itemid>

Rate limit: a delay between requests; personal price tracking only, not bulk
crawling. All failures are {"error": ...}, never raised.

Import root is agent/src, so this lives at agent/src/connectors/shopee.py and
is imported as `from connectors.shopee import ShopeeConnector`.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from connectors.base import Connector, _schema

API_BASE = "https://shopee.vn/api/v4"
SITE_BASE = "https://shopee.vn"
FETCH_TIMEOUT = 15.0
RATE_DELAY = 1.0
MAX_LIMIT = 20
BLOCKED_MARKERS = ("captcha", "robot", "challenge")
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _urllib_fetch(url: str) -> bytes:
    """Fetch a URL, returning raw bytes. Raises on network/HTTP failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                               "Accept": "application/json",
                                               "Accept-Language": "vi-VN,vi;q=0.9"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read()


def _is_blocked_payload(text: str, data: Any) -> bool:
    """True when Shopee answered with its anti-bot block instead of data."""
    if isinstance(data, dict) and data.get("error") == 90309999:
        return True
    lowered = text.lower()
    return any(m in lowered for m in BLOCKED_MARKERS)


def _to_vnd(raw: Any) -> Optional[int]:
    try:
        return int(raw) // 100000
    except (TypeError, ValueError):
        return None


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "san-pham"


def _summarize(basic: Dict[str, Any]) -> Dict[str, Any]:
    itemid = basic.get("itemid")
    shopid = basic.get("shopid")
    name = (basic.get("name") or "").strip()
    url = (f"{SITE_BASE}/{_slug(name)}-i.{shopid}.{itemid}"
           if itemid and shopid else "")
    rating = (basic.get("item_rating") or {}).get("rating_star")
    return {
        "itemid": itemid,
        "shopid": shopid,
        "name": name,
        "price": _to_vnd(basic.get("price")),
        "price_min": _to_vnd(basic.get("price_min")),
        "price_max": _to_vnd(basic.get("price_max")),
        "url": url,
        "shop_location": basic.get("shop_location") or "",
        "sold": basic.get("historical_sold"),
        "rating": rating,
    }


class ShopeeConnector(Connector):
    """Shopee search/detail over the internal web API (best-effort).

    `fetcher`/`sleeper` are injectable so tests never touch the network.
    """

    name = "shopee"

    def __init__(self,
                 fetcher: Optional[Callable[[str], bytes]] = None,
                 sleeper: Optional[Callable[[float], None]] = None):
        self._fetcher = fetcher or _urllib_fetch
        self._sleeper = sleeper or time.sleep

    # ------------------------------------------------------------------ contract

    def status(self) -> Dict[str, Any]:
        return {"connected": True,
                "detail": ("Scraping-based, no public buyer API; Shopee anti-bot "
                           "may block requests (reported honestly, never bypassed)")}

    def build_tools(self) -> Dict[str, Dict[str, Any]]:
        return {
            "shopee_search": {
                "name": "shopee_search",
                "func": self.shopee_search,
                "description": (
                    "Search Shopee products by keyword (best-effort: Shopee "
                    "anti-bot often blocks non-browser clients, reported as "
                    "an error dict). Returns name, price (VND), url, sold, rating."
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
            "shopee_product": {
                "name": "shopee_product",
                "func": self.shopee_product,
                "description": (
                    "Shopee product detail from a shopee.vn URL "
                    "(...-i.<shopid>.<itemid>). Best-effort, same anti-bot caveat."
                ),
                "schema": _schema(
                    {
                        "url": {"type": "string",
                                "description": "A shopee.vn product URL."},
                    },
                    ["url"],
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

    def _get_json(self, url: str) -> Any:
        """GET a JSON endpoint. Parsed dict, or {"error": ...} (incl. anti-bot)."""
        self._sleeper(RATE_DELAY)
        try:
            raw = self._fetcher(url)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                return {"error": (f"blocked by Shopee anti-bot (HTTP {e.code}); "
                                  "Shopee serves captcha to non-browser clients")}
            return {"error": f"Shopee HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"error": f"Shopee request failed: {type(e).__name__}: {e}"}
        text = raw.decode("utf-8", errors="replace")
        try:
            data = json.loads(text)
        except ValueError:
            if _is_blocked_payload(text, None):
                return {"error": ("blocked by Shopee anti-bot (captcha page); "
                                  "Shopee serves captcha to non-browser clients")}
            return {"error": "Shopee returned non-JSON response"}
        if _is_blocked_payload(text, data):
            return {"error": ("blocked by Shopee anti-bot (error 90309999); "
                              "Shopee serves captcha to non-browser clients")}
        return data

    @staticmethod
    def _ids_from_url(url: str) -> Any:
        m = re.search(r"-i\.(\d+)\.(\d+)", (url or ""))
        if not m:
            return {"error": ("could not find shopid/itemid in URL; expected "
                              "shopee.vn/...-i.<shopid>.<itemid>")}
        return {"shopid": m.group(1), "itemid": m.group(2)}

    # ------------------------------------------------------------------ tools

    def shopee_search(self, keyword: str, limit: int = 10) -> Dict[str, Any]:
        """Search products. Errors are {"error": ...}, never raised."""
        if not (keyword or "").strip():
            return {"error": "shopee_search needs a keyword."}
        n = self._clamp_limit(limit)
        if isinstance(n, dict):
            return n
        q = urllib.parse.quote_plus(keyword.strip())
        url = (f"{API_BASE}/search/search_items?by=relevancy&keyword={q}"
               f"&limit={n}&newest=0&order=desc&page_type=search"
               f"&scenario=PAGE_GLOBAL_SEARCH&version=2")
        data = self._get_json(url)
        if "error" in data:
            return data
        items = data.get("items") or []
        out = []
        for it in items[:n]:
            basic = it.get("item_basic") or {}
            if basic.get("itemid"):
                out.append(_summarize(basic))
        return {"keyword": keyword.strip(), "products": out, "total": len(out)}

    def shopee_product(self, url: str) -> Dict[str, Any]:
        """Product detail from a shopee.vn URL. Errors are {"error": ...}."""
        ids = self._ids_from_url(url)
        if "error" in ids:
            return ids
        api = (f"{API_BASE}/item/get?itemid={ids['itemid']}&shopid={ids['shopid']}")
        data = self._get_json(api)
        if "error" in data:
            return data
        item = data.get("item") or {}
        basic = item.get("item_basic") or item
        if not basic.get("itemid"):
            return {"error": "Shopee product not found in response."}
        out = _summarize(basic)
        out.update({
            "description": (item.get("description") or "")[:500],
            "stock": (item.get("stock") if isinstance(item.get("stock"), int)
                      else None),
        })
        return out


# Module-level instance picked up by connectors.registry.load_default_registry().
CONNECTOR = ShopeeConnector()
