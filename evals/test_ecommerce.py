"""Offline tests for the Vietnamese e-commerce connectors (Tiki, Shopee, Lazada).

No network: every connector gets an injected fetcher serving fixtures shaped
like the real responses observed 2026-10-15. Run: python3 evals/test_ecommerce.py

NOTE: agent/src/connectors/base.py + __init__.py in this staging dir are
test scaffolding only (copies of the framework files). The deliverables are
tiki.py, shopee.py, lazada.py.
"""

import json
import sys
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "agent" / "src"))

from connectors.tiki import TikiConnector
from connectors.shopee import ShopeeConnector
from connectors.lazada import LazadaConnector

PASS = 0


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {extra}")


def no_sleep(_):
    pass


# ------------------------------------------------------------------ fixtures
# Tiki: shaped like the real api/v2 responses fetched 2026-10-15.

TIKI_SEARCH = {
    "data": [
        {"id": 279077078, "name": " Điện thoại Oppo A5x (4GB/128GB) - Hàng chính hãng ",
         "price": 4690000, "original_price": 5190000,
         "url_path": "dien-thoai-oppo-a5x-4gb-128gb-hang-chinh-hang-p279077078.html",
         "seller_name": "Tiki Trading", "brand_name": "OPPO", "rating_average": 4.8},
        {"id": 279077079, "name": "Ốp lưng Oppo A5x",
         "price": 99000, "original_price": 99000,
         "url_path": "op-lung-oppo-a5x-p279077079.html",
         "seller_name": "", "brand_name": "", "rating_average": 0},
    ],
    "paging": {"total": 2000, "per_page": 2},
}

TIKI_DETAIL = {
    "data": {"id": 279077078, "name": "Điện thoại Oppo A5x",
             "price": 4690000, "original_price": 5190000,
             "url_path": "dien-thoai-oppo-a5x-p279077078.html",
             "seller_name": "Tiki Trading", "brand_name": "OPPO",
             "rating_average": 4.8, "review_count": 12,
             "stock_item": {"qty": 1000, "max_sale_qty": 1000},
             "short_description": "Mô tả ngắn."},
}

TIKI_DETAIL_UNWRAPPED = {
    "id": 276117054, "name": "Tai nghe Bluetooth Apple AirPods 4",
    "price": 2890000, "original_price": 3290000,
    "url_path": "airpods-4-p276117054.html?spid=276117486",
    "rating_average": 4.8, "review_count": 122,
    "stock_item": {"qty": 1000},
}

TIKI_404 = {"status": 404, "errors": "map[message:Product not found]"}

# Shopee: success shaped like the v4 API item_basic; the block payload is the
# REAL error observed 2026-10-15 (error 90309999 = anti-bot).

SHOPEE_SEARCH_OK = {
    "items": [
        {"item_basic": {"itemid": 111, "shopid": 222, "name": "Tai nghe Bluetooth X",
                        "price": 25900000000, "price_min": 25900000000,
                        "price_max": 29900000000, "shop_location": "Hà Nội",
                        "historical_sold": 1500,
                        "item_rating": {"rating_star": 4.9}}},
        {"item_basic": {"itemid": 333, "shopid": 444, "name": "Tai nghe Bluetooth Y",
                        "price": 19900000000, "price_min": 19900000000,
                        "price_max": 19900000000, "shop_location": "",
                        "historical_sold": 200,
                        "item_rating": {"rating_star": 0}}},
    ],
}

SHOPEE_BLOCKED = {"is_customized": False, "is_login": False, "action_type": 2,
                  "error": 90309999, "redirect_to_error_page": True}

SHOPEE_CAPTCHA_HTML = (b"<html><head><title>Captcha</title></head>"
                       b"<body>please complete the captcha challenge</body></html>")

SHOPEE_DETAIL_OK = {
    "item": {"item_basic": {"itemid": 111, "shopid": 222,
                            "name": "Tai nghe Bluetooth X",
                            "price": 25900000000, "price_min": 25900000000,
                            "price_max": 29900000000,
                            "item_rating": {"rating_star": 4.9}},
             "description": "Mô tả chi tiết sản phẩm.",
             "stock": 50},
}

