"""Tiện ích web cho Boom: search / fetch / extract / scrape.

Cấu trúc mới (gộp điểm mạnh của 2 module cũ):
  - ``web.py``        -> giữ circuit breaker + retry/backoff + httpx + body cap
  - ``web_tools.py``  -> giữ cache search (TTL 3600s, 128 entries, FIFO)
                         + batch fetch song song (web_extract_many)
  - ``scraper.py``    -> scrape_basic (list crawl + field selector)
  - ``browser_tool.py`` -> KHÔNG mang vào đây: cần Chrome thật + CDP, vẫn giữ
                         nguyên ở tool cũ cho ca JS-heavy.

Bảo mật (bắt buộc):
  - ``ssrf.validate_url``: chỉ cho http/https, chặn localhost, IP private,
    loopback, link-local, multicast, unspecified — chống SSRF.
    Mọi entry point có URL đều phải gọi nó (qua fetch.py / scrape.py).

Chỉ dùng stdlib + httpx (+ pydantic cho model kết quả).
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
    """Tool wrapper: không bao giờ raise vào agent loop, lỗi -> {"error"}."""
    try:
        result = fn()
        if isinstance(result, FetchResult):
            return result.model_dump()
        return result
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def build_tools(env=None) -> dict:
    """Registry-ready tool dicts cho environment của Boom (repo convention)."""

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
                "Tìm kiếm web qua DuckDuckGo (không cần API key). Kết quả "
                "[{title, url, snippet}], có cache 1 giờ. Lỗi -> {\"error\"}."
            ),
            "schema": _schema(
                {
                    "query": {"type": "string", "description": "Câu truy vấn."},
                    "max_results": {"type": "integer", "description": "Tối đa. Mặc định 5."},
                },
                ["query"],
            ),
        },
        "web_fetch": {
            "name": "web_fetch",
            "func": t_web_fetch,
            "description": (
                "Fetch 1 URL (http/https, chặn SSRF; follow redirect; body cap "
                "500 KiB; retry + circuit breaker). Trả về {url, status, "
                "content_type, text, title, truncated}."
            ),
            "schema": _schema(
                {
                    "url": {"type": "string", "description": "URL cần fetch."},
                    "timeout": {"type": "integer", "description": "Timeout giây. Mặc định 15."},
                },
                ["url"],
            ),
        },
        "web_extract": {
            "name": "web_extract",
            "func": t_web_extract,
            "description": (
                "Fetch URL rồi trả về text hiển thị chính (bỏ script/style/nav/"
                "footer). Có thể chỉ lấy 1 section bằng selector đơn giản "
                "('article', '.content', '#main', 'div.post')."
            ),
            "schema": _schema(
                {
                    "url": {"type": "string", "description": "URL cần extract."},
                    "selector": {
                        "type": "string",
                        "description": "Selector CSS đơn giản, tùy chọn.",
                    },
                },
                ["url"],
            ),
        },
        "web_extract_many": {
            "name": "web_extract_many",
            "func": t_web_extract_many,
            "description": (
                "Fetch song song nhiều URL; trả về {results: {url: kết quả}}. "
                "Dùng thay vì gọi web_extract từng URL khi cần nhiều trang. "
                "URL lỗi chỉ lỗi ở slot của nó."
            ),
            "schema": _schema(
                {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Danh sách URL fetch song song.",
                    },
                    "max_workers": {
                        "type": "integer",
                        "description": "Số thread song song. Mặc định 5.",
                    },
                },
                ["urls"],
            ),
        },
        "scrape_basic": {
            "name": "scrape_basic",
            "func": t_scrape_basic,
            "description": (
                "Scrape cấu trúc 1 trang HTML tĩnh: mỗi element khớp "
                "item_selector thành 1 item; fields {tên: selector}, hậu tố "
                "'@attr' để lấy attribute (VD {'link': 'a@href'}). Trang "
                "render bằng JS -> báo lỗi gợi ý dùng browser_tool."
            ),
            "schema": _schema(
                {
                    "url": {"type": "string", "description": "URL trang cần scrape."},
                    "item_selector": {
                        "type": "string",
                        "description": "Selector cho mỗi item (VD 'div.product').",
                    },
                    "fields": {
                        "type": "object",
                        "description": "{tên_field: selector}, VD {'title': 'h2', 'link': 'a@href'}.",
                    },
                    "max_items": {
                        "type": "integer",
                        "description": "Tối đa item. Mặc định 50.",
                    },
                },
                ["url", "item_selector", "fields"],
            ),
        },
    }
