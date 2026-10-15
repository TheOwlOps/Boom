"""Offline tests for the 4 new scraper tools. No network: HTML comes from
scraper._HTML_FIXTURES, sitemaps from scraper._SITEMAP_FIXTURES.
Run: python3 evals/test_scraper2.py
"""

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# repo layout: evals/test_scraper2.py, agent/src is the import root
sys.path.insert(0, str(HERE.parent / "agent" / "src"))

import tools.scraper as scraper
from tools.scraper import (
    build_tools,
    crawl_site,
    read_sitemap,
    scrape_table,
    watch_url,
)
from tools.web_tools import WebError

PASS = 0


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {extra}")
        raise AssertionError(name)


def reset():
    scraper._HTML_FIXTURES.clear()
    scraper._BROWSER_FIXTURES.clear()
    scraper._SITEMAP_FIXTURES.clear()
    scraper._WATCH_ROOT = None


TABLE_HTML = """<html><body>
<table id="prices">
<tr><th>Name</th><th>Price</th><th>Stock</th></tr>
<tr><td>Apple</td><td>10</td><td>5</td></tr>
<tr><td>Banana</td><td colspan="2">7</td></tr>
</table>
<table id="empty"></table>
</body></html>"""

NOTABLE_HTML = "<html><body><p>no tables here</p></body></html>"

SITE_INDEX = """<html><head><title>Home</title></head><body>
<a href="/a">A</a><a href="/b">B</a>
<a href="https://other.test/x">External</a>
<a href="/a">A again</a>
</body></html>"""
SITE_A = """<html><head><title>Page A</title></head><body>
<a href="/b">B from A</a></body></html>"""
SITE_B = """<html><head><title>Page B</title></head><body><p>leaf</p></body></html>"""

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://site.test/p1</loc><lastmod>2026-10-01</lastmod></url>
<url><loc>https://site.test/p2</loc></url>
</urlset>"""
SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://site.test/sitemap-a.xml</loc></sitemap>
<sitemap><loc>https://site.test/sitemap-b.xml</loc></sitemap>
</sitemapindex>"""
SITEMAP_A = """<?xml version="1.0" encoding="UTF-8"?>
<urlset><url><loc>https://site.test/a1</loc></url></urlset>"""
SITEMAP_B = """<?xml version="1.0" encoding="UTF-8"?>
<urlset><url><loc>https://site.test/b1</loc><lastmod>2026-09-01</lastmod></url></urlset>"""

WATCH_HTML_100 = '<html><body><div class="price">100</div><div class="other">x</div></body></html>'
WATCH_HTML_120 = '<html><body><div class="price">120</div><div class="other">x</div></body></html>'


def test_scrape_table():
    reset()
    scraper._HTML_FIXTURES["https://t.test/table"] = TABLE_HTML
    scraper._HTML_FIXTURES["https://t.test/notable"] = NOTABLE_HTML

    res = scrape_table("https://t.test/table", "#prices", use_browser="never", delay=0)
    check("table: one table with data", len(res["tables"]) == 1, res)
    t = res["tables"][0]
    check("table: headers", t["headers"] == ["Name", "Price", "Stock"], t["headers"])
    check("table: row count", len(t["rows"]) == 2, t["rows"])
    check("table: row1", t["rows"][0] == {"Name": "Apple", "Price": "10", "Stock": "5"}, t["rows"][0])
    # colspan="2" expands the value across Price and Stock
    check("table: colspan expanded",
          t["rows"][1] == {"Name": "Banana", "Price": "7", "Stock": "7"}, t["rows"][1])

    # default selector matches every table; the empty one comes back honest
    res_all = scrape_table("https://t.test/table", use_browser="never", delay=0)
    check("table: all tables", len(res_all["tables"]) == 2, res_all["tables"])
    check("table: empty table honest",
          res_all["tables"][1] == {"headers": [], "rows": []}, res_all["tables"][1])

    # no header mode
    res3 = scrape_table("https://t.test/table", has_header=False, use_browser="never", delay=0)
    h3 = res3["tables"][0]["headers"]
    check("table: no-header cols", h3 == ["col1", "col2", "col3"], h3)
    check("table: no-header keeps header row as data",
          res3["tables"][0]["rows"][0]["col1"] == "Name", res3["tables"][0]["rows"][0])

    # error paths
    e1 = scrape_table("https://t.test/notable", use_browser="never", delay=0)
    check("table: no table -> error", e1.get("error", "").startswith("no table found"), e1)
    e2 = scrape_table("https://t.test/table", "!!!", use_browser="never", delay=0)
    check("table: bad selector -> error", "unsupported" in e2.get("error", ""), e2)
    e3 = scrape_table("", use_browser="never")
    check("table: empty url -> error", "error" in e3, e3)
    e4 = scrape_table("https://t.test/table", has_header="yes", use_browser="never", delay=0)
    check("table: bad has_header -> error", "error" in e4, e4)


