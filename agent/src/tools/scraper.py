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

import hashlib
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from tools.web_tools import WebError, _decode, _http

__all__ = [
    "scrape_list",
    "scrape_product",
    "scrape_table",
    "crawl_site",
    "read_sitemap",
    "watch_url",
    "build_tools",
]

# ---------------------------------------------------------------------------
# Test seams (offline tests only). _HTML_FIXTURES maps URL -> HTML string, an
# Exception instance to raise (e.g. WebError("HTTP 429 ...")), or a zero-arg
# callable returning either (for stateful sequences like 429-then-200).
# Production code never touches these.
_HTML_FIXTURES: Dict[str, Any] = {}
_BROWSER_FIXTURES: Dict[str, Any] = {}
# Same convention for sitemap XML bodies, keyed by sitemap URL.
_SITEMAP_FIXTURES: Dict[str, Any] = {}

# Default watch state root, captured from the environment by build_tools().
# Lets watch_url() default to <root_dir>/memories without threading env
# through every call. Tests may set it directly.
_WATCH_ROOT: Optional[str] = None

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


def _fetch_with_backoff(url: str, use_browser: str, delay: float) -> str:
    """Fetch HTML, retrying HTTP 429 with exponential backoff (max 3 retries).

    Raises WebError: immediately for blocked pages, and for 429 once the
    retries are exhausted. Shared by scrape_list and crawl_site so the
    politeness policy lives in exactly one place.
    """
    attempts = 0
    while True:
        try:
            return _fetch_html(url, use_browser)
        except WebError as e:
            if "HTTP 429" in str(e) and attempts < 3:
                attempts += 1
                time.sleep(delay * (2 ** attempts))
                continue
            raise


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

    while url and pages_fetched < max_pages:
        if pages_fetched > 0 and delay > 0:
            time.sleep(delay)
        try:
            markup = _fetch_with_backoff(url, use_browser, delay)
        except WebError as e:
            msg = str(e)
            if msg.startswith("blocked:"):
                return {"error": msg, "items": items, "pages_fetched": pages_fetched}
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
# Tables
def _table_rows(table: "_Node") -> List["_Node"]:
    """All <tr> under a table, without descending into nested tables."""
    rows: List[_Node] = []
    stack = list(table.children)
    while stack:
        node = stack.pop(0)
        if node.tag == "table":
            continue  # a nested table's rows are not this table's rows
        if node.tag == "tr":
            rows.append(node)
        stack[0:0] = node.children
    return rows


def _expand_row(cells: List["_Node"]) -> List[str]:
    """Cell texts, expanding colspan by repeating the value.

    rowspan is intentionally NOT expanded: tracking it across rows needs a
    grid model, and a wrong guess is worse than an honest gap. Documented.
    """
    out: List[str] = []
    for cell in cells:
        try:
            span = int(cell.attrs.get("colspan", "1") or "1")
        except (TypeError, ValueError):
            span = 1
        span = max(1, min(span, 20))
        out.extend([cell.text] * span)
    return out


def _dedupe_headers(headers: List[str]) -> List[str]:
    seen: Dict[str, int] = {}
    out: List[str] = []
    for header in headers:
        header = header or ""
        if header in seen:
            seen[header] += 1
            out.append(f"{header}_{seen[header]}")
        else:
            seen[header] = 1
            out.append(header)
    return out


def scrape_table(
    url: str,
    table_selector: str = "table",
    has_header: bool = True,
    delay: float = 1.0,
    use_browser: str = "auto",
) -> Dict[str, Any]:
    """Extract HTML table(s) into [{column: value}] rows.

    ``table_selector`` picks which tables (default: every <table>). With
    ``has_header=True`` the first row's <th> cells — or its <td> cells when no
    <th> exists — become the column names; otherwise columns are col1..colN.
    colspan is expanded by repeating the value; rowspan is NOT expanded (see
    _expand_row). Nested tables are treated as separate tables, never merged.
    Returns ``{"url", "tables": [{"headers", "rows"}]}`` or ``{"error"}``.
    """
    err = _validate_common(url, use_browser)
    if err:
        return err
    if not isinstance(table_selector, str) or not table_selector.strip():
        return {"error": "table_selector must be a non-empty string"}
    if not isinstance(has_header, bool):
        return {"error": "has_header must be true or false"}
    try:
        delay = float(delay)
    except (TypeError, ValueError):
        return {"error": "delay must be a number"}
    delay = max(0.0, delay)
    try:
        _select(_parse_html("<div></div>"), table_selector)
    except ValueError:
        return {"error": f"unsupported table_selector: {table_selector!r}"}

    try:
        markup = _fetch_with_backoff(url.strip(), use_browser, delay)
    except WebError as e:
        return {"error": str(e)}

    root = _parse_html(markup)
    try:
        tables = _select(root, table_selector)
    except ValueError:
        return {"error": f"unsupported table_selector: {table_selector!r}"}
    if not tables:
        return {"error": f"no table found for selector {table_selector!r} at {url}"}

    out_tables: List[Dict[str, Any]] = []
    for table in tables:
        rows = _table_rows(table)
        cell_rows = [
            [c for c in row.children if c.tag in ("td", "th")] for row in rows
        ]
        if has_header and cell_rows:
            first = cell_rows[0]
            header_cells = [c for c in first if c.tag == "th"] or first
            headers = _dedupe_headers(_expand_row(header_cells))
            data_rows = cell_rows[1:]
        elif has_header:
            headers, data_rows = [], []
        else:
            width = max((len(_expand_row(r)) for r in cell_rows), default=0)
            headers = [f"col{i + 1}" for i in range(width)]
            data_rows = cell_rows
        table_rows: List[Dict[str, str]] = []
        for row in data_rows:
            values = _expand_row(row)
            if not any(values):
                continue  # skip fully empty rows
            values += [""] * (len(headers) - len(values))
            table_rows.append(dict(zip(headers, values[: len(headers)])))
        out_tables.append({"headers": headers, "rows": table_rows})

    return {"url": url, "tables": out_tables}


