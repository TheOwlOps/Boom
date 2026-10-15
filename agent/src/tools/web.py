"""Unified web primitives for Boom: search, fetch, extract.

Only third-party dependency is ``httpx``; all parsing is stdlib
(``html.parser``, ``re``, ``urllib``). No API key needed for search.

Public API:
  - ``web_search(query, max_results=5)`` -> ``[{title, url, snippet}]``
  - ``web_fetch(url, timeout=15)`` -> ``{url, status, content_type, text, title, truncated}``
  - ``web_extract(url, selector=None)`` -> ``{url, text}``

Resilience:
  - Network failures raise ``WebFetchError`` after 1 attempt + 2 retries,
    with backoff sleeps of 1s then 2s (``_BACKOFF``; test seam: ``_sleep``).
  - ``web_fetch`` runs inside the repo's ``CircuitBreaker``
    (``lifecycle.circuit``) when that import succeeds; when it fails
    (e.g. standalone use), the module still works with plain retry.
  - A proper browser-like User-Agent header on every request.
  - Downloaded bodies are capped at 500 KiB (``_MAX_DOWNLOAD``); anything
    beyond is cut and reported via ``truncated: True``.

Error contract: bad input raises ``ValueError``; network/service failures
raise ``WebFetchError`` (or ``CircuitOpenError`` from the breaker when the
remote host is already known-dead — the breaker re-raises the original
error, it never invents one).
"""

import html
import re
import time
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

import httpx

# ---------------------------------------------------------------------------
# Optional circuit breaker (repo: agent/src/lifecycle/circuit.py)
try:
    from lifecycle.circuit import CircuitBreaker, CircuitOpenError  # type: ignore
except ImportError:  # standalone use outside the repo: degrade to plain retry
    CircuitBreaker = None  # type: ignore[assignment,misc]

    class CircuitOpenError(Exception):  # type: ignore[no-redef]
        """Raised when the (unavailable) breaker would have failed fast."""


__all__ = [
    "WebFetchError",
    "CircuitOpenError",
    "web_search",
    "web_fetch",
    "web_extract",
    "build_tools",
]

DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 BoomBot/1.0"
)
_MAX_DOWNLOAD = 500 * 1024          # 500 KiB body cap for web_fetch
_MAX_RETRIES = 2                    # retries after the first attempt
_BACKOFF = (1.0, 2.0)               # seconds slept before retry 1 and retry 2
_STREAM_CHUNK = 64 * 1024

# Test seams: replaced by tests, never by production code.
_sleep = time.sleep

# Exceptions worth retrying: transport-level httpx failures.
_RETRYABLE = (httpx.TransportError,)


class WebFetchError(Exception):
    """A web request failed after all retries (or a non-retryable HTTP state)."""


def _new_client(timeout: float) -> httpx.Client:
    """One httpx client per request: follows redirects, sane headers/timeout."""
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


def _with_retry(fn, what: str):
    """Run ``fn`` (a zero-arg callable doing one attempt); retry network errors.

    Sleeps ``_BACKOFF[0]`` then ``_BACKOFF[1]`` between the retries. Raises
    ``WebFetchError`` naming ``what`` once the attempts are exhausted.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return fn()
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                _sleep(_BACKOFF[attempt])
    raise WebFetchError(f"{what} failed after {_MAX_RETRIES + 1} attempts: {last_exc}")


def _charset(content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    return (m.group(1) if m else "utf-8").lower()


def _decode(body: bytes, content_type: str) -> str:
    return body.decode(_charset(content_type), errors="replace")


# ---------------------------------------------------------------------------
# DuckDuckGo HTML search
def _clean_ddg_url(href: str) -> str:
    """Resolve a SERP href, unwrapping DuckDuckGo's ``/l/?uddg=`` redirect."""
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
    """Collect {title, url, snippet} rows from a DuckDuckGo no-JS SERP."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[Dict[str, str]] = []
        self._field = ""       # "title" | "snippet" while inside a result element
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


def _parse_ddg_html(markup: str) -> List[Dict[str, str]]:
    parser = _DDGResults()
    parser.feed(markup)
    parser.close()
    return parser.rows


def web_search(
    query: str, max_results: int = 5, timeout: int = 15
) -> List[Dict[str, str]]:
    """Search the web via DuckDuckGo's no-JS HTML endpoint (no API key).

    Returns ``[{title, url, snippet}]`` (at most ``max_results``).
    Raises ``ValueError`` for an empty query and ``WebFetchError`` when the
    SERP cannot be retrieved after retries.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("query must be a non-empty string")
    try:
        limit = max(1, int(max_results))
    except (TypeError, ValueError):
        limit = 5

    def _one() -> httpx.Response:
        with _new_client(float(timeout)) as client:
            return client.post(
                DDG_ENDPOINT, data={"q": q},
                headers={"Referer": "https://duckduckgo.com/"},
            )

    resp = _with_retry(_one, f"web_search({q!r})")
    if resp.status_code >= 400:
        raise WebFetchError(
            f"web_search({q!r}): DuckDuckGo returned HTTP {resp.status_code}"
        )
    markup = _decode(resp.content, resp.headers.get("content-type", ""))
    return _parse_ddg_html(markup)[:limit]