def test_crawl_site():
    reset()
    scraper._HTML_FIXTURES["https://site.test/"] = SITE_INDEX
    scraper._HTML_FIXTURES["https://site.test/a"] = SITE_A
    scraper._HTML_FIXTURES["https://site.test/b"] = SITE_B

    res = crawl_site("https://site.test/", use_browser="never", delay=0)
    urls = [p["url"] for p in res["pages"]]
    check("crawl: 3 pages", len(urls) == 3, urls)
    check("crawl: external skipped", "https://other.test/x" not in urls, urls)
    check("crawl: dedupe /a", urls.count("https://site.test/a") == 1, urls)
    titles = {p["url"]: p["title"] for p in res["pages"]}
    check("crawl: titles", titles["https://site.test/"] == "Home"
          and titles["https://site.test/a"] == "Page A", titles)
    check("crawl: pages_fetched", res["pages_fetched"] == 3, res)

    # url_pattern filter
    res2 = crawl_site("https://site.test/", url_pattern=r"/b$", use_browser="never", delay=0)
    urls2 = [p["url"] for p in res2["pages"]]
    check("crawl: pattern filter", urls2 == ["https://site.test/", "https://site.test/b"], urls2)

    # max_depth=0 -> start page only
    res3 = crawl_site("https://site.test/", max_depth=0, use_browser="never", delay=0)
    check("crawl: max_depth=0", len(res3["pages"]) == 1, res3["pages"])

    # max_pages cap
    res4 = crawl_site("https://site.test/", max_pages=2, use_browser="never", delay=0)
    check("crawl: max_pages=2", len(res4["pages"]) == 2, res4["pages"])

    # error paths
    e1 = crawl_site("https://site.test/", url_pattern="([", use_browser="never", delay=0)
    check("crawl: bad regex -> error", "valid regex" in e1.get("error", ""), e1)
    e2 = crawl_site("", use_browser="never")
    check("crawl: empty url -> error", "error" in e2, e2)

    # 429 on a child page: crawl stops with partial results
    reset()
    scraper._HTML_FIXTURES["https://site.test/"] = SITE_INDEX
    def seq429():
        raise WebError("HTTP 429 for https://site.test/a")
    scraper._HTML_FIXTURES["https://site.test/a"] = seq429
    scraper._HTML_FIXTURES["https://site.test/b"] = SITE_B
    res5 = crawl_site("https://site.test/", use_browser="never", delay=0)
    check("crawl: 429 child -> partial + error",
          "error" in res5 and "rate limited" in res5["error"]
          and len(res5["pages"]) == 1, res5)


def test_read_sitemap():
    reset()
    scraper._SITEMAP_FIXTURES["https://site.test/sitemap.xml"] = SITEMAP_XML
    scraper._SITEMAP_FIXTURES["https://site.test/sitemap-index.xml"] = SITEMAP_INDEX
    scraper._SITEMAP_FIXTURES["https://site.test/sitemap-a.xml"] = SITEMAP_A
    scraper._SITEMAP_FIXTURES["https://site.test/sitemap-b.xml"] = SITEMAP_B
    scraper._SITEMAP_FIXTURES["https://site.test/bad.xml"] = "<not xml"

    res = read_sitemap("https://site.test/sitemap.xml")
    check("sitemap: 2 urls", len(res["urls"]) == 2, res["urls"])
    check("sitemap: lastmod", res["urls"][0] == {"url": "https://site.test/p1", "lastmod": "2026-10-01"},
          res["urls"][0])
    check("sitemap: missing lastmod -> empty", res["urls"][1]["lastmod"] == "", res["urls"][1])
    check("sitemap: not truncated", res["truncated"] is False, res)

    res2 = read_sitemap("https://site.test/sitemap-index.xml")
    got = sorted(u["url"] for u in res2["urls"])
    check("sitemap: index recursion", got == ["https://site.test/a1", "https://site.test/b1"], got)

    e1 = read_sitemap("https://site.test/bad.xml")
    check("sitemap: bad xml -> error", "invalid sitemap XML" in e1.get("error", ""), e1)
    e2 = read_sitemap("")
    check("sitemap: empty url -> error", "error" in e2, e2)
    scraper._SITEMAP_FIXTURES["https://site.test/feed.xml"] = "<rss></rss>"
    e3 = read_sitemap("https://site.test/feed.xml")
    check("sitemap: non-sitemap root -> error", "not a sitemap" in e3.get("error", ""), e3)


