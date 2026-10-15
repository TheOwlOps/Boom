"""web_fetch / web_extract / web_extract_many.

Gộp điểm mạnh của 2 bản cũ:
  - từ ``web.py``      : httpx + retry/backoff + circuit breaker + body cap
                         500 KiB + stream (không đọc ồ ạt vào RAM).
  - từ ``web_tools.py``: web_extract_many — fetch song song nhiều URL bằng
                         thread pool (stdlib concurrent.futures).

SSRF: mọi URL vào đều qua ``validate_url`` trước khi request.
"""

import concurrent.futures
import html
import re
import threading
import time
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

import httpx
from pydantic import BaseModel, Field

from .ssrf import validate_url

__all__ = [
    "WebFetchError",
    "CircuitOpenError",
    "FetchResult",
    "web_fetch",
    "web_extract",
    "web_extract_many",
]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 Boom/1.0"
)
_MAX_DOWNLOAD = 500 * 1024          # 500 KiB cap cho body (kế thừa web.py)
_MAX_RETRIES = 2
_BACKOFF = (1.0, 2.0)
_STREAM_CHUNK = 64 * 1024
_sleep = time.sleep                 # test seam
_RETRYABLE = (httpx.TransportError,)


class WebFetchError(Exception):
    """Request thất bại sau khi đã retry hết (hoặc HTTP lỗi không retry)."""


class CircuitOpenError(Exception):
    """Circuit đang mở: host đã chết nhiều lần liên tiếp, fail-fast."""


class FetchResult(BaseModel):
    """Kết quả chuẩn của web_fetch (pydantic model để agent/tool parse chắc)."""

    url: str = Field(description="URL cuối cùng sau redirect")
    status: int = Field(description="HTTP status code")
    content_type: str = Field(description="Content-Type header")
    text: str = Field(default="", description="Body đã decode (chỉ với text/*)")
    title: str = Field(default="", description="Nội dung thẻ <title>")
    truncated: bool = Field(default=False, description="True nếu body bị cắt ở 500 KiB")


# --- circuit breaker gọn (stdlib, thay cho lifecycle.circuit của repo) -----
# Mục đích: host nào chết 5 lần liên tiếp thì 60s tiếp theo fail-fast, không
# phí retry vào host đã biết chết. Đơn giản hơn bản repo nhưng đủ dùng.
class _CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._lock = threading.Lock()
        self._failures: Dict[str, Tuple[int, float]] = {}  # host -> (count, last_ts)

    def call(self, host: str, fn):
        now = time.monotonic()
        with self._lock:
            count, last = self._failures.get(host, (0, 0.0))
            if count >= self.failure_threshold and now - last < self.recovery_timeout:
                raise CircuitOpenError(
                    f"circuit đang mở cho host {host} (fail-fast, thử lại sau)"
                )
        try:
            return fn()
        except Exception:
            with self._lock:
                count, _ = self._failures.get(host, (0, 0.0))
                self._failures[host] = (count + 1, time.monotonic())
            raise
        else:
            with self._lock:
                self._failures.pop(host, None)


_BREAKER = _CircuitBreaker()


def _new_client(timeout: float) -> httpx.Client:
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
    last_exc: Optional[BaseException] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return fn()
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                _sleep(_BACKOFF[attempt])
    raise WebFetchError(f"{what} thất bại sau {_MAX_RETRIES + 1} lần thử: {last_exc}")


def _decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    enc = (m.group(1) if m else "utf-8").lower()
    return body.decode(enc, errors="replace")


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _extract_title(markup: str) -> str:
    m = _TITLE_RE.search(markup or "")
    if not m:
        return ""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub("", m.group(1)))).strip()


def web_fetch(url: str, timeout: int = 15) -> FetchResult:
    """Fetch 1 URL: follow redirect, body cap 500 KiB, retry + circuit breaker.

    Trả về FetchResult (pydantic). Raise:
      - ValueError/SSRFError: URL không hợp lệ hoặc bị chặn (chống SSRF).
      - WebFetchError: network lỗi sau retry. CircuitOpenError: host đang chết.
    """
    target = validate_url(url)  # chặn SSRF trước mọi request

    def _one() -> Tuple[str, int, str, bytes, bool]:
        with _new_client(float(timeout)) as client:
            with client.stream("GET", target) as resp:
                status = resp.status_code
                content_type = resp.headers.get("content-type", "")
                body = bytearray()
                truncated = False
                for chunk in resp.iter_bytes(chunk_size=_STREAM_CHUNK):
                    room = _MAX_DOWNLOAD - len(body)
                    if len(chunk) >= room:
                        body += chunk[:room]
                        truncated = True
                        break
                    body += chunk
                return str(resp.url), status, content_type, bytes(body), truncated

    host = httpx.URL(target).host or ""
    final_url, status, content_type, body, truncated = _BREAKER.call(
        host, lambda: _with_retry(_one, f"GET {target}")
    )

    text = ""
    if content_type.startswith("text/") or "html" in content_type:
        text = _decode(body, content_type)
    return FetchResult(
        url=final_url,
        status=status,
        content_type=content_type,
        text=text,
        title=_extract_title(text) if text else "",
        truncated=truncated,
    )