# ---------------------------------------------------------------------------
# Fetch (with circuit breaker + 500 KiB cap)
def _breaker() -> Optional[Any]:
    """Module-level breaker for outbound fetches, or None when unavailable."""
    if CircuitBreaker is None:
        return None
    return CircuitBreaker(
        name="web",
        failure_threshold=5,
        recovery_timeout=60.0,
        expected_exception=(httpx.HTTPError, WebFetchError),
    )


_BREAKER = _breaker()


def _stream_fetch(url: str, timeout: float) -> Tuple[str, int, str, bytes, bool]:
    """One GET attempt, streaming the body up to ``_MAX_DOWNLOAD`` bytes.

    Returns (final_url, status, content_type, body, truncated).
    Raises httpx.TransportError subclasses on network failure (retryable).
    """

    def _one() -> Tuple[str, int, str, bytes, bool]:
        with _new_client(timeout) as client:
            with client.stream("GET", url) as resp:
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

    return _with_retry(_one, f"GET {url}")


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _extract_title(markup: str) -> str:
    m = _TITLE_RE.search(markup)
    if not m:
        return ""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub("", m.group(1)))).strip()


def web_fetch(url: str, timeout: int = 15) -> Dict[str, Any]:
    """Fetch a URL: follows redirects, caps the body at 500 KiB.

    Returns ``{url, status, content_type, text, title, truncated}`` where
    ``url`` is the final URL after redirects, ``text`` is the decoded body
    (only for ``text/*`` content; ``""`` otherwise), ``title`` comes from
    the ``<title>`` tag, and ``truncated`` is True when the 500 KiB cap cut
    the body. Wrapped in the repo's ``CircuitBreaker`` when available:
    repeated failures open the circuit and later calls raise
    ``CircuitOpenError`` fail-fast instead of hitting a dead host.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    target = url.strip()
    if _BREAKER is not None:
        final_url, status, content_type, body, truncated = _BREAKER.call(
            _stream_fetch, target, float(timeout)
        )
    else:
        final_url, status, content_type, body, truncated = _stream_fetch(
            target, float(timeout)
        )

    text = ""
    if content_type.startswith("text/") or "html" in content_type:
        text = _decode(body, content_type)
    return {
        "url": final_url,
        "status": status,
        "content_type": content_type,
        "text": text,
        "title": _extract_title(text) if text else "",
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Extract visible text (+ simple CSS section selector)
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
            return
        if tag in _BLOCK_TAGS:
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
        lines = (re.sub(r"[ \t]+", " ", ln).strip() for ln in "".join(self._parts).split("\n"))
        return "\n".join(ln for ln in lines if ln)


_SELECTOR_RE = re.compile(
    r"^(?:(?P<tag>[a-zA-Z][a-zA-Z0-9]*)?)?"
    r"(?:#(?P<id>[a-zA-Z0-9_-]+))?"
    r"(?:\.(?P<class>[a-zA-Z0-9_-]+))?$"
)


def _parse_selector(selector: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Parse a simple selector (tag / .class / #id / tag.class / tag#id)."""
    m = _SELECTOR_RE.match(selector.strip())
    if not m:
        raise ValueError(
            f"unsupported selector {selector!r}: "
            "use tag, .class, #id, tag.class or tag#id"
        )
    g = m.groupdict()
    if not any(g.values()):
        raise ValueError(f"unsupported selector {selector!r}: selector is empty")
    return g["tag"], g["id"], g["class"]