def test_watch_url():
    reset()
    scraper._HTML_FIXTURES["https://site.test/watch"] = WATCH_HTML_100
    with tempfile.TemporaryDirectory() as tmp:
        # first run: watching
        r1 = watch_url("https://site.test/watch", ".price", state_dir=tmp,
                       use_browser="never", delay=0)
        check("watch: first run watching", r1.get("status") == "watching" and "hash" in r1, r1)
        h1 = r1["hash"]
        # state file written atomically
        state_path = Path(tmp) / "watch.json"
        check("watch: state file exists", state_path.exists())
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        check("watch: state has hash", list(saved.values())[0]["hash"] == h1, saved)

        # second run, same content: unchanged
        r2 = watch_url("https://site.test/watch", ".price", state_dir=tmp,
                       use_browser="never", delay=0)
        check("watch: unchanged", r2.get("changed") is False and r2["prev_hash"] == h1, r2)

        # content changes: changed=true with prev_hash
        scraper._HTML_FIXTURES["https://site.test/watch"] = WATCH_HTML_120
        r3 = watch_url("https://site.test/watch", ".price", state_dir=tmp,
                       use_browser="never", delay=0)
        check("watch: changed", r3.get("changed") is True and r3["prev_hash"] == h1
              and r3["hash"] != h1, r3)

        # whole-page mode (no selector)
        r4 = watch_url("https://site.test/watch", state_dir=tmp, use_browser="never", delay=0)
        check("watch: whole page mode", r4.get("status") == "watching", r4)

    # no state_dir and no env root: honest error, writes nothing surprising
    reset()
    scraper._HTML_FIXTURES["https://site.test/watch"] = WATCH_HTML_100
    e1 = watch_url("https://site.test/watch", ".price", use_browser="never", delay=0)
    check("watch: no state_dir+no root -> error", "state_dir" in e1.get("error", ""), e1)

    # build_tools(env) captures root_dir -> default memories/ works
    with tempfile.TemporaryDirectory() as tmp:
        class FakeEnv:
            root_dir = tmp
        scraper.build_tools(FakeEnv())
        r5 = watch_url("https://site.test/watch", ".price", use_browser="never", delay=0)
        check("watch: env root_dir default", r5.get("status") == "watching", r5)
        check("watch: default under memories",
              (Path(tmp) / "memories" / "watch.json").exists())
    reset()

    # bad selector
    with tempfile.TemporaryDirectory() as tmp:
        e2 = watch_url("https://site.test/watch", "!!!", state_dir=tmp,
                       use_browser="never", delay=0)
        check("watch: bad selector -> error", "unsupported" in e2.get("error", ""), e2)


def test_build_tools_shape():
    reset()
    tools = build_tools()
    check("tools: 6 tools", set(tools) == {
        "scrape_list", "scrape_product", "scrape_table",
        "crawl_site", "read_sitemap", "watch_url",
    }, sorted(tools))
    for name, meta in tools.items():
        check(f"tools: {name} shape",
              meta["name"] == name and callable(meta["func"])
              and set(meta["schema"]) == {"type", "properties", "required"}, meta)
    check("tools: scrape_table required", build_tools()["scrape_table"]["schema"]["required"] == ["url"])
    check("tools: crawl_site required", build_tools()["crawl_site"]["schema"]["required"] == ["start_url"])
    check("tools: read_sitemap required", build_tools()["read_sitemap"]["schema"]["required"] == ["sitemap_url"])
    check("tools: watch_url required", build_tools()["watch_url"]["schema"]["required"] == ["url"])


if __name__ == "__main__":
    test_scrape_table()
    test_crawl_site()
    test_read_sitemap()
    test_watch_url()
    test_build_tools_shape()
    print(f"\nALL SCRAPER2 TESTS PASSED ({PASS} checks)")
