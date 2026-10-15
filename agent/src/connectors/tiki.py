"""Tiki connector for Boom: product search and detail via Tiki's public API v2.

Unlike Shopee/Lazada, Tiki exposes a public, no-key JSON API
(https://tiki.vn/api/v2/products) that returns real product data — this
connector is API-based, not HTML scraping. Verified working 2026-10-15.

Endpoints used:
- GET /api/v2/products?q=<keyword>&limit=<n>  -> {"data": [...], "paging": {...}}
- GET /api/v2/products/<id>                  -> {"data": {...}} (404 JSON when unknown)

Product page URL: https://tiki.vn/<url_path>

Rate limit: a short delay between requests; this is a personal-use price
tracker, not a bulk crawler. All failures are {"error": ...}, never raised.

Import root is agent/src, so this lives at agent/src/connectors/tiki.py and is
imported as `from connectors.tiki import TikiConnector`.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from connectors.base import Connector, _schema

API_BASE = "https://tiki.vn/api/v2"
SITE_BASE = "https://tiki.vn"
FETCH_TIMEOUT = 15.0
RATE_DELAY = 0.5
MAX_LIMIT = 20
_USER_AGENT = "BoomTikiConnector/1.0 (+https://github.com/TheOwlOps/Boom)"


def _urllib_fetch(url: str) -> bytes:
    """Fetch a URL, returning raw bytes. Raises on network/HTTP failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read()


def _product_url(item: Dict[str, Any]) -> str:
    path = (item.get("url_path") or "").lstrip("/")
    return f"{SITE_BASE}/{path}" if path else ""


def _summarize(item: Dict[str, Any]) -> Dict[str, Any]:
    """Pick the stable fields out of a Tiki product object."""
    return {
        "id": item.get("id"),
        "name": (item.get("name") or "").strip(),
        "price": item.get("price"),
        "original_price": item.get("original_price"),
        "url": _product_url(item),
        "seller": item.get("seller_name") or "",
        "brand": item.get("brand_name") or "",
        "rating": item.get("rating_average"),
    }


class TikiConnector(Connector):
    """Tiki product search/detail over the public API v2.

    `fetcher` is injectable so tests never touch the network. A `sleeper`
    is injectable so tests don't wait on the rate-limit delay.
    """

    name = "tiki"

    def __init__(self,
                 fetcher: Optional[Callable[[str], bytes]] = None,
                 sleeper: Optional[Callable[[float], None]] = None):
        self._fetcher = fetcher or _urllib_fetch
        self._sleeper = sleeper or time.sleep

    # ------------------------------------------------------------------ contract

    def status(self) -> Dict[str, Any]:
        return {"connected": True,
                "detail": "Tiki public API v2 (tiki.vn/api/v2), no key required"}

    def build_tools(self) -> Dict[str, Dict[str, Any]]:
        return {
            "tiki_search": {
                "name": "tiki_search",
                "func": self.tiki_search,
                "description": (
                    "Search Tiki products by keyword. Returns id, name, price, "
                    "original_price, url, seller, brand and rating per hit."
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
            "tiki_product": {
                "name": "tiki_product",
                "func": self.tiki_product,
                "description": (
                    "Product detail from Tiki by id (or a tiki.vn product URL): "
                    "price, original_price, rating, review count, stock quantity."
                ),
                "schema": _schema(
                    {
                        "product": {"type": "string",
                                    "description": "Tiki product id, or a tiki.vn product URL."},
                    },
                    ["product"],
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
        """GET a JSON endpoint. Returns the parsed dict, or {"error": ...}."""
        self._sleeper(RATE_DELAY)
        try:
            raw = self._fetcher(url)
        except urllib.error.HTTPError as e:
            return {"error": f"Tiki HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"error": f"Tiki request failed: {type(e).__name__}: {e}"}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            return {"error": f"Tiki returned non-JSON: {e}"}

    @staticmethod
    def _product_id(product: str) -> Any:
        """Accept a bare id or a tiki.vn URL; return the numeric id or {"error"}."""
        text = (product or "").strip()
        if not text:
            return {"error": "tiki_product needs a product id or URL."}
        if text.isdigit():
            return text
        m = re.search(r"-p(\d+)(?:\.html)?", text)
        if m:
            return m.group(1)
        return {"error": f"could not find a Tiki product id in {text!r}"}

    # ------------------------------------------------------------------ tools

    def tiki_search(self, keyword: str, limit: int = 10) -> Dict[str, Any]:
        """Search products. Errors are {"error": ...}, never raised."""
        if not (keyword or "").strip():
            return {"error": "tiki_search needs a keyword."}
        n = self._clamp_limit(limit)
        if isinstance(n, dict):
            return n
        q = urllib.parse.quote_plus(keyword.strip())
        data = self._get_json(f"{API_BASE}/products?q={q}&limit={n}")
        if "error" in data:
            return data
        items = data.get("data") or []
        return {
            "keyword": keyword.strip(),
            "products": [_summarize(it) for it in items[:n]],
            "total": (data.get("paging") or {}).get("total"),
        }

    def tiki_product(self, product: str) -> Dict[str, Any]:
        """Product detail by id or URL. Errors are {"error": ...}, never raised."""
        pid = self._product_id(product)
        if isinstance(pid, dict):
            return pid
        data = self._get_json(f"{API_BASE}/products/{pid}")
        if "error" in data:
            return data
        # Detail API returns the product object directly; tolerate a {"data": ...}
        # wrapper too, in case the shape varies.
        item = data.get("data") if isinstance(data.get("data"), dict) else data
        if not isinstance(item, dict) or not item.get("id"):
            return {"error": f"Tiki product {pid} not found."}
        stock = item.get("stock_item") or {}
        out = _summarize(item)
        out.update({
            "review_count": item.get("review_count"),
            "stock_qty": stock.get("qty"),
            "description": (item.get("short_description") or "")[:500],
        })
        return out


# Module-level instance picked up by connectors.registry.load_default_registry().
CONNECTOR = TikiConnector()
