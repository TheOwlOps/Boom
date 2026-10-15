"""Structured web scraping toolkit: lists and product pages.

This module does NOT reimplement fetching or rendering:

- Static HTML comes from ``tools.web_tools``' network primitives
  (``_http`` + ``_decode``) — the same headers, timeout and error mapping as
  ``web_extract``. We need *raw* HTML (``web_extract`` returns visible text,
  which has no DOM left for selectors), so we reuse the primitives directly
  instead of duplicating them.
- JS-heavy pages fall back to ``tools.browser_tool`` (headless Chrome over
  CDP): navigate, then read ``document.documentElement.outerHTML`` and run the
  same selector engine on the rendered markup. The browser import is lazy, so
  importing this module never requires Chrome.

When each path is used (documented here, not hidden):
  1. Static fetch first, always.
  2. If selector extraction finds nothing AND the markup looks client-rendered
     (empty body with script bundles / known SPA roots), try the browser once.
  3. ``use_browser="always"`` skips the static attempt; ``"never"`` disables
     the fallback (pure offline/static use, e.g. tests).

Politeness: a ``delay`` (default 1.0s) sleeps between page fetches. An HTTP
429 triggers exponential backoff (up to 3 attempts); if the server still says
429 the crawl stops and returns what it already collected — never an infinite
retry loop.

Bot-blocked pages (HTTP 403, captcha markers) return
``{"error": "blocked: ..."}``. This module never tries to bypass a block;
that belongs to a dedicated captcha connector.

All public functions are never-raise: failures come back as ``{"error": ...}``.
"""

import re
import time
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from tools.web_tools import WebError, _decode, _http

__all__ = ["scrape_list", "scrape_product", "build_tools"]

# ---------------------------------------------------------------------------
# Test seams (offline tests only). _HTML_FIXTURES maps URL -> HTML string, an
# Exception instance to raise (e.g. WebError("HTTP 429 ...")), or a zero-arg
# callable returning either (for stateful sequences like 429-then-200).
# Production code never touches these.
_HTML_FIXTURES: Dict[str, Any] = {}
_BROWSER_FIXTURES: Dict[str, Any] = {}

_CAPTCHA_MARKERS = (
    "captcha", "recaptcha", "cf-challenge", "challenge-platform",
    "datadome", "perimeterx", "just a moment", "attention required",
)

_SPA_MARKERS = (
    'id="root"', "id='root'", 'id="app"', "id='app'",
    "__NEXT_DATA__", "__NUXT__", 'id="__next"',
)

_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


