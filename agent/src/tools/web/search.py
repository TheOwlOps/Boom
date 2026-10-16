"""web_search: web search via DuckDuckGo (no API key needed).

Merges the strengths of the 2 old implementations:
  - from ``web.py``      : httpx + retry with backoff (1s, 2s) on transport errors.
  - from ``web_tools.py``: in-RAM search result cache (TTL 3600s,
                         max 128 entries, FIFO evict, thread-safe,
                         only successful results cached, deep-copy on return).

SSRF: result URLs in the SERP aren't fetched here, but the query POST to
DuckDuckGo still goes through validate_url for policy consistency.
"""

import copy
import re
import threading
import time
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .ssrf import SSRFError, validate_url

__all__ = [
    "WebSearchError",
    "web_search",
    "clear_web_search_cache",
    "parse_ddg_html",
]

DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 Boom/1.0"
)

# --- retry (inherited from web.py) -----------------------------------------
_MAX_RETRIES = 2                    # retries after the first attempt
_BACKOFF = (1.0, 2.0)               # seconds to sleep before retry 1 and retry 2
_sleep = time.sleep                 # test seam: tests can swap in a no-op
_RETRYABLE = (httpx.TransportError,)


class WebSearchError(Exception):
    """Search failed after all retries."""


def _new_client(timeout: float) -> httpx.Client:
    """1 httpx client per request: follows redirects, browser-like headers."""
    # trust_env=False: ignores proxy env (avoids no_proxy IPv6 parse errors
    # in sandboxes; SSRF is already validated at the URL layer).
    return httpx.Client(
        trust_env=False,
        timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


def _with_retry(fn, what: str):
    """Runs fn(); retries transport errors, raises WebSearchError when exhausted."""
    last_exc: Optional[BaseException] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return fn()
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                _sleep(_BACKOFF[attempt])
    raise WebSearchError(f"{what} failed after {_MAX_RETRIES + 1} attempts: {last_exc}")


# --- DuckDuckGo SERP parsing (stdlib html.parser, inherited from both) ----
def _clean_ddg_url(href: str) -> str:
    """Unwraps DuckDuckGo's /l/?uddg=... redirect to get the real URL."""
    href = (href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    parts = urllib.parse.urlsplit(href)
    if parts.path.startswith("/l/") and (
        not parts.netloc or parts.netloc.endswith("duckduckgo.com")
    ):
        target = urllib.parse.parse_qs(parts.query).get("uddg")
        if target:
            return target[0]
    return href


class _DDGResults(HTMLParser):
    """Collects {title, url, snippet} from DuckDuckGo's no-JS results page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[Dict[str, str]] = []
        self._field = ""
        self._close_tag = ""
        self._buf: List[str] = []
        self._href = ""

    @staticmethod
    def _classes(attrs: List[Tuple[str, Optional[str]]]) -> List[str]:
        for key, value in attrs:
            if key == "class":
                return (value or "").split()
        return []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        classes = self._classes(attrs)
        if self._field or ("result__a" not in classes and "result__snippet" not in classes):
            return
        self._field = "title" if "result__a" in classes else "snippet"
        self._close_tag = tag
        self._buf = []
        self._href = dict(attrs).get("href") or ""

    def handle_data(self, data: str) -> None:
        if self._field:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._field or tag != self._close_tag:
            return
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if self._field == "title":
            url = _clean_ddg_url(self._href)
            if url:
                self.rows.append({"title": text, "url": url, "snippet": ""})
        elif self.rows and text and not self.rows[-1]["snippet"]:
            self.rows[-1]["snippet"] = text
        self._field = ""
        self._close_tag = ""
        self._buf = []
        self._href = ""


def parse_ddg_html(markup: str) -> List[Dict[str, str]]:
    """Pure: [{title, url, snippet}] from DuckDuckGo results page HTML."""
    parser = _DDGResults()
    parser.feed(markup or "")
    parser.close()
    return parser.rows


# --- search cache (inherited from web_tools.py) ------------------------------
# In-RAM cache: key=(stripped query, limit), value=(monotonic ts, deep copy
# of the results). TTL 3600s, max 128 entries, FIFO evict (earliest-inserted
# entry removed first). Only successful results are cached — errors are never
# cached, so a transient failure can't shadow a retry. Returns a deep copy so
# callers mutating the result dicts can't corrupt the cache.
_SEARCH_CACHE_TTL = 3600.0
_SEARCH_CACHE_MAX = 128
_search_cache: Dict[Tuple[str, int], Tuple[float, Any]] = {}
_search_cache_lock = threading.Lock()


def clear_web_search_cache() -> int:
    """Clears the whole search cache; returns entries removed (for test/eval)."""
    with _search_cache_lock:
        n = len(_search_cache)
        _search_cache.clear()
        return n


def _decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    enc = (m.group(1) if m else "utf-8").lower()
    return body.decode(enc, errors="replace")


def _fetch_search(q: str, limit: int, timeout: float) -> List[Dict[str, str]]:
    """1 real search round: POSTs the query to DuckDuckGo, parses the SERP. No cache."""
    validate_url(DDG_ENDPOINT)  # uniform SSRF policy for every outbound request

    def _one() -> httpx.Response:
        with _new_client(timeout) as client:
            return client.post(
                DDG_ENDPOINT,
                data={"q": q},
                headers={"Referer": "https://duckduckgo.com/"},
            )

    resp = _with_retry(_one, f"web_search({q!r})")
    if resp.status_code >= 400:
        raise WebSearchError(f"DuckDuckGo returned HTTP {resp.status_code}")
    markup = _decode(resp.content, resp.headers.get("content-type", ""))
    return parse_ddg_html(markup)[:limit]


def web_search(query: str, max_results: int = 5, timeout: int = 15) -> List[Dict[str, str]]:
    """Searches the web via DuckDuckGo (no-JS endpoint, no API key).

    Returns ``[{title, url, snippet}]`` (up to max_results). Successful
    results are cached for 1 hour by (query, limit) — repeating an old query
    within the TTL costs no network. Raises ValueError on empty query,
    WebSearchError when the SERP can't be fetched after retries.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("query must be a non-empty string")
    try:
        limit = max(1, int(max_results))
    except (TypeError, ValueError):
        limit = 5

    key = (q, limit)
    now = time.monotonic()
    with _search_cache_lock:
        entry = _search_cache.get(key)
        if entry is not None:
            ts, stored = entry
            if now - ts <= _SEARCH_CACHE_TTL:
                return copy.deepcopy(stored)  # cache hit: no network
            del _search_cache[key]  # expired: drop, refetch below

    rows = _fetch_search(q, limit, float(timeout))

    with _search_cache_lock:
        if key not in _search_cache and len(_search_cache) >= _SEARCH_CACHE_MAX:
            oldest = next(iter(_search_cache))
            del _search_cache[oldest]
        _search_cache[key] = (time.monotonic(), copy.deepcopy(rows))
    return rows
