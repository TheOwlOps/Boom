"""web_fetch / web_extract / web_extract_many.

Merges the strengths of the 2 old implementations:
  - from ``web.py``      : httpx + retry/backoff + circuit breaker + 500 KiB
                         body cap + streaming (no bulk reads into RAM).
  - from ``web_tools.py``: web_extract_many — parallel multi-URL fetch via a
                         thread pool (stdlib concurrent.futures).

SSRF: every incoming URL goes through ``validate_url`` before requesting.
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
_MAX_DOWNLOAD = 500 * 1024          # 500 KiB body cap (inherited from web.py)
_MAX_RETRIES = 2
_BACKOFF = (1.0, 2.0)
_STREAM_CHUNK = 64 * 1024
_sleep = time.sleep                 # test seam
_RETRYABLE = (httpx.TransportError,)


class WebFetchError(Exception):
    """Request failed after all retries (or a non-retryable HTTP error)."""


class CircuitOpenError(Exception):
    """Circuit is open: the host died many times in a row, fail-fast."""


class FetchResult(BaseModel):
    """Standard web_fetch result (pydantic model so agents/tools parse reliably)."""

    url: str = Field(description="Final URL after redirects")
    status: int = Field(description="HTTP status code")
    content_type: str = Field(description="Content-Type header")
    text: str = Field(default="", description="Decoded body (text/* only)")
    title: str = Field(default="", description="Contents of the <title> tag")
    truncated: bool = Field(default=False, description="True when the body was cut at 500 KiB")


# --- compact circuit breaker (stdlib, replaces the repo's lifecycle.circuit) ---
# Purpose: a host that died 5 times in a row fail-fasts for the next 60s, no
# retries wasted on a known-dead host. Simpler than the repo's version but enough.
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
                    f"circuit open for host {host} (fail-fast, retry later)"
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
    last_exc: Optional[BaseException] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return fn()
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                _sleep(_BACKOFF[attempt])
    raise WebFetchError(f"{what} failed after {_MAX_RETRIES + 1} attempts: {last_exc}")


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
    """Fetches 1 URL: follows redirects, 500 KiB body cap, retry + circuit breaker.

    Returns a FetchResult (pydantic). Raises:
      - ValueError/SSRFError: invalid or blocked URL (SSRF protection).
      - WebFetchError: network failure after retries. CircuitOpenError: dying host.
    """
    target = validate_url(url)  # blocks SSRF before every request

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


# --- visible text extraction (+ simple selectors) ---------------------------
_SKIP_TAGS = {"script", "style", "noscript", "template", "nav", "footer"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
    "header", "aside", "blockquote", "pre", "form", "hr",
}


class _TextExtractor(HTMLParser):
    """Visible text: drops script/style/nav/footer, keeps block structure."""

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
    """Grabs the text of the FIRST element matching a simple selector."""

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
    """Fetches a URL then returns its main visible text (drops script/style/nav/footer).

    selector: a simple CSS selector ("article", ".content", "#main", "div.post")
    to grab just one section. An unmatched selector raises ValueError (never
    silently returns empty).
    """
    fetched = web_fetch(url)  # validate_url + SSRF live inside web_fetch
    markup = fetched.text
    if selector is None:
        extractor = _TextExtractor()
    else:
        m = _SELECTOR_RE.match((selector or "").strip())
        if not m or not any(m.groupdict().values()):
            raise ValueError(
                f"unsupported selector {selector!r}: use tag, .class, #id, tag.class"
            )
        g = m.groupdict()
        extractor = _SectionExtractor(g["tag"], g["id"], g["class"])
    extractor.feed(markup)
    extractor.close()
    text = extractor.text()
    if selector is not None and not text:
        raise ValueError(f"selector {selector!r} matched nothing at {url}")
    return {"url": fetched.url, "text": text}


def web_extract_many(urls: List[str], max_workers: int = 5) -> Dict[str, Dict[str, Any]]:
    """Fetches many URLs in parallel (inherited from web_tools.py).

    Returns ``{"results": {url: {"url", "text"} or {"error": ...}}}``.
    One failing URL only fails its own slot, never the others. Duplicate
    URLs are fetched once. SSRF/validation errors land in the slot's {"error"}.
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
        except Exception as exc:  # never-raise under the thread pool
            return {"error": f"{type(exc).__name__}: {exc}"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for u, res in zip(unique, pool.map(_one, unique)):  # map preserves order
            results[u] = res
    return {"results": results}