# ---------------------------------------------------------------------------
# Site crawl
def _page_title(root: "_Node") -> str:
    titles = _select(root, "title")
    if titles and titles[0].text:
        return titles[0].text
    h1s = _select(root, "h1")
    if h1s and h1s[0].text:
        return h1s[0].text
    return ""


def crawl_site(
    start_url: str,
    max_pages: int = 20,
    url_pattern: Optional[str] = None,
    max_depth: int = 3,
    delay: float = 1.0,
    use_browser: str = "auto",
) -> Dict[str, Any]:
    """Breadth-first crawl of one site: [{url, title}].

    Only follows links on the start URL's domain. ``url_pattern`` is a regex
    the full URL must match (e.g. ``r"/san-pham/"``) — invalid regex is an
    error, not a silent no-op. ``max_depth`` counts link hops from the start
    page (0 = start page only). Visited URLs are deduped (fragments stripped).
    Polite like scrape_list: delay between fetches, backoff on 429, honest
    ``blocked`` errors. Returns ``{"start_url", "pages", "pages_fetched"}``.
    """
    err = _validate_common(start_url, use_browser)
    if err:
        return err
    pattern = None
    if url_pattern is not None:
        if not isinstance(url_pattern, str):
            return {"error": "url_pattern must be a regex string or null"}
        try:
            pattern = re.compile(url_pattern)
        except re.error as e:
            return {"error": f"url_pattern is not a valid regex: {e}"}
    try:
        max_pages = int(max_pages)
    except (TypeError, ValueError):
        return {"error": "max_pages must be an integer"}
    max_pages = max(1, min(max_pages, 200))
    try:
        max_depth = int(max_depth)
    except (TypeError, ValueError):
        return {"error": "max_depth must be an integer"}
    max_depth = max(0, min(max_depth, 5))
    try:
        delay = float(delay)
    except (TypeError, ValueError):
        return {"error": "delay must be a number"}
    delay = max(0.0, delay)

    start = start_url.strip()
    domain = urlparse(start).netloc.lower()
    if not domain:
        return {"error": f"start_url has no domain: {start_url!r}"}

    def _norm(u: str) -> str:
        return urljoin(start, u).split("#")[0]

    visited: set = set()
    queue: List[Tuple[str, int]] = [(_norm(start), 0)]
    pages: List[Dict[str, str]] = []

    while queue and len(pages) < max_pages:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        if pages and delay > 0:
            time.sleep(delay)
        try:
            markup = _fetch_with_backoff(url, use_browser, delay)
        except WebError as e:
            msg = str(e)
            if msg.startswith("blocked:"):
                return {
                    "error": msg,
                    "start_url": start,
                    "pages": pages,
                    "pages_fetched": len(pages),
                }
            if "HTTP 429" in msg:
                return {
                    "error": f"rate limited (HTTP 429) at {url}: stopping after backoff",
                    "start_url": start,
                    "pages": pages,
                    "pages_fetched": len(pages),
                }
            # A single dead page does not kill the crawl; skip it honestly.
            continue

        root = _parse_html(markup)
        pages.append({"url": url, "title": _page_title(root)})
        if depth >= max_depth:
            continue
        try:
            anchors = _select(root, "a")
        except ValueError:  # pragma: no cover - "a" always parses
            anchors = []
        for anchor in anchors:
            href = (anchor.attrs.get("href") or "").strip()
            if not href or href.startswith(("javascript:", "mailto:", "tel:")):
                continue
            child = _norm(href)
            if urlparse(child).netloc.lower() != domain:
                continue
            if pattern is not None and not pattern.search(child):
                continue
            if child not in visited:
                queue.append((child, depth + 1))

    return {"start_url": start, "pages": pages, "pages_fetched": len(pages)}