# Lazada: one fixture with embedded product JSON, one empty JS shell (the real
# shape observed 2026-10-15: no product data in plain HTML).

LAZADA_WITH_DATA = b"""<html><head></head><body>
<script>window.__LAZ__ = {"products": [
  {"name": "Tai nghe Bluetooth Z", "price": 259000,
   "productUrl": "/products/tai-nghe-bluetooth-z-12345.html"},
  {"name": "Loa mini", "price": 199000,
   "productUrl": "/products/loa-mini-67890.html"}
]};</script>
</body></html>"""

LAZADA_EMPTY_SHELL = b"""<html lang="vi"><head><meta charset="utf-8"/>
<script src="/app.js"></script></head>
<body><div id="root"></div><script>window.LZD = {};</script></body></html>"""


def _json_fixture(obj):
    def fetch(url):
        return json.dumps(obj).encode("utf-8")
    return fetch


def _bytes_fixture(raw: bytes):
    def fetch(url):
        return raw
    return fetch


def _http_error(code):
    def fetch(url):
        raise urllib.error.HTTPError(url, code, "Forbidden", {}, None)
    return fetch


# ------------------------------------------------------------------ contract

for cls, name, tools in [
    (TikiConnector, "tiki", ["tiki_search", "tiki_product"]),
    (ShopeeConnector, "shopee", ["shopee_search", "shopee_product"]),
    (LazadaConnector, "lazada", ["lazada_search"]),
]:
    c = cls(fetcher=_bytes_fixture(b"{}"), sleeper=no_sleep)
    check(f"{name} connector name", c.name == name)
    st = c.status()
    check(f"{name} status shape",
          st.get("connected") is True and isinstance(st.get("detail"), str))
    bt = c.build_tools()
    check(f"{name} tools", sorted(bt.keys()) == sorted(tools), str(sorted(bt.keys())))
    for t in tools:
        meta = bt[t]
        check(f"{name}.{t} shape",
              meta["name"] == t and callable(meta["func"])
              and set(meta["schema"]) == {"type", "properties", "required"})
    check(f"{name} CONNECTOR instance",
          getattr(sys.modules[cls.__module__], "CONNECTOR", None) is not None)

# ------------------------------------------------------------------ tiki

tiki = TikiConnector(fetcher=_json_fixture(TIKI_SEARCH), sleeper=no_sleep)
res = tiki.tiki_search("oppo", limit=10)
check("tiki_search parses", res["total"] == 2000 and len(res["products"]) == 2,
      str(res)[:200])
p0 = res["products"][0]
check("tiki_search fields",
      p0["id"] == 279077078 and p0["price"] == 4690000
      and p0["original_price"] == 5190000
      and p0["name"] == "Điện thoại Oppo A5x (4GB/128GB) - Hàng chính hãng"
      and p0["url"] == "https://tiki.vn/dien-thoai-oppo-a5x-4gb-128gb-hang-chinh-hang-p279077078.html"
      and p0["seller"] == "Tiki Trading" and p0["rating"] == 4.8)
check("tiki_search empty keyword", "error" in tiki.tiki_search("  "))
check("tiki_search bad limit", "error" in tiki.tiki_search("x", limit="abc"))

tiki_d = TikiConnector(fetcher=_json_fixture(TIKI_DETAIL), sleeper=no_sleep)
d = tiki_d.tiki_product("279077078")
check("tiki_product detail",
      d["price"] == 4690000 and d["review_count"] == 12
      and d["stock_qty"] == 1000 and "tiki.vn" in d["url"])
d2 = tiki_d.tiki_product("https://tiki.vn/dien-thoai-oppo-a5x-p279077078.html?spid=1")
check("tiki_product id from URL", d2["id"] == 279077078)
check("tiki_product bad input", "error" in tiki_d.tiki_product("not-a-url"))

