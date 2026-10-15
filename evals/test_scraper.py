"""Offline tests for the scraper toolkit. No network: all HTML comes from
scraper._HTML_FIXTURES / _BROWSER_FIXTURES. Run: python3 evals/test_scraper.py
"""

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# repo layout: evals/test_scraper.py, agent/src is the import root
sys.path.insert(0, str(HERE.parent / "agent" / "src"))

import tools.scraper as scraper
from tools.scraper import build_tools, scrape_list, scrape_product
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


PAGE1 = """
<html><head><title>Shop - page 1</title></head><body>
<div class="product" data-id="p1">
  <h2 class="name">Ao thun trang</h2>
  <span class="price">199.000d</span>
  <img class="thumb" src="/img/p1.jpg"/>
</div>
<div class="product" data-id="p2">
  <h2 class="name">Quan jean xanh</h2>
  <span class="price">450.000d</span>
  <img class="thumb" src="/img/p2.jpg"/>
</div>
<a class="next" rel="next" href="/shop?page=2">Next</a>
</body></html>
"""

PAGE2 = """
<html><head><title>Shop - page 2</title></head><body>
<div class="product" data-id="p3">
  <h2 class="name">Giay the thao</h2>
  <span class="price">899.000d</span>
  <img class="thumb" src="/img/p3.jpg"/>
</div>
</body></html>
"""

DETAIL = """
<html><head><title>Ao thun trang</title></head><body>
<h1 id="title">Ao thun trang</h1>
<div class="price-box"><span class="price">199.000d</span></div>
<img id="main-img" src="/img/p1-large.jpg"/>
<div class="desc"><p>Chat lieu cotton 100%.</p><p>Made in Vietnam.</p></div>
</body></html>
"""

SPA_SHELL = """
<html><head><title>Shop</title><script src="/bundle.js"></script></head>
<body><div id="root"></div><script>renderApp();</script></body></html>
"""

SPA_RENDERED = """
<html><body><div id="root">
<div class="product"><h2 class="name">Rendered item</h2><span class="price">10d</span></div>
</div></body></html>
"""

CAPTCHA_PAGE = """
<html><head><title>Just a moment...</title></head><body>
<div class="cf-challenge">Please complete the captcha to continue.</div>
</body></html>
"""

FIELDS = {"name": ".name", "price": ".price", "img": ".thumb@src"}


def test_list_two_pages():
    reset()
    scraper._HTML_FIXTURES["https://shop.test/shop"] = PAGE1
    scraper._HTML_FIXTURES["https://shop.test/shop?page=2"] = PAGE2
    res = scrape_list(
        "https://shop.test/shop", ".product", FIELDS,
        next_selector='a[rel="next"]', delay=0, use_browser="never",
    )
    check("list no error", "error" not in res, res)
    check("list 3 items", len(res["items"]) == 3, res["items"])
    check("list pages", res["pages_fetched"] == 2, res)
    first = res["items"][0]
    check("field name", first["name"] == "Ao thun trang", first)
    check("field price", first["price"] == "199.000d", first)
    check("field img @src", first["img"] == "/img/p1.jpg", first)
    check("relative href resolved", res["items"][2]["_source"] == "https://shop.test/shop?page=2",
          res["items"][2])


def test_list_max_pages_truncates():
    reset()
    scraper._HTML_FIXTURES["https://shop.test/shop"] = PAGE1
    scraper._HTML_FIXTURES["https://shop.test/shop?page=2"] = PAGE2
    res = scrape_list(
        "https://shop.test/shop", ".product", FIELDS,
        max_pages=1, next_selector='a[rel="next"]', delay=0, use_browser="never",
    )
    check("truncated flag", res.get("truncated") is True, res)
    check("truncated items", len(res["items"]) == 2, res)


def test_product_detail():
    reset()
    scraper._HTML_FIXTURES["https://shop.test/p1"] = DETAIL
    res = scrape_product(
        "https://shop.test/p1",
        {"title": "#title", "price": ".price-box .price",
         "image": "#main-img@src", "desc": ".desc"},
        delay=0, use_browser="never",
    )
    check("product no error", "error" not in res, res)
    d = res["data"]
    check("product title", d["title"] == "Ao thun trang", d)
    check("product price descendant", d["price"] == "199.000d", d)
    check("product img attr", d["image"] == "/img/p1-large.jpg", d)
    check("product desc text", "cotton 100%" in d["desc"], d)


def test_bad_item_selector():
    reset()
    scraper._HTML_FIXTURES["https://shop.test/shop"] = PAGE1
    res = scrape_list("https://shop.test/shop", ".nope", FIELDS, delay=0, use_browser="never")
    check("bad item selector errors", "error" in res and ".nope" in res["error"], res)


def test_bad_field_selector():
    reset()
    scraper._HTML_FIXTURES["https://shop.test/shop"] = PAGE1
    res = scrape_list(
        "https://shop.test/shop", ".product",
        {"name": ".name", "nope": ".does-not-exist"},
        delay=0, use_browser="never",
    )
    check("bad field selector names it", "error" in res and ".does-not-exist" in res["error"], res)


def test_bad_product_selector():
    reset()
    scraper._HTML_FIXTURES["https://shop.test/p1"] = DETAIL
    res = scrape_product("https://shop.test/p1", {"x": ".missing"}, delay=0, use_browser="never")
    check("product bad selector", "error" in res and ".missing" in res["error"], res)