# ---------------------------------------------------------------------------
# Sitemaps
_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
_MAX_SITEMAP_URLS = 10000
_MAX_SITEMAP_CHILDREN = 5


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _fetch_sitemap_xml(url: str) -> str:
    """Raw sitemap XML body. Fixtures first, then network. Raises WebError."""
    if url in _SITEMAP_FIXTURES:
        return _resolve_fixture(url, _SITEMAP_FIXTURES)
    body, content_type, charset = _http(url)
    if "xml" not in content_type and "text" not in content_type:
        raise WebError(f"not a sitemap (content-type {content_type!r}) at {url}")
    return _decode(body, charset)


def read_sitemap(sitemap_url: str, _depth: int = 0) -> Dict[str, Any]:
    """Read a sitemap.xml into [{url, lastmod}].

    Handles <urlset> directly and <sitemapindex> by recursing into child
    sitemaps (max 5 children, max depth 5). Namespaces are tolerated whether
    present or not. Caps at 10000 URLs total — beyond that the caller should
    page through the sitemapindex children itself.
    """
    if not isinstance(sitemap_url, str) or not sitemap_url.strip():
        return {"error": "sitemap_url must be a non-empty string"}
    url = sitemap_url.strip()
    try:
        xml_text = _fetch_sitemap_xml(url)
    except WebError as e:
        return {"error": str(e)}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return {"error": f"invalid sitemap XML at {url}: {e}"}

    kind = _local_name(root.tag)
    urls: List[Dict[str, str]] = []
    if kind == "urlset":
        for url_el in root:
            if _local_name(url_el.tag) != "url":
                continue
            loc, lastmod = "", ""
            for child in url_el:
                name = _local_name(child.tag)
                if name == "loc":
                    loc = (child.text or "").strip()
                elif name == "lastmod":
                    lastmod = (child.text or "").strip()
            if loc:
                urls.append({"url": loc, "lastmod": lastmod})
    elif kind == "sitemapindex":
        if _depth >= 5:
            return {"error": f"sitemapindex nesting too deep at {url}"}
        children = [
            (child.text or "").strip()
            for sm in root
            if _local_name(sm.tag) == "sitemap"
            for child in sm
            if _local_name(child.tag) == "loc" and (child.text or "").strip()
        ][: _MAX_SITEMAP_CHILDREN]
        for child_url in children:
            sub = read_sitemap(child_url, _depth + 1)
            if "error" in sub:
                # One bad child sitemap does not poison the rest.
                continue
            urls.extend(sub["urls"])
    else:
        return {"error": f"not a sitemap (root <{kind}>) at {url}"}

    return {
        "sitemap_url": url,
        "urls": urls[:_MAX_SITEMAP_URLS],
        "truncated": len(urls) > _MAX_SITEMAP_URLS,
    }


# ---------------------------------------------------------------------------
# Change watching (designed for cron: run daily, report diffs)
def _watch_state_path(state_dir: str) -> str:
    return os.path.join(state_dir, "watch.json")


