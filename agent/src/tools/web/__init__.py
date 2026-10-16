"""Web utilities for Boom: search / fetch / extract / scrape.

New structure (merging the 2 old modules' strengths):
  - ``web.py``        -> keeps circuit breaker + retry/backoff + httpx + body cap
  - ``web_tools.py``  -> keeps the search cache (TTL 3600s, 128 entries, FIFO)
                         + parallel batch fetch (web_extract_many)
  - ``scraper.py``    -> scrape_basic (list crawl + field selector)
  - ``browser_tool.py`` -> NOT brought in here: needs a real Chrome + CDP,
                         stays in the old tool for JS-heavy cases.

Security (mandatory):
  - ``ssrf.validate_url``: http/https only; blocks localhost, private IPs,
    loopback, link-local, multicast, unspecified — anti-SSRF.
    Every URL entry point must call it (via fetch.py / scrape.py).

Stdlib + httpx only (+ pydantic for the result model).
"""

from .fetch import (
    CircuitOpenError,
    FetchResult,
    WebFetchError,
    web_extract,
    web_extract_many,
    web_fetch,
)
from .scrape import ScrapeError, scrape_basic
from .search import WebSearchError, clear_web_search_cache, parse_ddg_html, web_search
from .ssrf import SSRFError, validate_url

__all__ = [
    # search
    "WebSearchError",
    "web_search",
    "clear_web_search_cache",
    "parse_ddg_html",
    # fetch / extract
    "WebFetchError",
    "CircuitOpenError",
    "FetchResult",
    "web_fetch",
    "web_extract",
    "web_extract_many",
    # scrape
    "ScrapeError",
    "scrape_basic",
    # ssrf
    "SSRFError",
    "validate_url",
    # registry
    "build_tools",
]


def _schema(properties=None, required=None):
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
    }


def _guard(fn):
    """Tool wrapper: never raises into the agent loop, errors -> {"error"}."""
    try:
        result = fn()
        if isinstance(result, FetchResult):
            return result.model_dump()
        return result
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def build_tools(env=None) -> dict:
    """Registry-ready tool dicts for Boom's environment (repo convention)."""

    def t_web_search(query: str, max_results: int = 5):
        return _guard(lambda: web_search(query, int(max_results)))

    def t_web_fetch(url: str, timeout: int = 15):
        return _guard(lambda: web_fetch(url, int(timeout)))

    def t_web_extract(url: str, selector: str = ""):
        return _guard(lambda: web_extract(url, selector or None))

    def t_web_extract_many(urls, max_workers: int = 5):
        return _guard(lambda: web_extract_many(list(urls or []), int(max_workers)))

    def t_scrape_basic(url: str, item_selector: str, fields: dict, max_items: int = 50):
        return _guard(
            lambda: scrape_basic(url, item_selector, dict(fields or {}), int(max_items))
        )

    return {
        "web_search": {
            "name": "web_search",
            "func": t_web_search,
            "description": (
                "Searches the web via DuckDuckGo (no API key). Results "
                "[{title, url, snippet}], cached for 1 hour. Errors -> {\"error\"}."
            ),
            "schema": _schema(
                {
                    "query": {"type": "string", "description": "Search query."},
                    "max_results": {"type": "integer", "description": "Max results. Default 5."},
                },
                ["query"],
            ),
        },
        "web_fetch": {
            "name": "web_fetch",
            "func": t_web_fetch,
            "description": (
                "Fetches 1 URL (http/https, SSRF-blocked; follows redirects; "
                "500 KiB body cap; retry + circuit breaker). Returns {url, status, "
                "content_type, text, title, truncated}."
            ),
            "schema": _schema(
                {
                    "url": {"type": "string", "description": "URL to fetch."},
                    "timeout": {"type": "integer", "description": "Timeout in seconds. Default 15."},
                },
                ["url"],
            ),
        },
        "web_extract": {
            "name": "web_extract",
            "func": t_web_extract,
            "description": (
                "Fetches a URL and returns its main visible text (drops script/style/nav/"
                "footer). Can grab one section with a simple selector "
                "('article', '.content', '#main', 'div.post')."
            ),
            "schema": _schema(
                {
                    "url": {"type": "string", "description": "URL to extract."},
                    "selector": {
                        "type": "string",
                        "description": "Simple CSS selector, optional.",
                    },
                },
                ["url"],
            ),
        },
        "web_extract_many": {
            "name": "web_extract_many",
            "func": t_web_extract_many,
            "description": (
                "Fetches many URLs in parallel; returns {results: {url: result}}. "
                "Use instead of calling web_extract per URL when many pages are needed. "
                "A failing URL only fails its own slot."
            ),
            "schema": _schema(
                {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "URLs to fetch in parallel.",
                    },
                    "max_workers": {
                        "type": "integer",
                        "description": "Parallel threads. Default 5.",
                    },
                },
                ["urls"],
            ),
        },
        "scrape_basic": {
            "name": "scrape_basic",
            "func": t_scrape_basic,
            "description": (
                "Structured scrape of one static HTML page: each element matching "
                "item_selector becomes an item; fields {name: selector}, '@attr' "
                "suffix to grab an attribute (e.g. {'link': 'a@href'}). "
                "JS-rendered pages -> an error suggesting browser_tool."
            ),
            "schema": _schema(
                {
                    "url": {"type": "string", "description": "Page URL to scrape."},
                    "item_selector": {
                        "type": "string",
                        "description": "Selector for each item (e.g. 'div.product').",
                    },
                    "fields": {
                        "type": "object",
                        "description": "{field_name: selector}, e.g. {'title': 'h2', 'link': 'a@href'}.",
                    },
                    "max_items": {
                        "type": "integer",
                        "description": "Max items. Default 50.",
                    },
                },
                ["url", "item_selector", "fields"],
            ),
        },
    }