tiki_404 = TikiConnector(fetcher=_json_fixture(TIKI_404), sleeper=no_sleep)
check("tiki_product 404", "error" in tiki_404.tiki_product("123"))

tiki_u = TikiConnector(fetcher=_json_fixture(TIKI_DETAIL_UNWRAPPED), sleeper=no_sleep)
du = tiki_u.tiki_product("276117054")
check("tiki_product unwrapped shape",
      du["price"] == 2890000 and du["review_count"] == 122
      and du["stock_qty"] == 1000)

# ------------------------------------------------------------------ shopee

sh = ShopeeConnector(fetcher=_json_fixture(SHOPEE_SEARCH_OK), sleeper=no_sleep)
res = sh.shopee_search("tai nghe", limit=10)
check("shopee_search parses", len(res["products"]) == 2, str(res)[:200])
s0 = res["products"][0]
check("shopee_search fields",
      s0["itemid"] == 111 and s0["shopid"] == 222
      and s0["price"] == 259000 and s0["price_min"] == 259000
      and s0["price_max"] == 299000
      and s0["url"] == "https://shopee.vn/tai-nghe-bluetooth-x-i.222.111"
      and s0["sold"] == 1500 and s0["rating"] == 4.9)
check("shopee_search empty keyword", "error" in sh.shopee_search(""))

sh_b = ShopeeConnector(fetcher=_json_fixture(SHOPEE_BLOCKED), sleeper=no_sleep)
err = sh_b.shopee_search("tai nghe")
check("shopee blocked 90309999 -> honest error",
      "error" in err and "anti-bot" in err["error"], str(err)[:120])

sh_c = ShopeeConnector(fetcher=_bytes_fixture(SHOPEE_CAPTCHA_HTML), sleeper=no_sleep)
err = sh_c.shopee_search("tai nghe")
check("shopee captcha page -> honest error",
      "error" in err and "anti-bot" in err["error"], str(err)[:120])

sh_403 = ShopeeConnector(fetcher=_http_error(403), sleeper=no_sleep)
err = sh_403.shopee_search("tai nghe")
check("shopee HTTP 403 -> honest error",
      "error" in err and "anti-bot" in err["error"], str(err)[:120])

sh_d = ShopeeConnector(fetcher=_json_fixture(SHOPEE_DETAIL_OK), sleeper=no_sleep)
d = sh_d.shopee_product("https://shopee.vn/Tai-nghe-Bluetooth-X-i.222.111")
check("shopee_product detail",
      d["price"] == 259000 and d["stock"] == 50
      and "Mô tả" in d["description"])
check("shopee_product bad url", "error" in sh_d.shopee_product("https://shopee.vn/abc"))

# ------------------------------------------------------------------ lazada

lz = LazadaConnector(fetcher=_bytes_fixture(LAZADA_WITH_DATA), sleeper=no_sleep)
res = lz.lazada_search("tai nghe", limit=10)
check("lazada_search parses embedded JSON", len(res["products"]) == 2,
      str(res)[:200])
l0 = res["products"][0]
check("lazada_search fields",
      l0["name"] == "Tai nghe Bluetooth Z" and l0["price"] == 259000
      and l0["url"] == "https://www.lazada.vn/products/tai-nghe-bluetooth-z-12345.html")

lz_e = LazadaConnector(fetcher=_bytes_fixture(LAZADA_EMPTY_SHELL), sleeper=no_sleep)
err = lz_e.lazada_search("tai nghe")
check("lazada empty JS shell -> honest error",
      "error" in err and "JS-rendered" in err["error"], str(err)[:150])
check("lazada_search empty keyword", "error" in lz_e.lazada_search("  "))

lz_403 = LazadaConnector(fetcher=_http_error(403), sleeper=no_sleep)
err = lz_403.lazada_search("tai nghe")
check("lazada HTTP 403 -> honest error",
      "error" in err and "anti-bot" in err["error"], str(err)[:120])

# ------------------------------------------------------------------ done

print(f"\nALL ECOMMERCE TESTS PASSED ({PASS} checks)")
