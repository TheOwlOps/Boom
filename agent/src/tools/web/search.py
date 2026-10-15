"""web_search: tìm kiếm web qua DuckDuckGo (không cần API key).

Gộp điểm mạnh của 2 bản cũ:
  - từ ``web.py``      : httpx + retry với backoff (1s, 2s) cho lỗi transport.
  - từ ``web_tools.py``: cache kết quả search trong RAM (TTL 3600s,
                         tối đa 128 entries, FIFO evict, thread-safe,
                         chỉ cache kết quả thành công, deep-copy khi trả về).

SSRF: URL kết quả trong SERP không bị fetch ở đây, nhưng query POST tới
DuckDuckGo vẫn đi qua validate_url để đồng nhất chính sách.
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

# --- retry (kế thừa từ web.py) -------------------------------------------
_MAX_RETRIES = 2                    # số lần retry sau attempt đầu
_BACKOFF = (1.0, 2.0)               # giây nghỉ trước retry 1 và retry 2
_sleep = time.sleep                 # test seam: test có thể thay bằng no-op
_RETRYABLE = (httpx.TransportError,)


class WebSearchError(Exception):
    """Search thất bại sau khi đã retry hết."""


def _new_client(timeout: float) -> httpx.Client:
    """1 httpx client cho mỗi request: follow redirect, header giống browser."""
    # trust_env=False: bỏ qua proxy env (tránh lỗi parse no_proxy
    # IPv6 trong sandbox; SSRF đã validate ở tầng URL).
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
    """Chạy fn(); retry lỗi transport, hết lượt thì raise WebSearchError."""
    last_exc: Optional[BaseException] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return fn()
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                _sleep(_BACKOFF[attempt])
    raise WebSearchError(f"{what} thất bại sau {_MAX_RETRIES + 1} lần thử: {last_exc}")


# --- parse SERP DuckDuckGo (stdlib html.parser, kế thừa từ cả 2 bản) -----
def _clean_ddg_url(href: str) -> str:
    """Gỡ redirect /l/?uddg=... của DuckDuckGo để lấy URL thật."""
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
    """Thu thập {title, url, snippet} từ trang kết quả DuckDuckGo no-JS."""

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
    """Pure: [{title, url, snippet}] từ HTML trang kết quả DuckDuckGo."""
    parser = _DDGResults()
    parser.feed(markup or "")
    parser.close()
    return parser.rows


# --- cache search (kế thừa từ web_tools.py) --------------------------------
# Cache trong RAM: key=(query đã strip, limit), value=(monotonic ts, deep copy
# kết quả). TTL 3600s, tối đa 128 entries, evict FIFO (entry chèn sớm nhất
# bị xóa trước). Chỉ cache kết quả thành công — lỗi không bao giờ cache để
# lỗi thoáng qua không che mất lần thử lại. Trả về deep copy nên caller sửa
# dict kết quả cũng không làm hỏng cache.
_SEARCH_CACHE_TTL = 3600.0
_SEARCH_CACHE_MAX = 128
_search_cache: Dict[Tuple[str, int], Tuple[float, Any]] = {}
_search_cache_lock = threading.Lock()


def clear_web_search_cache() -> int:
    """Xóa toàn bộ cache search; trả về số entry đã xóa (cho test/eval)."""
    with _search_cache_lock:
        n = len(_search_cache)
        _search_cache.clear()
        return n


def _decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    enc = (m.group(1) if m else "utf-8").lower()
    return body.decode(enc, errors="replace")


def _fetch_search(q: str, limit: int, timeout: float) -> List[Dict[str, str]]:
    """1 vòng search thật: POST query lên DuckDuckGo, parse SERP. Không cache."""
    validate_url(DDG_ENDPOINT)  # đồng nhất chính sách SSRF cho mọi request ra ngoài

    def _one() -> httpx.Response:
        with _new_client(timeout) as client:
            return client.post(
                DDG_ENDPOINT,
                data={"q": q},
                headers={"Referer": "https://duckduckgo.com/"},
            )

    resp = _with_retry(_one, f"web_search({q!r})")
    if resp.status_code >= 400:
        raise WebSearchError(f"DuckDuckGo trả về HTTP {resp.status_code}")
    markup = _decode(resp.content, resp.headers.get("content-type", ""))
    return parse_ddg_html(markup)[:limit]


def web_search(query: str, max_results: int = 5, timeout: int = 15) -> List[Dict[str, str]]:
    """Tìm kiếm web qua DuckDuckGo (no-JS endpoint, không cần API key).

    Trả về ``[{title, url, snippet}]`` (tối đa max_results). Kết quả thành
    công được cache 1 giờ theo (query, limit) — gọi lại query cũ trong TTL
    không tốn network. Raise ValueError nếu query rỗng, WebSearchError khi
    SERP không lấy được sau retry.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("query phải là string không rỗng")
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
                return copy.deepcopy(stored)  # cache hit: không network
            del _search_cache[key]  # hết hạn: xóa, fetch lại bên dưới

    rows = _fetch_search(q, limit, float(timeout))

    with _search_cache_lock:
        if key not in _search_cache and len(_search_cache) >= _SEARCH_CACHE_MAX:
            oldest = next(iter(_search_cache))
            del _search_cache[oldest]
        _search_cache[key] = (time.monotonic(), copy.deepcopy(rows))
    return rows
