"""Offline tests for web.py — httpx is fully mocked, no real network.

Run:  python3 test_web.py   (or: python3 -m unittest test_web -v)
"""

import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402

import web  # noqa: E402

# ---------------------------------------------------------------------------
# Fake httpx plumbing
DDG_SAMPLE = """
<html><body>
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Falpha&rut=abc">Alpha <b>Page</b></a>
  </h2>
  <a class="result__snippet" href="https://example.com/alpha">First snippet about alpha.</a>
</div>
<div class="result">
  <h2 class="result__title">
    <a class="result__a" href="https://example.com/beta">Beta Page</a>
  </h2>
  <a class="result__snippet" href="https://example.com/beta">Second snippet about beta.</a>
</div>
<div class="result">
  <h2 class="result__title">
    <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Fgamma">Gamma Page</a>
  </h2>
  <a class="result__snippet" href="https://example.com/gamma">Third snippet about gamma.</a>
</div>
</body></html>
"""

PAGE_HTML = """<!DOCTYPE html>
<html><head><title>  Hello &amp; World  </title>
<style>body{color:red}</style>
<script>alert("x")</script>
</head>
<body>
<nav><a href="/">home</a> <a href="/x">x</a></nav>
<header>Site Header</header>
<article id="main" class="content">
  <h1>Main Article</h1>
  <p>First paragraph with <b>bold</b> text.</p>
  <p>Second paragraph.</p>
</article>
<footer>Copyright 2026</footer>
</body></html>
"""


class FakeHeaders(dict):
    def get(self, key, default=None):  # case-insensitive like httpx.Headers
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class FakeResponse:
    def __init__(self, status=200, content=b"", content_type="text/html; charset=utf-8",
                 url="https://example.com/page"):
        self.status_code = status
        self.content = content
        self.headers = FakeHeaders({"content-type": content_type})
        self.url = url


class FakeStream:
    """Context manager mimicking httpx.Client.stream()."""

    def __init__(self, response=None, chunks=None, exc=None):
        self._response = response
        self._chunks = chunks or []
        self._exc = exc

    def __enter__(self):
        if self._exc:
            raise self._exc
        return self

    def __exit__(self, *a):
        return False

    @property
    def status_code(self):
        return self._response.status_code

    @property
    def headers(self):
        return self._response.headers

    @property
    def url(self):
        return self._response.url

    def iter_bytes(self, chunk_size=None):
        if self._exc:
            raise self._exc
        for chunk in self._chunks:
            yield chunk


class FakeClient:
    """Replaces httpx.Client. Class attributes configure each test's behavior."""

    mode = "ok"            # ok | timeout | stream_chunks
    posted = []            # records (url, data) of post() calls
    request_count = 0

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, data=None, headers=None, **kw):
        FakeClient.request_count += 1
        FakeClient.posted.append((url, data))
        if FakeClient.mode == "timeout":
            raise httpx.ConnectError("connection refused")
        return FakeResponse(200, DDG_SAMPLE.encode("utf-8"))

    def stream(self, method, url, **kw):
        FakeClient.request_count += 1
        if FakeClient.mode == "timeout":
            return FakeStream(exc=httpx.ConnectError("connection refused"))
        if FakeClient.mode == "stream_chunks":
            return FakeStream(
                response=FakeResponse(200, b"", url=url),
                chunks=FakeClient.chunks,
            )
        return FakeStream(
            response=FakeResponse(200, b"", url=url),
            chunks=[PAGE_HTML.encode("utf-8")],
        )


