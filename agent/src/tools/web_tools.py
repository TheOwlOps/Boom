"""Real web search and page extraction — standard library only.

Search posts to DuckDuckGo's no-JS HTML endpoint; extract fetches a URL with urllib
and reduces HTML to visible text. Both return ``{"error": ...}`` on failure and never
fabricate rows. Everything is byte/stdin-pure except ``_http``: the parsing helpers
take a str and return data, and ``SEARCH_ENDPOINT`` is the seam the evals point at a
local server.

``web_search`` results are cached in memory (TTL 3600s, 128 entries, FIFO eviction;
see its docstring). ``web_extract_many`` fetches many URLs concurrently via a
thread pool and is exposed as a tool alongside ``web_extract``.
"""

import concurrent.futures
import copy
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 20.0
MAX_CHARS = 20000
MAX_RESULTS = 5
MAX_DOWNLOAD = 4 * 1024 * 1024  # read cap, so one huge page cannot exhaust memory

# ------------------------------------------------------------------ S5 search cache
# In-memory cache for web_search results: keyed by (stripped query, normalized
# limit), holding (monotonic timestamp, deep-copied result). Bounded at
# _WEB_SEARCH_CACHE_MAX entries with FIFO eviction (oldest inserted dropped
# first). Invalidation is TTL-only: an entry older than _WEB_SEARCH_CACHE_TTL
# is treated as a miss and re-fetched. Search results are public data, not user
# state, so caching them is safe; errors are never cached.
_WEB_SEARCH_CACHE_TTL = 3600.0  # seconds
_WEB_SEARCH_CACHE_MAX = 128     # entries
_web_search_cache: Dict[Tuple[str, int], Tuple[float, Dict[str, Any]]] = {}
_web_search_cache_lock = threading.Lock()

# Tags whose text is never visible; script/style also hold what a naive extractor leaks.
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
# Tags that imply a line break in the rendered page.
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "table", "h1", "h2", "h3",
    "h4", "h5", "h6", "section", "article", "header", "footer", "nav", "aside",
    "blockquote", "pre", "form", "hr", "title",
}

_SEARCH_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
}


class WebError(Exception):
    """A network/protocol failure. Its message becomes the ``{"error": ...}`` value."""


def _http(url: str, data: Optional[bytes] = None) -> Tuple[bytes, str, str]:
    """The one network call. Returns (body, content_type, charset); raises WebError."""
    if not url.lower().startswith(("http://", "https://")):
        raise WebError(f"unsupported URL scheme (only http/https): {url!r}")
    headers = dict(_SEARCH_HEADERS)
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read(MAX_DOWNLOAD)
            return body, resp.headers.get_content_type(), resp.headers.get_content_charset() or ""
    except urllib.error.HTTPError as e:  # subclass of URLError: check it first
        raise WebError(f"HTTP {e.code} for {url}") from e
    except urllib.error.URLError as e:
        raise WebError(f"cannot reach {url}: {e.reason}") from e
    except (socket.timeout, TimeoutError) as e:
        raise WebError(f"timeout after {TIMEOUT:g}s for {url}") from e


def _decode(body: bytes, charset: str) -> str:
    for enc in (charset, "utf-8"):
        if not enc:
            continue
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", "replace")