class _SectionExtractor(HTMLParser):
    """Capture the inner text of the FIRST element matching a simple selector."""

    def __init__(self, tag: Optional[str], id_: Optional[str], class_: Optional[str]) -> None:
        super().__init__(convert_charrefs=True)
        self._want = (tag.lower() if tag else None, id_, class_)
        self._depth = 0            # depth inside the matched element (0 = not inside)
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
        lines = (re.sub(r"[ \t]+", " ", ln).strip() for ln in "".join(self._parts).split("\n"))
        return "\n".join(ln for ln in lines if ln)


def web_extract(url: str, selector: str = None) -> Dict[str, str]:
    """Fetch a URL and return its main visible text.

    Drops ``script``/``style``/``nav``/``footer`` content and collapses
    whitespace. With ``selector`` (simple CSS: ``"article"``, ``".content"``,
    ``"#main"``, ``"div.post"``) only that element's text is returned;
    an unsupported selector raises ``ValueError``, a selector matching
    nothing raises ``ValueError`` too (never a silent empty string).
    Network failures raise ``WebFetchError`` after retries.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    fetched = web_fetch(url)
    markup = fetched["text"]
    if selector is None:
        extractor = _TextExtractor()
        extractor.feed(markup)
        extractor.close()
        return {"url": fetched["url"], "text": extractor.text()}
    tag, id_, class_ = _parse_selector(selector)
    extractor = _SectionExtractor(tag, id_, class_)
    extractor.feed(markup)
    extractor.close()
    text = extractor.text()
    if not text:
        raise ValueError(f"selector {selector!r} matched nothing at {url}")
    return {"url": fetched["url"], "text": text}


# ---------------------------------------------------------------------------
# Tool registry (repo convention: environment.py consumes build_tools())
def build_tools(env: Any = None) -> Dict[str, Dict[str, Any]]:
    """Registry-ready tool dicts for the Boom agent environment."""

    def _schema(properties: Optional[Dict[str, Any]] = None,
                required: Optional[List[str]] = None) -> Dict[str, Any]:
        return {"type": "object", "properties": properties or {},
                "required": required or []}

    def _guard(fn):
        try:
            return fn()
        except Exception as exc:  # tool calls never raise into the agent loop
            return {"error": f"{type(exc).__name__}: {exc}"}

    def t_web_search(query: str, max_results: int = 5) -> Any:
        return _guard(lambda: web_search(query, int(max_results)))

    def t_web_fetch(url: str, timeout: int = 15) -> Any:
        return _guard(lambda: web_fetch(url, int(timeout)))

    def t_web_extract(url: str, selector: str = "") -> Any:
        return _guard(lambda: web_extract(url, selector or None))

    return {
        "web_search": {
            "name": "web_search",
            "func": t_web_search,
            "description": (
                "Search the web via DuckDuckGo (no API key). Returns "
                "[{title, url, snippet}]. Network failures raise inside the "
                "tool and are reported as {\"error\": ...}."
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
                "Fetch a URL (follows redirects, 500 KiB body cap, retries, "
                "circuit breaker). Returns {url, status, content_type, text, "
                "title, truncated}."
            ),
            "schema": _schema(
                {
                    "url": {"type": "string", "description": "URL to fetch."},
                    "timeout": {"type": "integer", "description": "Seconds. Default 15."},
                },
                ["url"],
            ),
        },
        "web_extract": {
            "name": "web_extract",
            "func": t_web_extract,
            "description": (
                "Fetch a URL and return its main visible text "
                "(script/style/nav/footer removed). Optional simple CSS "
                "selector (tag, .class, #id, tag.class) to extract one section."
            ),
            "schema": _schema(
                {
                    "url": {"type": "string", "description": "URL to extract text from."},
                    "selector": {
                        "type": "string",
                        "description": "Optional simple CSS selector, e.g. 'article', '.content', '#main'.",
                    },
                },
                ["url"],
            ),
        },
    }