def test_429_backoff_then_stop():
    reset()
    scraper._HTML_FIXTURES["https://shop.test/shop"] = WebError("HTTP 429 for https://shop.test/shop")
    t0 = time.monotonic()
    res = scrape_list("https://shop.test/shop", ".product", FIELDS, delay=0, use_browser="never")
    dt = time.monotonic() - t0
    check("429 returns error", "error" in res and "429" in res["error"], res)
    check("429 keeps partial", res["items"] == [] and res["pages_fetched"] == 0, res)
    check("429 no infinite loop", dt < 5, f"took {dt:.1f}s")


def test_429_then_success():
    reset()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise WebError("HTTP 429 for https://shop.test/shop")
        return PAGE1

    scraper._HTML_FIXTURES["https://shop.test/shop"] = flaky
    res = scrape_list("https://shop.test/shop", ".product", FIELDS, delay=0, use_browser="never")
    check("429 retry succeeds", "error" not in res and len(res["items"]) == 2, res)


def test_captcha_blocked():
    reset()
    scraper._HTML_FIXTURES["https://shop.test/shop"] = CAPTCHA_PAGE
    res = scrape_list("https://shop.test/shop", ".product", FIELDS, delay=0, use_browser="never")
    check("captcha blocked", "error" in res and res["error"].startswith("blocked:"), res)


def test_403_blocked():
    reset()
    scraper._HTML_FIXTURES["https://shop.test/shop"] = WebError("HTTP 403 for https://shop.test/shop")
    res = scrape_list("https://shop.test/shop", ".product", FIELDS, delay=0, use_browser="never")
    check("403 blocked", "error" in res and "blocked" in res["error"], res)


def test_browser_fallback():
    reset()
    scraper._HTML_FIXTURES["https://shop.test/spa"] = SPA_SHELL
    scraper._BROWSER_FIXTURES["https://shop.test/spa"] = SPA_RENDERED
    res = scrape_list("https://shop.test/spa", ".product", {"name": ".name", "price": ".price"},
                      delay=0, use_browser="auto")
    check("browser fallback items", "error" not in res and len(res["items"]) == 1, res)
    check("browser fallback name", res["items"][0]["name"] == "Rendered item", res)


def test_browser_never_no_fallback():
    reset()
    scraper._HTML_FIXTURES["https://shop.test/spa"] = SPA_SHELL
    scraper._BROWSER_FIXTURES["https://shop.test/spa"] = SPA_RENDERED
    res = scrape_list("https://shop.test/spa", ".product", {"name": ".name"},
                      delay=0, use_browser="never")
    check("browser=never errors", "error" in res, res)


def test_politeness_delay():
    reset()
    scraper._HTML_FIXTURES["https://shop.test/shop"] = PAGE1
    scraper._HTML_FIXTURES["https://shop.test/shop?page=2"] = PAGE2
    sleeps = []
    orig_sleep = time.sleep
    scraper.time.sleep = lambda s: sleeps.append(s)
    try:
        scrape_list("https://shop.test/shop", ".product", FIELDS,
                    next_selector='a[rel="next"]', delay=1.5, use_browser="never")
    finally:
        scraper.time.sleep = orig_sleep
    check("delay between pages", sleeps == [1.5], sleeps)


def test_validation():
    reset()
    check("empty url", "error" in scrape_list("", ".p", FIELDS))
    check("empty fields", "error" in scrape_list("https://x.test", ".p", {}))
    check("bad use_browser", "error" in scrape_list("https://x.test", ".p", FIELDS, use_browser="sometimes"))
    check("bad max_pages", "error" in scrape_list("https://x.test", ".p", FIELDS, max_pages="many"))
    check("product empty schema", "error" in scrape_product("https://x.test", {}))


def test_build_tools_shape():
    tools = build_tools()
    check("six tools", set(tools) == {"scrape_list", "scrape_product", "scrape_table", "crawl_site", "read_sitemap", "watch_url"}, set(tools))
    for name, meta in tools.items():
        check(f"{name} shape",
              meta["name"] == name and callable(meta["func"])
              and set(meta["schema"]) == {"type", "properties", "required"},
              meta)
    check("required fields",
          tools["scrape_list"]["schema"]["required"] == ["start_url", "item_selector", "fields"]
          and tools["scrape_product"]["schema"]["required"] == ["url", "schema"])


def test_selector_engine():
    from tools.scraper import _parse_html, _select
    root = _parse_html(
        '<div id="main"><p class="a b">hi</p><a href="/x" data-id="1">link</a></div>')
    check("tag", len(_select(root, "p")) == 1)
    check(".class", len(_select(root, ".a")) == 1)
    check("tag.class", len(_select(root, "p.b")) == 1)
    check("#id", len(_select(root, "#main")) == 1)
    check("[attr]", len(_select(root, "[data-id]")) == 1)
    check('[attr="v"]', len(_select(root, '[data-id="1"]')) == 1)
    check("descendant", len(_select(root, "#main .b")) == 1)
    check("no match", _select(root, ".zzz") == [])
    try:
        _select(root, "div >> p")
        check("bad selector raises", False)
    except ValueError:
        check("bad selector raises", True)


if __name__ == "__main__":
    test_list_two_pages()
    test_list_max_pages_truncates()
    test_product_detail()
    test_bad_item_selector()
    test_bad_field_selector()
    test_bad_product_selector()
    test_429_backoff_then_stop()
    test_429_then_success()
    test_captcha_blocked()
    test_403_blocked()
    test_browser_fallback()
    test_browser_never_no_fallback()
    test_politeness_delay()
    test_validation()
    test_build_tools_shape()
    test_selector_engine()
    print(f"\nALL SCRAPER TESTS PASSED ({PASS} checks)")