# ---------------------------------------------------------------------------
# Tiny DOM + selector engine (stdlib only)
class _Node:
    __slots__ = ("tag", "attrs", "children", "text_parts")

    def __init__(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        self.tag = tag.lower()
        # HTMLParser gives attrs as list of (name, value); value may be None.
        self.attrs = {k.lower(): (v if v is not None else "") for k, v in attrs}
        self.children: List["_Node"] = []
        self.text_parts: List[str] = []

    @property
    def text(self) -> str:
        parts = list(self.text_parts)
        for child in self.children:
            t = child.text
            if t:
                parts.append(t)
        return re.sub(r"\s+", " ", "".join(parts)).strip()

    def classes(self) -> List[str]:
        return self.attrs.get("class", "").split()


class _DOMBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", [])
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        node = _Node(tag, attrs)
        self._stack[-1].children.append(node)
        if tag.lower() not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        node = _Node(tag, attrs)
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        # Pop until the matching open tag; never pop the document root.
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                break

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._stack[-1].text_parts.append(data)


def _parse_html(markup: str) -> _Node:
    builder = _DOMBuilder()
    builder.feed(markup)
    builder.close()
    return builder.root


# Selector grammar (intentionally small, documented):
#   tag            e.g. "h2"
#   .class         e.g. ".price"
#   tag.class      e.g. "span.price"
#   #id            e.g. "#main"
#   [attr]         e.g. "[data-id]"
#   [attr="v"]     e.g. '[data-type="item"]'
#   tag[attr="v"]  e.g. 'a[rel="next"]'
#   "A B"          descendant combinator, e.g. ".product .price"
# A trailing "@attr" on a *field* selector (not part of the CSS grammar)
# extracts that attribute instead of text, e.g. ".thumb@src".
_SIMPLE_RE = re.compile(
    r"^(?:(?P<tag>[a-zA-Z][a-zA-Z0-9]*)?)?"
    r"(?:#(?P<id>[a-zA-Z0-9_-]+))?"
    r"(?:\.(?P<class>[a-zA-Z0-9_-]+))?"
    r"(?:\[(?P<attr>[a-zA-Z0-9_-]+)(?:=\"(?P<val>[^\"]*)\")?\])?$"
)


def _match_simple(node: _Node, part: str) -> bool:
    m = _SIMPLE_RE.match(part)
    if not m or node.tag == "document":
        return False
    g = m.groupdict()
    if not any([g["tag"], g["id"], g["class"], g["attr"]]):
        return False
    if g["tag"] and node.tag != g["tag"].lower():
        return False
    if g["id"] and node.attrs.get("id") != g["id"]:
        return False
    if g["class"] and g["class"] not in node.classes():
        return False
    if g["attr"]:
        if g["attr"] not in node.attrs:
            return False
        if g["val"] is not None and node.attrs[g["attr"]] != g["val"]:
            return False
    return True


def _select(root: _Node, selector: str) -> List[_Node]:
    """Find nodes matching a (possibly descendant) selector, in document order."""
    parts = selector.split()
    if not parts or any(not _SIMPLE_RE.match(p) for p in parts):
        raise ValueError(f"unsupported selector: {selector!r}")
    current = [root]
    for part in parts:
        found: List[_Node] = []
        for node in current:
            stack = list(node.children)
            while stack:
                n = stack.pop(0)
                if _match_simple(n, part):
                    found.append(n)
                stack[0:0] = n.children
        current = found
        if not current:
            break
    return current


def _split_field_selector(field_selector: str) -> Tuple[str, Optional[str]]:
    """Split 'selector@attr' into (selector, attr). '@' inside [...] is not split."""
    # Only a trailing @attr counts; find the last '@' outside brackets.
    depth = 0
    for i in range(len(field_selector) - 1, -1, -1):
        ch = field_selector[i]
        if ch == "]":
            depth += 1
        elif ch == "[":
            depth -= 1
        elif ch == "@" and depth == 0:
            return field_selector[:i], field_selector[i + 1 :]
    return field_selector, None


# ---------------------------------------------------------------------------
# Fetching
def _looks_client_rendered(markup: str) -> bool:
    """Heuristic: body nearly empty but SPA markers present."""
    lowered = markup.lower()
    if not any(m.lower() in lowered for m in _SPA_MARKERS):
        return False
    text_len = len(re.sub(r"<[^>]+>", "", markup).strip())
    return text_len < 500


def _resolve_fixture(url: str, fixtures: Dict[str, Any]) -> str:
    fixture = fixtures[url]
    if callable(fixture):
        fixture = fixture()
    if isinstance(fixture, Exception):
        raise fixture
    return fixture


def _fetch_html_static(url: str) -> str:
    """Raw HTML via web_tools' network primitives. Raises WebError / returns fixture."""
    if url in _HTML_FIXTURES:
        return _resolve_fixture(url, _HTML_FIXTURES)
    body, content_type, charset = _http(url)
    if "html" not in content_type:
        raise WebError(f"not an HTML page (content-type {content_type!r}) for {url}")
    return _decode(body, charset)


def _fetch_html_browser(url: str) -> str:
    """Rendered HTML via headless Chrome. Raises WebError on failure."""
    if url in _BROWSER_FIXTURES:
        return _resolve_fixture(url, _BROWSER_FIXTURES)
    try:
        from tools.browser_tool import get_session
    except Exception as e:
        raise WebError(f"browser unavailable: {type(e).__name__}: {e}")
    session = get_session()
    nav = session.goto_url(url)
    if isinstance(nav, dict) and "error" in nav:
        raise WebError(f"browser navigation failed: {nav['error']}")
    res = session.js_eval("document.documentElement.outerHTML")
    if not isinstance(res, dict) or "error" in res:
        err = res.get("error", "unknown") if isinstance(res, dict) else res
        raise WebError(f"browser eval failed: {err}")
    html = res.get("result")
    if not isinstance(html, str) or not html.strip():
        raise WebError("browser returned empty markup")
    return html


def _blocked_error(url: str, markup: str, status: str) -> Dict[str, Any]:
    lowered = markup.lower()
    marker = next((m for m in _CAPTCHA_MARKERS if m in lowered), None)
    if marker:
        return {"error": f"blocked: captcha/challenge detected ({marker}) at {url} — not bypassing"}
    return {"error": f"blocked: HTTP {status} at {url}"}


def _fetch_html(url: str, use_browser: str = "auto") -> str:
    """Fetch raw HTML: static first, browser fallback per policy. Raises WebError."""
    if use_browser == "always":
        return _fetch_html_browser(url)
    try:
        markup = _fetch_html_static(url)
    except WebError as e:
        msg = str(e)
        if "HTTP 403" in msg:
            # A 403 may still carry a challenge page body; _http discards it,
            # so report blocked without the marker scan.
            raise WebError(f"blocked: HTTP 403 at {url} — not bypassing")
        raise
    # Captcha/challenge pages sometimes return 200 with a challenge body.
    lowered = markup.lower()
    if any(m in lowered for m in _CAPTCHA_MARKERS) and len(markup) < 20000:
        raise WebError(f"blocked: captcha/challenge detected at {url} — not bypassing")
    if use_browser == "never":
        return markup
    if _looks_client_rendered(markup):
        return _fetch_html_browser(url)
    return markup


# ---------------------------------------------------------------------------
# Extraction
def _extract_fields(root: _Node, fields: Dict[str, str]) -> Tuple[Dict[str, Any], Optional[str]]:
    """Extract {field: value} from root. Returns (data, error_selector_or_None)."""
    data: Dict[str, Any] = {}
    for field, field_selector in fields.items():
        if not isinstance(field_selector, str) or not field_selector.strip():
            return {}, field_selector
        selector, attr = _split_field_selector(field_selector.strip())
        try:
            nodes = _select(root, selector)
        except ValueError:
            return {}, field_selector
        if not nodes:
            return {}, field_selector
        node = nodes[0]
        if attr:
            data[field] = node.attrs.get(attr, "")
        else:
            data[field] = node.text
    return data, None


def _validate_common(url: str, use_browser: str) -> Optional[Dict[str, Any]]:
    if not isinstance(url, str) or not url.strip():
        return {"error": "url must be a non-empty string"}
    if use_browser not in ("auto", "always", "never"):
        return {"error": "use_browser must be 'auto', 'always' or 'never'"}
    return None


def scrape_list(
    start_url: str,
    item_selector: str,
    fields: Dict[str, str],
    max_pages: int = 5,
    next_selector: Optional[str] = None,
    delay: float = 1.0,
    use_browser: str = "auto",
) -> Dict[str, Any]:
    """Crawl a paginated list, extracting fields per item.

    ``fields`` maps field name -> selector; a trailing ``@attr`` extracts an
    attribute instead of text (e.g. ``{"img": ".thumb@src"}``).
    ``next_selector`` should match the "next page" link (its ``href`` is used).
    Returns ``{"url", "items", "pages_fetched", "truncated"?}`` or ``{"error"}``.
    A page whose items selector matches nothing yields ``{"error"}`` naming the
    selector — never silent empty results.
    """
    err = _validate_common(start_url, use_browser)
    if err:
        return err
    if not isinstance(item_selector, str) or not item_selector.strip():
        return {"error": "item_selector must be a non-empty string"}
    if not isinstance(fields, dict) or not fields:
        return {"error": "fields must be a non-empty {name: selector} dict"}
    try:
        max_pages = int(max_pages)
    except (TypeError, ValueError):
        return {"error": "max_pages must be an integer"}
    max_pages = max(1, min(max_pages, 50))
    try:
        delay = float(delay)
    except (TypeError, ValueError):
        return {"error": "delay must be a number"}
    delay = max(0.0, delay)

    try:
        _select(_parse_html("<div></div>"), item_selector)
    except ValueError:
        return {"error": f"unsupported item_selector: {item_selector!r}"}

    items: List[Dict[str, Any]] = []
    url: Optional[str] = start_url.strip()
    pages_fetched = 0
    backoff_attempts = 0

    while url and pages_fetched < max_pages:
        if pages_fetched > 0 and delay > 0:
            time.sleep(delay)
        try:
            markup = _fetch_html(url, use_browser)
            backoff_attempts = 0
        except WebError as e:
            msg = str(e)
            if msg.startswith("blocked:"):
                return {"error": msg, "items": items, "pages_fetched": pages_fetched}
            if "HTTP 429" in msg and backoff_attempts < 3:
                backoff_attempts += 1
                time.sleep(delay * (2 ** backoff_attempts))
                continue  # retry the same URL
            if "HTTP 429" in msg:
                return {
                    "error": f"rate limited (HTTP 429) at {url}: stopping after backoff",
                    "items": items,
                    "pages_fetched": pages_fetched,
                }
            return {"error": msg, "items": items, "pages_fetched": pages_fetched}

        root = _parse_html(markup)
        try:
            item_nodes = _select(root, item_selector)
        except ValueError:
            return {"error": f"unsupported item_selector: {item_selector!r}"}
        if not item_nodes:
            return {
                "error": (
                    f"selector {item_selector!r} matched nothing at {url} "
                    "(page may need JS — try use_browser='always' — or the selector is wrong)"
                ),
                "items": items,
                "pages_fetched": pages_fetched,
            }
        for node in item_nodes:
            data, bad = _extract_fields(node, fields)
            if bad is not None:
                return {
                    "error": f"field selector {bad!r} matched nothing at {url}",
                    "items": items,
                    "pages_fetched": pages_fetched,
                }
            data["_source"] = url
            items.append(data)
        pages_fetched += 1

        # Pagination: resolve the next URL whenever one exists, so the caller
        # can tell "no more pages" apart from "stopped at max_pages".
        url = None
        if next_selector:
            try:
                next_nodes = _select(root, next_selector)
            except ValueError:
                return {
                    "error": f"unsupported next_selector: {next_selector!r}",
                    "items": items,
                    "pages_fetched": pages_fetched,
                }
            if next_nodes:
                href = next_nodes[0].attrs.get("href", "")
                if href:
                    url = urljoin(start_url, href)

    out: Dict[str, Any] = {
        "url": start_url,
        "items": items,
        "pages_fetched": pages_fetched,
    }
    if pages_fetched >= max_pages and url:
        out["truncated"] = True
        out["note"] = f"stopped at max_pages={max_pages}"
    return out


def scrape_product(
    url: str,
    schema: Dict[str, str],
    delay: float = 1.0,
    use_browser: str = "auto",
) -> Dict[str, Any]:
    """Extract one detail page (e.g. a product) per ``schema`` {field: selector}.

    Same ``@attr`` convention as ``scrape_list``. Returns
    ``{"url", "data"}`` or ``{"error"}`` naming the failing selector.
    """
    err = _validate_common(url, use_browser)
    if err:
        return err
    if not isinstance(schema, dict) or not schema:
        return {"error": "schema must be a non-empty {field: selector} dict"}
    try:
        delay = float(delay)
    except (TypeError, ValueError):
        return {"error": "delay must be a number"}
    delay = max(0.0, delay)

    try:
        markup = _fetch_html(url.strip(), use_browser)
    except WebError as e:
        return {"error": str(e)}

    root = _parse_html(markup)
    data, bad = _extract_fields(root, schema)
    if bad is not None:
        return {"error": f"selector {bad!r} matched nothing at {url}"}
    return {"url": url, "data": data}


# ---------------------------------------------------------------------------
# Tool registry
def build_tools(env: Any = None) -> Dict[str, Dict[str, Any]]:
    """Registry-ready tool dicts for AgentEnvironment._tools."""
    _ = env  # accepted for the registration call site; no per-env state.
    return {
        "scrape_list": {
            "name": "scrape_list",
            "func": scrape_list,
            "description": (
                "Crawl a paginated product/list page and extract structured fields "
                "per item via CSS selectors (tag, .class, tag.class, #id, [attr], "
                "descendant; trailing @attr extracts an attribute, e.g. '.thumb@src'). "
                "Polite: 1s delay between pages, backs off on HTTP 429. "
                "JS-heavy pages fall back to the headless browser automatically. "
                "Blocked pages (403/captcha) return an honest 'blocked' error."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "start_url": {"type": "string", "description": "First list-page URL."},
                    "item_selector": {
                        "type": "string",
                        "description": "Selector matching one item per card, e.g. '.product'.",
                    },
                    "fields": {
                        "type": "object",
                        "description": "{field: selector} extracted per item.",
                    },
                    "max_pages": {"type": "integer", "description": "Page cap. Default 5."},
                    "next_selector": {
                        "type": "string",
                        "description": "Selector for the next-page link (uses its href).",
                    },
                    "delay": {
                        "type": "number",
                        "description": "Seconds between page fetches. Default 1.0.",
                    },
                    "use_browser": {
                        "type": "string",
                        "description": "'auto' (default), 'always' or 'never'.",
                    },
                },
                "required": ["start_url", "item_selector", "fields"],
            },
        },
        "scrape_product": {
            "name": "scrape_product",
            "func": scrape_product,
            "description": (
                "Extract one detail page (e.g. a product: name, price, image, "
                "description) per a {field: selector} schema. Same selector "
                "grammar and @attr convention as scrape_list."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Detail-page URL."},
                    "schema": {
                        "type": "object",
                        "description": "{field: selector} to extract.",
                    },
                    "delay": {
                        "type": "number",
                        "description": "Seconds before fetching. Default 1.0.",
                    },
                    "use_browser": {
                        "type": "string",
                        "description": "'auto' (default), 'always' or 'never'.",
                    },
                },
                "required": ["url", "schema"],
            },
        },
    }
