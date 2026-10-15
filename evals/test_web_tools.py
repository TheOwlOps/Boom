"""Offline eval for tools.web_tools: real HTTP against a local server, no internet.

The parsing functions are exercised directly on fixture strings; the network path is
exercised end to end against a ThreadingHTTPServer on 127.0.0.1.
"""

import shutil
import socket
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

import tools.web_tools as web_tools
from tools.web_tools import WebTools, build_tools, parse_ddg_html

# --- fixtures ---------------------------------------------------------------

PAGE_HTML = """<!DOCTYPE html>
<html><head><title>Fixture &amp; Page</title>
<script>var secret = "SHOULD_NOT_APPEAR";</script>
<style>.x { color: red }</style>
</head><body>
<h1>Hello &amp; welcome</h1>
<p>First paragraph with an entity &mdash; and a &lt;tag&gt;.</p>
<noscript>js is off</noscript>
<script>console.log("also hidden");</script>
<p>Second paragraph.</p>
</body></html>"""

JSON_BODY = '{"ok": true, "note": "plain json body"}'

BIG_HTML = "<html><head><title>Big</title></head><body><p>" + ("lorem ipsum dolor " * 2000) + "</p></body></html>"

# A saved copy of the DuckDuckGo no-JS result markup: two `uddg=` redirects, one plain
# href, one <div> snippet, entities in the title.
DDG_FIXTURE = """<!DOCTYPE html>
<html><body>
<div class="serp__results">
  <div class="result results_links results_links_deep web-result">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fhtml.parser.html&amp;rut=8f1">html.parser &mdash; Python docs</a>
      </h2>
      <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fhtml.parser.html">This module defines a class <b>HTMLParser</b> &amp; more.</a>
    </div>
  </div>
  <div class="result results_links web-result">
    <div class="links_main result__body">
      <h2 class="result__title"><a rel="nofollow" class="result__a" href="https://example.org/plain">Plain result</a></h2>
      <div class="result__snippet">A plain snippet with no markup.</div>
    </div>
  </div>
  <div class="result results_links web-result">
    <div class="links_main result__body">
      <h2 class="result__title"><a rel="nofollow" class="result__a" href="/l/?uddg=https%3A%2F%2Fja.wikipedia.org%2Fwiki%2F%E3%83%A1%E3%83%A2%E3%83%AA">メモリ - Wikipedia</a></h2>
      <div class="result__snippet">Japanese page about memory.</div>
    </div>
  </div>
</div>
</body></html>"""

