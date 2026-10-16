"""scrape_basic: simple structured scraping (no Chrome needed).

Borrowing the idea from ``scraper.py`` but self-contained (doesn't import
the old tool):
  - fetches static HTML with httpx + retry/backoff (like fetch.py), WITH SSRF validation.
  - mini DOM via stdlib html.parser + simple selectors
    (tag / .class / #id / tag.class / tag#id / "tag@attr").
  - fields: {field_name: selector}; the "@attr" suffix grabs an attribute
    instead of text (e.g. {"img": ".thumb@src"}).

NOT included: multi-page crawl, sitemap, watch_url, browser fallback
(JS-heavy still uses the old browser_tool). scrape_basic never raises:
errors return {"error": ...}, never into the agent loop.
"""

import re
import time
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .ssrf import SSRFError, validate_url

__all__ = ["ScrapeError", "scrape_basic"]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 Boom/1.0"
)
_MAX_DOWNLOAD = 2 * 1024 * 1024     # 2 MiB: scraping needs a fuller DOM than text fetch
_MAX_RETRIES = 2
_BACKOFF = (1.0, 2.0)
_sleep = time.sleep
_RETRYABLE = (httpx.TransportError,)

# Signs of a JS-rendered page (empty body + script bundle / SPA root).
_SPA_MARKERS = re.compile(
    r'<div[^>]+id=["\'](?:root|app|__next)["\']',
    re.I,
)


class ScrapeError(Exception):
    """Scrape error (network / parse / selector)."""