class WebTest(unittest.TestCase):
    def setUp(self):
        FakeClient.mode = "ok"
        FakeClient.request_count = 0
        FakeClient.posted = []
        FakeClient.chunks = []
        if web._BREAKER is not None:
            web._BREAKER.reset()
        self._client_patcher = mock.patch("web.httpx.Client", FakeClient)
        self._client_patcher.start()
        self._sleep_patcher = mock.patch("web._sleep")  # no real sleeping
        self.mock_sleep = self._sleep_patcher.start()

    def tearDown(self):
        self._client_patcher.stop()
        self._sleep_patcher.stop()

    # --- Test 1: DDG search parses exactly 3 results -----------------------
    def test_search_parses_three_results(self):
        results = web.web_search("boom agent", max_results=5)
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["title"], "Alpha Page")
        self.assertEqual(results[0]["url"], "https://example.com/alpha")
        self.assertEqual(results[0]["snippet"], "First snippet about alpha.")
        self.assertEqual(results[1]["title"], "Beta Page")
        self.assertEqual(results[1]["url"], "https://example.com/beta")
        self.assertEqual(results[1]["snippet"], "Second snippet about beta.")
        self.assertEqual(results[2]["title"], "Gamma Page")
        self.assertEqual(results[2]["url"], "https://example.com/gamma")
        self.assertEqual(results[2]["snippet"], "Third snippet about gamma.")
        # DDG /l/?uddg= redirects were unwrapped, nothing points at duckduckgo
        for r in results:
            self.assertNotIn("duckduckgo", r["url"])
            self.assertTrue(r["url"].startswith("https://example.com/"))

    def test_search_max_results_limits(self):
        results = web.web_search("boom agent", max_results=2)
        self.assertEqual(len(results), 2)

    def test_search_empty_query_raises(self):
        with self.assertRaises(ValueError):
            web.web_search("   ")

    # --- Test 2: fetch extracts <title> + text ------------------------------
    def test_fetch_title_and_text(self):
        out = web.web_fetch("https://example.com/page")
        self.assertEqual(out["status"], 200)
        self.assertEqual(out["url"], "https://example.com/page")
        self.assertIn("text/html", out["content_type"])
        self.assertEqual(out["title"], "Hello & World")
        self.assertIn("Main Article", out["text"])
        self.assertFalse(out["truncated"])

    # --- Test 3: extract drops script/style ---------------------------------
    def test_extract_removes_script_style_nav_footer(self):
        out = web.web_extract("https://example.com/page")
        text = out["text"]
        self.assertIn("Main Article", text)
        self.assertIn("First paragraph with bold text.", text)
        self.assertNotIn("alert(", text)          # script gone
        self.assertNotIn("color:red", text)        # style gone
        self.assertNotIn("Copyright 2026", text)   # footer gone
        # nav removed entirely: its links must not leak into the text
        self.assertNotRegex(text, r"(?m)^home$")
        # <header> is kept by design (spec drops only script/style/nav/footer)
        self.assertIn("Site Header", text)
        self.assertNotIn('href="/"', text)

    def test_extract_with_selector(self):
        out = web.web_extract("https://example.com/page", selector="article")
        self.assertIn("Main Article", out["text"])
        self.assertNotIn("Site Header", out["text"])
        out = web.web_extract("https://example.com/page", selector="#main")
        self.assertIn("Second paragraph.", out["text"])
        out = web.web_extract("https://example.com/page", selector=".content")
        self.assertIn("First paragraph", out["text"])
        out = web.web_extract("https://example.com/page", selector="article#main")
        self.assertIn("Main Article", out["text"])

    def test_extract_bad_selector(self):
        with self.assertRaises(ValueError):
            web.web_extract("https://example.com/page", selector="div > p")
        with self.assertRaises(ValueError):
            web.web_extract("https://example.com/page", selector="#nope")

    # --- Test 4: timeout retries then raises ---------------------------------
    def test_timeout_retries_twice_then_raises(self):
        FakeClient.mode = "timeout"
        with self.assertRaises(web.WebFetchError):
            web.web_fetch("https://example.com/dead", timeout=5)
        # 1 initial attempt + 2 retries
        self.assertEqual(FakeClient.request_count, 3)
        # backoff sleeps were 1s then 2s
        self.assertEqual(
            [c.args[0] for c in self.mock_sleep.call_args_list], [1.0, 2.0]
        )

    def test_search_timeout_raises_after_retries(self):
        FakeClient.mode = "timeout"
        with self.assertRaises(web.WebFetchError):
            web.web_search("boom agent")
        self.assertEqual(FakeClient.request_count, 3)

    # --- Test 5: > 500 KiB body is cut ----------------------------------------
    def test_large_body_is_truncated_at_500k(self):
        big = b"x" * (web._MAX_DOWNLOAD + 12345)
        FakeClient.mode = "stream_chunks"
        FakeClient.chunks = [big[i:i + 65536] for i in range(0, len(big), 65536)]
        out = web.web_fetch("https://example.com/big")
        self.assertTrue(out["truncated"])
        self.assertLessEqual(len(out["text"].encode("utf-8")), web._MAX_DOWNLOAD)

    # --- breaker sanity (fallback path when lifecycle.circuit is absent) ------
    def test_module_reports_breaker_state(self):
        # In this standalone dir lifecycle.circuit is NOT importable.
        if web.CircuitBreaker is None:
            self.assertIsNone(web._BREAKER)
        else:  # pragma: no cover - only when run inside the repo tree
            self.assertIsNotNone(web._BREAKER)

    # --- build_tools registry -------------------------------------------------
    def test_build_tools_registry(self):
        tools = web.build_tools()
        self.assertEqual(set(tools), {"web_search", "web_fetch", "web_extract"})
        res = tools["web_search"]["func"]("boom agent")
        self.assertEqual(len(res), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