POSTED_BODIES = []
DOCROOT = None


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DOCROOT), **kwargs)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        POSTED_BODIES.append(self.rfile.read(length).decode("utf-8"))
        payload = (DOCROOT / "search.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def _closed_port() -> int:
    """A 127.0.0.1 port nothing is listening on, for the refused-connection case."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_web_tools():
    global DOCROOT
    tmp = Path(tempfile.mkdtemp(prefix="web_tools_eval_"))
    DOCROOT = tmp
    (tmp / "page.html").write_text(PAGE_HTML, encoding="utf-8")
    (tmp / "big.html").write_text(BIG_HTML, encoding="utf-8")
    (tmp / "search.html").write_text(DDG_FIXTURE, encoding="utf-8")
    (tmp / "plain.json").write_text(JSON_BODY, encoding="utf-8")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    original_endpoint = web_tools.SEARCH_ENDPOINT
    try:
        # 1. HTML -> visible text, script/style/noscript gone, entities decoded.
        res = WebTools.extract(f"{base}/page.html")
        assert "error" not in res, res
        assert res["title"] == "Fixture & Page", res
        content = res["content"]
        assert "Hello & welcome" in content, content
        assert "First paragraph with an entity — and a <tag>." in content, content
        assert "Second paragraph." in content, content
        assert "SHOULD_NOT_APPEAR" not in content, content
        assert "also hidden" not in content, content
        assert "color: red" not in content, content
        assert "js is off" not in content, content
        print("[PASS] web_extract HTML -> visible text (script/style/noscript dropped).")

        # 2. Truncation is reported, not silent.
        res = WebTools.extract(f"{base}/big.html")
        assert res.get("truncated") is True, res
        assert "[truncated:" in res["content"], res["content"][-200:]
        assert len(res["content"]) < len(BIG_HTML), "content was not capped"
        print("[PASS] web_extract truncation reported at the cap.")

        # 3. text/* and application/json come back verbatim.
        res = WebTools.extract(f"{base}/plain.json")
        assert res["content"] == JSON_BODY, res
        assert res["content_type"] == "application/json", res
        assert not res.get("truncated"), res
        print("[PASS] web_extract JSON body returned verbatim.")

        # 4. 404 and refused connections return an error, never fabricated content.
        res = WebTools.extract(f"{base}/missing.html")
        assert "error" in res and "content" not in res, res
        assert "404" in res["error"], res
        res = WebTools.extract(f"http://127.0.0.1:{_closed_port()}/page.html")
        assert "error" in res and "content" not in res, res
        assert "content" not in res and "results" not in res, res
        print("[PASS] HTTP 404 and refused connection return {error}.")

        # 5. Non-http schemes are rejected before any fetch.
        for bad in ("file:///C:/Windows/win.ini", "ftp://example.com/x", "not a url"):
            res = WebTools.extract(bad)
            assert "error" in res and "content" not in res, res
        print("[PASS] non-http(s) URLs rejected.")

        # 6. Search parser, direct on the saved fixture (no network).
        rows = parse_ddg_html(DDG_FIXTURE)
        assert len(rows) == 3, rows
        assert rows[0]["title"] == "html.parser — Python docs", rows[0]
        assert rows[0]["url"] == "https://docs.python.org/3/library/html.parser.html", rows[0]
        assert rows[0]["snippet"] == "This module defines a class HTMLParser & more.", rows[0]
        assert rows[1] == {"title": "Plain result", "url": "https://example.org/plain",
                           "snippet": "A plain snippet with no markup."}, rows[1]
        assert rows[2]["url"] == "https://ja.wikipedia.org/wiki/メモリ", rows[2]
        print("[PASS] DuckDuckGo HTML parser: titles, urls, snippets, uddg redirects.")

        # 7. Search end to end against the local endpoint (real POST, real urllib).
        web_tools.SEARCH_ENDPOINT = f"{base}/html/"
        res = WebTools.search("python html parser", limit=2)
        assert "error" not in res, res
        assert len(res["results"]) == 2, res
        assert res["results"][0]["url"] == "https://docs.python.org/3/library/html.parser.html", res
        assert POSTED_BODIES and POSTED_BODIES[-1] == "q=python+html+parser", POSTED_BODIES
        print("[PASS] web_search posts the query form field and parses the response.")

        # 8. An unreachable search endpoint reports the failure instead of empty rows.
        web_tools.SEARCH_ENDPOINT = f"http://127.0.0.1:{_closed_port()}/html/"
        res = WebTools.search("anything")
        assert "error" in res and "results" not in res, res
        assert WebTools.search("")["error"] == "empty query"
        print("[PASS] failed search returns {error}, empty query rejected.")

        # 9. Registry shape: what environment._init_tools would merge in.
        tools = build_tools()
        assert set(tools) == {"web_search", "web_extract"}, tools
        for name, meta in tools.items():
            assert meta["name"] == name and callable(meta["func"]), meta
            schema = meta["schema"]
            assert set(schema) == {"type", "properties", "required"}, schema
            assert schema["type"] == "object" and schema["required"], schema
        assert build_tools().keys() == build_tools(None).keys()
        print("[PASS] build_tools returns registry-shaped web_search/web_extract dicts.")
    finally:
        web_tools.SEARCH_ENDPOINT = original_endpoint
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(tmp, ignore_errors=True)

    print("ALL WEB TOOL TESTS PASSED.")


if __name__ == "__main__":
    test_web_tools()