def _load_watch_state(state_dir: str) -> Dict[str, Any]:
    path = _watch_state_path(state_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_watch_state(state_dir: str, state: Dict[str, Any]) -> None:
    os.makedirs(state_dir, exist_ok=True)
    path = _watch_state_path(state_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def watch_url(
    url: str,
    selector: Optional[str] = None,
    state_dir: Optional[str] = None,
    delay: float = 1.0,
    use_browser: str = "auto",
) -> Dict[str, Any]:
    """Snapshot a page (or one selector's text) and report changes vs last run.

    Designed for a daily cron tick: first run stores a SHA256 snapshot and
    returns ``{"status": "watching"}``; later runs return
    ``{"changed": true/false, "prev_hash", "hash"}``. State lives in
    ``<state_dir>/watch.json`` (atomic writes). When ``state_dir`` is omitted
    it defaults to ``<root_dir>/memories`` captured from the environment at
    ``build_tools()`` time — with no root_dir and no state_dir it returns an
    honest error instead of writing somewhere surprising.

    Example cron prompt: "For each URL in my watchlist, call watch_url with
    the price selector and state_dir set; report only entries with
    changed=true."
    """
    err = _validate_common(url, use_browser)
    if err:
        return err
    if selector is not None:
        if not isinstance(selector, str) or not selector.strip():
            return {"error": "selector must be a non-empty string or null"}
        try:
            _select(_parse_html("<div></div>"), selector)
        except ValueError:
            return {"error": f"unsupported selector: {selector!r}"}
    if state_dir is None:
        if _WATCH_ROOT is None:
            return {
                "error": (
                    "watch_url needs state_dir: no root_dir on the environment "
                    "and no state_dir given — refusing to write somewhere surprising"
                )
            }
        state_dir = os.path.join(_WATCH_ROOT, "memories")
    if not isinstance(state_dir, str) or not state_dir.strip():
        return {"error": "state_dir must be a non-empty string or null"}
    try:
        delay = float(delay)
    except (TypeError, ValueError):
        return {"error": "delay must be a number"}
    delay = max(0.0, delay)

    try:
        markup = _fetch_with_backoff(url.strip(), use_browser, delay)
    except WebError as e:
        return {"error": str(e)}

    root = _parse_html(markup)
    if selector:
        try:
            nodes = _select(root, selector)
        except ValueError:
            return {"error": f"unsupported selector: {selector!r}"}
        if not nodes:
            return {"error": f"selector {selector!r} matched nothing at {url}"}
        text = "\n".join(n.text for n in nodes)
    else:
        text = root.text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    key = url.strip() if not selector else f"{url.strip()}\n{selector.strip()}"
    try:
        state = _load_watch_state(state_dir)
    except Exception as e:
        return {"error": f"cannot read watch state at {state_dir}: {e}"}
    entry = state.get(key)
    record = {
        "hash": digest,
        "selector": selector,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    state[key] = record
    try:
        _save_watch_state(state_dir, state)
    except OSError as e:
        return {"error": f"cannot write watch state at {state_dir}: {e}"}

    if entry is None or not isinstance(entry, dict) or "hash" not in entry:
        return {"url": url, "status": "watching", "hash": digest}
    prev = entry["hash"]
    return {"url": url, "changed": prev != digest, "prev_hash": prev, "hash": digest}


# ---------------------------------------------------------------------------
# Tool registry
def build_tools(env: Any = None) -> Dict[str, Dict[str, Any]]:
    """Registry-ready tool dicts for AgentEnvironment._tools."""
    global _WATCH_ROOT
    # watch_url() defaults its state dir to <root_dir>/memories; capture it
    # here so the tool itself stays a plain function of (url, selector).
    root_dir = getattr(env, "root_dir", None)
    if root_dir is not None:
        _WATCH_ROOT = str(root_dir)
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
        "scrape_table": {
            "name": "scrape_table",
            "func": scrape_table,
            "description": (
                "Extract HTML table(s) into [{column: value}] rows. The first "
                "row's <th> cells (or <td> when no <th>) become column names. "
                "colspan is expanded; rowspan is not. Returns {url, tables}."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Page URL holding the table."},
                    "table_selector": {
                        "type": "string",
                        "description": "Selector picking tables. Default 'table'.",
                    },
                    "has_header": {
                        "type": "boolean",
                        "description": "First row holds column names. Default true.",
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
                "required": ["url"],
            },
        },
        "crawl_site": {
            "name": "crawl_site",
            "func": crawl_site,
            "description": (
                "Breadth-first crawl of one site (same domain only): returns "
                "[{url, title}]. url_pattern is a regex filter, e.g. "
                "'/san-pham/'. Polite: delay between fetches, backs off on "
                "HTTP 429, dedupes visited URLs."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "start_url": {"type": "string", "description": "Crawl start URL."},
                    "max_pages": {
                        "type": "integer",
                        "description": "Page cap (1-200). Default 20.",
                    },
                    "url_pattern": {
                        "type": "string",
                        "description": "Regex the URL must match, e.g. '/san-pham/'.",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Link hops from start (0-5). Default 3.",
                    },
                    "delay": {
                        "type": "number",
                        "description": "Seconds between fetches. Default 1.0.",
                    },
                    "use_browser": {
                        "type": "string",
                        "description": "'auto' (default), 'always' or 'never'.",
                    },
                },
                "required": ["start_url"],
            },
        },
        "read_sitemap": {
            "name": "read_sitemap",
            "func": read_sitemap,
            "description": (
                "Read a sitemap.xml into [{url, lastmod}]. Handles <urlset> "
                "and <sitemapindex> (recurses into child sitemaps, max 5)."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "sitemap_url": {"type": "string", "description": "sitemap.xml URL."},
                },
                "required": ["sitemap_url"],
            },
        },
        "watch_url": {
            "name": "watch_url",
            "func": watch_url,
            "description": (
                "Snapshot a page (or one selector's text) and report changes "
                "vs the last run: first call stores a SHA256 snapshot, later "
                "calls return {changed, prev_hash, hash}. Designed for a daily "
                "cron tick (e.g. price watching). State in <state_dir>/watch.json."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to watch."},
                    "selector": {
                        "type": "string",
                        "description": "Only hash this selector's text. Default: whole page.",
                    },
                    "state_dir": {
                        "type": "string",
                        "description": "Dir holding watch.json. Default: <root_dir>/memories.",
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
                "required": ["url"],
            },
        },
    }