# ---------------------------------------------------------------- mini DOM
class _Node:
    __slots__ = ("tag", "attrs", "children", "text")

    def __init__(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        self.tag = tag.lower()
        self.attrs = {k.lower(): (v or "") for k, v in attrs}
        self.children: List["_Node"] = []
        self.text = ""


class _DOMBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", [])
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        node = _Node(tag, attrs)
        self._stack[-1].children.append(node)
        self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if len(self._stack) > 1:
            self._stack.pop()

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._stack[-1].text += data


def _parse_html(markup: str) -> _Node:
    builder = _DOMBuilder()
    builder.feed(markup or "")
    builder.close()
    return builder.root


_SELECTOR_RE = re.compile(
    r"^(?:(?P<tag>[a-zA-Z][a-zA-Z0-9]*)?)?"
    r"(?:#(?P<id>[a-zA-Z0-9_-]+))?"
    r"(?:\.(?P<class>[a-zA-Z0-9_-]+))?$"
)


def _split_field_selector(field_selector: str) -> Tuple[str, Optional[str]]:
    """Splits "selector@attr" -> (selector, attr|None)."""
    if "@" in field_selector:
        sel, _, attr = field_selector.rpartition("@")
        return sel.strip() or "*", attr.strip() or None
    return field_selector.strip(), None


def _match_simple(node: _Node, part: str) -> bool:
    m = _SELECTOR_RE.match(part.strip())
    if not m or not any(m.groupdict().values()):
        raise ValueError(f"unsupported selector {part!r}")
    g = m.groupdict()
    if g["tag"] and node.tag != g["tag"].lower():
        return False
    if g["id"] and node.attrs.get("id") != g["id"]:
        return False
    if g["class"] and g["class"] not in node.attrs.get("class", "").split():
        return False
    return True


def _select(root: _Node, selector: str) -> List[_Node]:
    """Finds every node matching the selector (whole-tree walk, no descendant support)."""
    sel, _attr = _split_field_selector(selector)
    out: List[_Node] = []

    def _walk(node: _Node) -> None:
        for child in node.children:
            if _match_simple(child, sel):
                out.append(child)
            _walk(child)

    _walk(root)
    return out


def _inner_text(node: _Node) -> str:
    parts = [node.text]

    def _walk(n: _Node) -> None:
        for c in n.children:
            parts.append(c.text)
            _walk(c)

    _walk(node)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


# ------------------------------------------------------------------ fetch
def _fetch_html(url: str, timeout: float = 20.0) -> str:
    """Fetches static HTML: SSRF validation + retry/backoff + 2 MiB cap."""
    target = validate_url(url)

    def _one() -> str:
        with httpx.Client(
            trust_env=False,  # like search.py/fetch.py
            timeout=httpx.Timeout(timeout, connect=5.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT,
                     "Accept": "text/html,application/xhtml+xml"},
        ) as client:
            with client.stream("GET", target) as resp:
                if resp.status_code == 429:
                    raise ScrapeError("rate limited (HTTP 429)")
                if resp.status_code >= 400:
                    raise ScrapeError(f"HTTP {resp.status_code} for {target}")
                body = bytearray()
                for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                    if len(body) + len(chunk) > _MAX_DOWNLOAD:
                        body += chunk[: _MAX_DOWNLOAD - len(body)]
                        break
                    body += chunk
                ctype = resp.headers.get("content-type", "")
                m = re.search(r"charset=([\w-]+)", ctype, re.I)
                enc = (m.group(1) if m else "utf-8").lower()
                return bytes(body).decode(enc, errors="replace")

    last_exc: Optional[BaseException] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return _one()
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                _sleep(_BACKOFF[attempt])
    raise ScrapeError(f"GET {target} failed after retries: {last_exc}")


def scrape_basic(
    url: str,
    item_selector: str,
    fields: Dict[str, str],
    max_items: int = 50,
    timeout: int = 20,
) -> Dict[str, Any]:
    """Scrapes 1 page: each element matching item_selector -> 1 item.

    fields: {field_name: selector}; selectors may carry "@attr" to grab an
    attribute (e.g. {"link": "a@href", "img": ".thumb@src"}); without @attr
    it takes the inner text with collapsed whitespace.

    Returns {"url", "items": [...], "count"} or {"error": ...} (never raises).
    Pages that look client-rendered (empty SPA root) -> {"error"} suggesting
    browser_tool, no bypass attempts.
    """
    try:
        if not isinstance(item_selector, str) or not item_selector.strip():
            return {"error": "item_selector must be a non-empty string"}
        if not isinstance(fields, dict) or not fields:
            return {"error": "fields must be a non-empty dict {name: selector}"}
        try:
            max_items = max(1, int(max_items))
        except (TypeError, ValueError):
            max_items = 50

        markup = _fetch_html(url, float(timeout))
        if _SPA_MARKERS.search(markup) and len(re.sub(r"<[^>]+>", "", markup).strip()) < 200:
            return {
                "error": (
                    "the page looks JS-rendered (SPA); scrape_basic only reads static HTML. "
                    "Use browser_tool for this page."
                )
            }

        root = _parse_html(markup)
        items_nodes = _select(root, item_selector)
        if not items_nodes:
            return {"error": f"item_selector {item_selector!r} matched no elements"}

        items: List[Dict[str, Any]] = []
        for node in items_nodes[:max_items]:
            item: Dict[str, Any] = {}
            for name, field_sel in fields.items():
                sel, attr = _split_field_selector(str(field_sel))
                found = _select(node, sel) if sel != "*" else [node]
                # _select walks from the node: matching children; also try the node itself
                if not found:
                    try:
                        if _match_simple(node, sel):
                            found = [node]
                    except ValueError:
                        return {"error": f"unsupported field selector {field_sel!r}"}
                if not found:
                    item[name] = None
                    continue
                el = found[0]
                item[name] = el.attrs.get(attr, "") if attr else _inner_text(el)
            items.append(item)
        return {"url": url, "items": items, "count": len(items)}
    except (SSRFError, ValueError) as exc:
        # Validation/SSRF errors: reported clearly so the agent can fix input, no retry.
        return {"error": f"{type(exc).__name__}: {exc}"}
    except ScrapeError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # never-raise: even odd errors become {"error"}
        return {"error": f"unexpected: {type(exc).__name__}: {exc}"}