def _collapse(text: str) -> str:
    text = re.sub(r"[ \t\f\v\r]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


def _cap(text: str) -> Tuple[str, bool]:
    if len(text) <= MAX_CHARS:
        return text, False
    return text[:MAX_CHARS] + f"\n\n[truncated: showing first {MAX_CHARS} of {len(text)} characters]", True


class _TextExtractor(HTMLParser):
    """Visible text plus <title> from an HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
        self.parts.append(data)


def html_to_text(markup: str) -> Tuple[str, str]:
    """Pure: (visible text, title) for an HTML string. script/style/noscript dropped."""
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return _collapse("".join(parser.parts)), _collapse(parser.title)


class _DDGResults(HTMLParser):
    """Collects result rows from a DuckDuckGo no-JS SERP."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[Dict[str, str]] = []
        self._field: str = ""      # "title" | "snippet" while inside a result element
        self._close_tag: str = ""
        self._buf: List[str] = []
        self._href = ""

    @staticmethod
    def _has_class(attrs: List[Tuple[str, Optional[str]]], name: str) -> str:
        for key, value in attrs:
            if key == "class" and name in (value or "").split():
                return value or ""
        return ""

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        classes = self._has_class(attrs, "result__a") or self._has_class(attrs, "result__snippet")
        if not classes or self._field:
            return
        self._field = "title" if "result__a" in classes.split() else "snippet"
        self._close_tag = tag
        self._buf = []
        self._href = dict(attrs).get("href") or ""

    def handle_data(self, data: str) -> None:
        if self._field:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._field or tag != self._close_tag:
            return
        text = _collapse(" ".join(self._buf))
        if self._field == "title":
            url = clean_result_url(self._href)
            if url:
                self.rows.append({"title": text, "url": url, "snippet": ""})
                self._href = ""
        elif self.rows and text:
            if not self.rows[-1]["snippet"]:
                self.rows[-1]["snippet"] = text
        self._field = ""
        self._close_tag = ""
        self._buf = []


def clean_result_url(href: str) -> str:
    """Pure: resolve a SERP href, unwrapping DuckDuckGo's ``/l/?uddg=`` redirect."""
    href = (href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    parts = urllib.parse.urlsplit(href)
    host = parts.netloc
    # The redirect is served as //duckduckgo.com/l/?uddg=... and, inside the SERP, as /l/?uddg=...
    if parts.path.startswith("/l/") and (not host or host.endswith("duckduckgo.com")):
        target = urllib.parse.parse_qs(parts.query).get("uddg")
        if target:
            return target[0]
    return href


def parse_ddg_html(markup: str) -> List[Dict[str, str]]:
    """Pure: [{"title", "url", "snippet"}] from a DuckDuckGo HTML results page."""
    parser = _DDGResults()
    parser.feed(markup)
    parser.close()
    return parser.rows


def web_search(query: str, max_results: int = MAX_RESULTS) -> Dict[str, Any]:
    """DuckDuckGo search over the no-JS HTML endpoint.

    Results are cached in memory, keyed by ``(query.strip(), limit)`` where
    ``limit`` is the normalized ``max_results`` (``max(1, int(...))``, default
    ``MAX_RESULTS`` on bad input). A repeated query inside the TTL returns the
    stored result with no network call.

    Cache policy: entries expire after ``_WEB_SEARCH_CACHE_TTL`` seconds
    (3600s) — invalidation is TTL-only, there is no manual invalidation in the
    tool path (``clear_web_search_cache()`` exists for tests). At most
    ``_WEB_SEARCH_CACHE_MAX`` (128) entries are kept; when full, the
    oldest-inserted entry is evicted first (FIFO, not LRU). Only successful
    searches are cached — ``{"error": ...}`` results are never stored, so a
    transient failure cannot mask recovery. Hits and stored values are deep
    copies, so a caller mutating a returned dict cannot corrupt the cache, and
    a cache hit is deep-equal to a fresh search.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "empty query"}
    try:
        limit = max(1, int(max_results))
    except (TypeError, ValueError):
        limit = MAX_RESULTS
    return _web_search_cached(q, limit)


def _web_search_cached(q: str, limit: int) -> Dict[str, Any]:
    """TTL-checked cache lookup around the real fetch; stores only successes."""
    key = (q, limit)
    now = time.monotonic()
    with _web_search_cache_lock:
        entry = _web_search_cache.get(key)
        if entry is not None:
            ts, stored = entry
            if now - ts <= _WEB_SEARCH_CACHE_TTL:
                return copy.deepcopy(stored)
            del _web_search_cache[key]  # expired: drop, re-fetch below
    result = _web_search_fetch(q, limit)
    if "error" not in result:
        with _web_search_cache_lock:
            if key not in _web_search_cache and len(_web_search_cache) >= _WEB_SEARCH_CACHE_MAX:
                oldest = next(iter(_web_search_cache))
                del _web_search_cache[oldest]
            _web_search_cache[key] = (time.monotonic(), copy.deepcopy(result))
    return result


def _web_search_fetch(q: str, limit: int) -> Dict[str, Any]:
    """The actual DuckDuckGo round-trip: POST the query, parse the SERP. No cache."""
    try:
        body, _content_type, charset = _http(
            SEARCH_ENDPOINT, urllib.parse.urlencode({"q": q}).encode("utf-8")
        )
    except WebError as e:
        return {"error": str(e)}
    rows = parse_ddg_html(_decode(body, charset))[:limit]
    out: Dict[str, Any] = {"query": q, "results": rows}
    if not rows:
        # Layout change or a block page: say so rather than imply the search ran clean.
        out["note"] = "no result blocks parsed from the DuckDuckGo response"
    return out


def clear_web_search_cache() -> int:
    """Drop every cached web_search result; returns the number of entries dropped.

    Test/eval helper only — production invalidation is TTL-based (see
    :func:`web_search`).
    """
    with _web_search_cache_lock:
        n = len(_web_search_cache)
        _web_search_cache.clear()
        return n


def web_extract(url: str) -> Dict[str, Any]:
    """Fetch one URL: HTML is reduced to visible text, text/json returned as-is."""
    try:
        body, content_type, charset = _http(url)
    except WebError as e:
        return {"error": str(e)}
    text = _decode(body, charset)
    if content_type in ("text/html", "application/xhtml+xml"):
        visible, title = html_to_text(text)
        content, truncated = _cap(visible)
    elif content_type == "application/json" or content_type.startswith("text/"):
        title = ""
        content, truncated = _cap(text)
    else:
        return {"error": f"unsupported content-type {content_type!r} for {url}"}
    out: Dict[str, Any] = {
        "url": url,
        "title": title,
        "content": content,
        "content_type": content_type,
    }
    if truncated:
        out["truncated"] = True
        out["note"] = f"content truncated to {MAX_CHARS} characters"
    return out


def web_extract_many(urls: List[str], max_workers: int = 5) -> Dict[str, Any]:
    """Fetch many URLs concurrently: ``{"results": {url: web_extract(url)}}``.

    Each URL is extracted independently in a thread pool (network-bound work,
    stdlib ``concurrent.futures`` only). A failure for one URL lands as
    ``{"error": ...}`` in that URL's slot and never affects the others.
    Duplicate URLs are fetched once. The results dict is keyed by URL in
    first-seen order. ``web_extract`` itself is untouched — this is the batch
    counterpart for research-style flows that used to call it in a loop.
    """
    unique: List[str] = []
    seen = set()
    for u in urls or []:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    results: Dict[str, Dict[str, Any]] = {}
    if not unique:
        return {"results": results}
    try:
        workers = int(max_workers)
    except (TypeError, ValueError):
        workers = 5
    workers = max(1, min(workers, len(unique)))

    def _one(url: str) -> Dict[str, Any]:
        try:
            return web_extract(url)
        except Exception as e:  # web_extract never raises by contract; stay never-raise under threads
            return {"error": f"unexpected extraction failure for {url}: {type(e).__name__}: {e}"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        # pool.map preserves input order, so results follow `unique` order.
        for url, res in zip(unique, pool.map(_one, unique)):
            results[url] = res
    return {"results": results}


class WebTools:
    """Backwards-compatible façade over the module-level functions."""

    @staticmethod
    def search(query: str, limit: int = MAX_RESULTS) -> Dict[str, Any]:
        return web_search(query, limit)

    @staticmethod
    def extract(url: str) -> Dict[str, Any]:
        return web_extract(url)

    @staticmethod
    def extract_many(urls: List[str], max_workers: int = 5) -> Dict[str, Any]:
        return web_extract_many(urls, max_workers)


def build_tools(env: Any = None) -> Dict[str, Dict[str, Any]]:
    """Registry-ready tool dicts for AgentEnvironment._tools.

    ``env`` is accepted for the registration call site; these tools hold no per-env state.
    """
    return {
        "web_search": {
            "name": "web_search",
            "func": web_search,
            "description": (
                "Search the web (DuckDuckGo). Returns {results: [{title, url, snippet}]} "
                "or {error} when the search itself failed."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "max_results": {
                        "type": "integer",
                        "description": f"Maximum results to return. Default {MAX_RESULTS}.",
                    },
                },
                "required": ["query"],
            },
        },
        "web_extract": {
            "name": "web_extract",
            "func": web_extract,
            "description": (
                "Fetch one http(s) URL and return its readable content: HTML pages come back "
                "as visible text, text and JSON bodies verbatim (capped, flagging truncation)."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http(s) URL to fetch."},
                },
                "required": ["url"],
            },
        },
        "web_extract_many": {
            "name": "web_extract_many",
            "func": web_extract_many,
            "description": (
                "Fetch many http(s) URLs in parallel; returns "
                "{results: {url: extract-result}}. Prefer this over calling "
                "web_extract once per URL when you need several pages (much "
                "faster). A URL that fails only errors in its own slot."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "http(s) URLs to fetch in parallel.",
                    },
                    "max_workers": {
                        "type": "integer",
                        "description": "Parallel fetch threads. Default 5.",
                    },
                },
                "required": ["urls"],
            },
        },
    }