# --- extract visible text (+ selector đơn giản) -----------------------------
_SKIP_TAGS = {"script", "style", "noscript", "template", "nav", "footer"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
    "header", "aside", "blockquote", "pre", "form", "hr",
}


class _TextExtractor(HTMLParser):
    """Text hiển thị: bỏ script/style/nav/footer, giữ cấu trúc block."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        lines = (re.sub(r"[ \t]+", " ", ln).strip()
                 for ln in "".join(self._parts).split("\n"))
        return "\n".join(ln for ln in lines if ln)


_SELECTOR_RE = re.compile(
    r"^(?:(?P<tag>[a-zA-Z][a-zA-Z0-9]*)?)?"
    r"(?:#(?P<id>[a-zA-Z0-9_-]+))?"
    r"(?:\.(?P<class>[a-zA-Z0-9_-]+))?$"
)


class _SectionExtractor(HTMLParser):
    """Lấy text của element ĐẦU TIÊN khớp selector đơn giản."""

    def __init__(self, tag: Optional[str], id_: Optional[str], class_: Optional[str]):
        super().__init__(convert_charrefs=True)
        self._want = (tag.lower() if tag else None, id_, class_)
        self._depth = 0
        self._found = False
        self._skip_depth = 0
        self._parts: List[str] = []

    def _matches(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> bool:
        want_tag, want_id, want_class = self._want
        ad = {k.lower(): (v or "") for k, v in attrs}
        if want_tag and tag != want_tag:
            return False
        if want_id and ad.get("id") != want_id:
            return False
        if want_class and want_class not in ad.get("class", "").split():
            return False
        return True

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if not self._found and self._depth == 0 and self._matches(tag, attrs):
            self._found = True
            self._depth = 1
            return
        if self._found:
            self._depth += 1
            if tag in _SKIP_TAGS:
                self._skip_depth += 1
            elif tag in _BLOCK_TAGS:
                self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self._found:
            return
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")
        self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._found and self._skip_depth == 0 and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        lines = (re.sub(r"[ \t]+", " ", ln).strip()
                 for ln in "".join(self._parts).split("\n"))
        return "\n".join(ln for ln in lines if ln)


def web_extract(url: str, selector: Optional[str] = None) -> Dict[str, str]:
    """Fetch URL rồi trả về text hiển thị chính (bỏ script/style/nav/footer).

    selector: CSS đơn giản ("article", ".content", "#main", "div.post") để chỉ
    lấy 1 section. Selector không khớp gì -> raise ValueError (không trả rỗng
    im lặng).
    """
    fetched = web_fetch(url)  # validate_url + SSRF đã nằm trong web_fetch
    markup = fetched.text
    if selector is None:
        extractor = _TextExtractor()
    else:
        m = _SELECTOR_RE.match((selector or "").strip())
        if not m or not any(m.groupdict().values()):
            raise ValueError(
                f"selector không hỗ trợ {selector!r}: dùng tag, .class, #id, tag.class"
            )
        g = m.groupdict()
        extractor = _SectionExtractor(g["tag"], g["id"], g["class"])
    extractor.feed(markup)
    extractor.close()
    text = extractor.text()
    if selector is not None and not text:
        raise ValueError(f"selector {selector!r} không khớp gì tại {url}")
    return {"url": fetched.url, "text": text}


def web_extract_many(urls: List[str], max_workers: int = 5) -> Dict[str, Dict[str, Any]]:
    """Fetch song song nhiều URL (kế thừa từ web_tools.py).

    Trả về ``{"results": {url: {"url", "text"} hoặc {"error": ...}}}``.
    1 URL lỗi chỉ lỗi ở slot của nó, không ảnh hưởng URL khác. URL trùng
    chỉ fetch 1 lần. Lỗi SSRF/validate cũng nằm gọn trong slot {"error"}.
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
        workers = max(1, min(int(max_workers), len(unique)))
    except (TypeError, ValueError):
        workers = min(5, len(unique))

    def _one(u: str) -> Dict[str, Any]:
        try:
            return web_extract(u)
        except Exception as exc:  # never-raise dưới thread pool
            return {"error": f"{type(exc).__name__}: {exc}"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for u, res in zip(unique, pool.map(_one, unique)):  # map giữ đúng thứ tự
            results[u] = res
    return {"results": results}
