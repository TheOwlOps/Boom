"""Offline tests for the news connector: no network, fixture RSS in VnExpress format.

Run: python3 evals/test_news.py
"""

import importlib.util
import sys
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Framework's agent/src provides `connectors.base` (owned by connectors-framework worker).
sys.path.insert(0, str(Path.home() / "workspace/boom-mods/connectors-framework/agent/src"))

# Load news.py from its file location as connectors.news (avoids clobbering the
# framework's connectors package on sys.path).
NEWS_PY = HERE.parent / "agent" / "src" / "connectors" / "news.py"
spec = importlib.util.spec_from_file_location("connectors.news", NEWS_PY)
news_mod = importlib.util.module_from_spec(spec)
sys.modules["connectors.news"] = news_mod
spec.loader.exec_module(news_mod)

NewsConnector = news_mod.NewsConnector
CONNECTOR = news_mod.CONNECTOR
FEEDS = news_mod.FEEDS

RSS_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
<channel>
<title>Tin m&#7899;i nh&#7845;t - VnExpress</title>
<link>https://vnexpress.net</link>
<item>
<title>Kinh t&#7871; Vi&#7879;t Nam t&#259;ng tr&#432;&#7903;ng qu&#253; 3</title>
<description><![CDATA[<a href="https://vnexpress.net/kinh-te-1"><img src="https://i.vnexpress.net/y.jpg"></a>T&#259;ng tr&#432;&#7903;ng GDP qu&#253; 3 &#273;&#7841;t m&#7913;c cao v&#7899;i nhi&#7873;u t&#237;n hi&#7879;u t&#237;ch c&#7921;c t&#7915; xu&#7845;t kh&#7849;u v&#224; &#273;&#7847;u t&#432; c&#244;ng.]]></description>
<pubDate>Thu, 15 Oct 2026 07:00:00 +0700</pubDate>
<link>https://vnexpress.net/kinh-te-1</link>
<guid>1</guid>
</item>
<item>
<title>&#272;&#7897;i tuy&#7875;n b&#243;ng &#273;&#225; th&#7855;ng tr&#7853;n giao h&#7919;u</title>
<description><![CDATA[Chi&#7871;n th&#7855;ng 2-0 trong tr&#7853;n giao h&#7919;u t&#7889;i qua.]]></description>
<pubDate>Thu, 15 Oct 2026 06:30:00 +0700</pubDate>
<link>https://vnexpress.net/the-thao-1</link>
<guid>2</guid>
</item>
<item>
<title></title>
<description>no title item, must be skipped</description>
<pubDate>Thu, 15 Oct 2026 06:00:00 +0700</pubDate>
<link>https://vnexpress.net/notitle</link>
</item>
</channel>
</rss>
""".encode("utf-8")

BROKEN_XML = b"<rss><channel><item><title>unclosed"


def make_connector(calls=None, payload=RSS_FIXTURE, exc=None):
    def fetcher(url):
        if calls is not None:
            calls.append(url)
        if exc is not None:
            raise exc
        return payload
    return NewsConnector(fetcher=fetcher)


PASS = 0


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        raise SystemExit(f"FAIL {name} {extra}")


# 1. contract
check("name is news", NewsConnector.name == "news")
check("CONNECTOR instance", isinstance(CONNECTOR, NewsConnector))
check("status always connected",
      CONNECTOR.status() == {"connected": True, "detail": "RSS feeds, no API key required"})
tools = CONNECTOR.build_tools()
check("two tools, prefixed",
      set(tools) == {"news_headlines", "news_search"})
for tname, t in tools.items():
    check(f"tool shape {tname}",
          t["name"] == tname and callable(t["func"]) and t["description"]
          and t["schema"]["type"] == "object")

# 2. headlines parse
calls = []
c = make_connector(calls)
res = c.news_headlines()
check("no error", "error" not in res, res)
check("default feed url", calls == [FEEDS["tin-moi-nhat"]], calls)
check("two items (untitled skipped)", len(res["headlines"]) == 2, res)
first = res["headlines"][0]
check("title decoded", first["title"] == "Kinh tế Việt Nam tăng trưởng quý 3", first["title"])
check("link", first["link"] == "https://vnexpress.net/kinh-te-1")
check("pubDate", first["pubDate"] == "Thu, 15 Oct 2026 07:00:00 +0700")
check("summary html stripped",
      first["summary"].startswith("Tăng trưởng GDP quý 3"), first["summary"])
check("summary capped", len(first["summary"]) <= 200)
check("total", res["total"] == 2)

# 3. limit
res = c.news_headlines(limit=1)
check("limit=1", len(res["headlines"]) == 1 and res["total"] == 1)
res = c.news_headlines(limit="abc")
check("bad limit -> error", "error" in res)

# 4. feed keys + custom URL
for key in ("kinh-doanh", "the-thao"):
    calls = []
    c2 = make_connector(calls)
    r = c2.news_headlines(feed=key)
    check(f"feed key {key}", "error" not in r and calls == [FEEDS[key]], (r, calls))
calls = []
c3 = make_connector(calls)
r = c3.news_headlines(feed="https://example.com/custom.rss")
check("custom url", "error" not in r and calls == ["https://example.com/custom.rss"])
r = c3.news_headlines(feed="nope")
check("unknown feed -> error", "error" in r and "unknown feed" in r["error"], r)

# 5. search
c4 = make_connector()
r = c4.news_search("bóng đá")
check("search hit", r["total"] == 1 and "bóng đá" in r["headlines"][0]["title"].lower(), r)
r = c4.news_search("KINH TẾ")
check("search case-insensitive", r["total"] == 1, r)
r = c4.news_search("không tồn tại")
check("search no hit", r["total"] == 0 and r["headlines"] == [])
r = c4.news_search("   ")
check("empty keyword -> error", "error" in r)

# 6. failures are honest
c5 = make_connector(payload=BROKEN_XML)
r = c5.news_headlines()
check("broken xml -> error", "error" in r and "could not parse" in r["error"], r)
c6 = make_connector(exc=TimeoutError("timed out"))
r = c6.news_headlines()
check("fetch timeout -> error", "error" in r and "TimeoutError" in r["error"], r)
c7 = make_connector(exc=urllib.error.URLError("dns fail"))
r = c7.news_search("x")
check("search fetch fail -> error", "error" in r, r)

# 7. registry integration (framework's real registry)
sys.path.insert(0, str(Path.home() / "workspace/boom-mods/connectors-framework/agent/src"))
from connectors.registry import ConnectorRegistry
reg = ConnectorRegistry()
reg.register(CONNECTOR)
check("registry names", reg.names() == ["news"])
check("registry status", reg.status_all()["news"]["connected"] is True)
merged = reg.build_tools()
check("registry merges tools", set(merged) == {"news_headlines", "news_search"})

print(f"\nALL NEWS TESTS PASSED ({PASS} checks)")
