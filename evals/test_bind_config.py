"""Test cho gateway/bind_config.py. Chay: python3 test_bind_config.py"""

import os
import sys

# Cho phep chay truc tiep tu thu muc chua file nay.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bind_config import (
    BindConfig,
    make_cors_headers,
    handle_preflight,
    apply_to_uvicorn_kwargs,
    ALLOW_METHODS,
    ALLOW_HEADERS,
    MAX_AGE,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_CORS_ORIGINS,
)

passed = 0
failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", msg)


def clear_env():
    for k in ("BOOM_HOST", "BOOM_PORT", "BOOM_CORS_ORIGINS"):
        os.environ.pop(k, None)


# --- 1. Hang so mac dinh ---
check(DEFAULT_HOST == "0.0.0.0", "DEFAULT_HOST phai la 0.0.0.0")
check(DEFAULT_PORT == 8788, "DEFAULT_PORT phai la 8788")
check(DEFAULT_CORS_ORIGINS == ["*"], "DEFAULT_CORS_ORIGINS phai la ['*']")
check(ALLOW_METHODS == "GET, POST, PUT, DELETE, OPTIONS", "ALLOW_METHODS sai")
check(ALLOW_HEADERS == "Content-Type, Authorization", "ALLOW_HEADERS sai")
check(MAX_AGE == "86400", "MAX_AGE phai la 86400")

# --- 2. BindConfig defaults ---
c = BindConfig()
check(c.host == "0.0.0.0", "host mac dinh phai la 0.0.0.0")
check(c.port == 8788, "port mac dinh phai la 8788")
check(c.cors_origins == ["*"], "cors_origins mac dinh phai la ['*']")
r = repr(c)
check("0.0.0.0" in r and "8788" in r, "repr phai chua host va port")

# --- 3. BindConfig voi tham so tuong minh ---
c2 = BindConfig(host="192.168.1.10", port="9000", cors_origins=["http://a"])
check(c2.host == "192.168.1.10", "host tuong minh sai")
check(c2.port == 9000 and isinstance(c2.port, int), "port dang str phai ep sang int")
check(c2.cors_origins == ["http://a"], "cors_origins tuong minh sai")

# --- 4. from_env: khong co env -> defaults ---
clear_env()
c3 = BindConfig.from_env()
check(c3.host == "0.0.0.0", "from_env host mac dinh sai")
check(c3.port == 8788, "from_env port mac dinh sai")
check(c3.cors_origins == ["*"], "from_env cors mac dinh sai")

# --- 5. from_env: override day du ---
os.environ["BOOM_HOST"] = "192.168.1.5"
os.environ["BOOM_PORT"] = "9999"
os.environ["BOOM_CORS_ORIGINS"] = "http://phone.local:8080, http://tablet:8080"
c4 = BindConfig.from_env()
check(c4.host == "192.168.1.5", "BOOM_HOST override sai")
check(c4.port == 9999, "BOOM_PORT override sai")
check(
    c4.cors_origins == ["http://phone.local:8080", "http://tablet:8080"],
    "BOOM_CORS_ORIGINS phai split phay va strip space",
)
clear_env()

# --- 6. from_env: BOOM_PORT sai cu phap -> roi ve mac dinh, khong crash ---
os.environ["BOOM_PORT"] = "khong-phai-so"
c5 = BindConfig.from_env()
check(c5.port == 8788, "BOOM_PORT sai cu phap phai roi ve 8788")
os.environ["BOOM_PORT"] = ""
c6 = BindConfig.from_env()
check(c6.port == 8788, "BOOM_PORT rong phai roi ve 8788")
clear_env()

# --- 7. from_env: CORS co muc rong / chuoi rong ---
os.environ["BOOM_CORS_ORIGINS"] = " http://a , ,http://b, "
c7 = BindConfig.from_env()
check(c7.cors_origins == ["http://a", "http://b"], "phai bo muc rong va strip")
os.environ["BOOM_CORS_ORIGINS"] = ""
c8 = BindConfig.from_env()
check(c8.cors_origins == ["*"], "BOOM_CORS_ORIGINS rong phai roi ve ['*']")
clear_env()

# --- 8. make_cors_headers: che do wildcard mac dinh ---
h = make_cors_headers("http://192.168.1.20:5173")
check(h["Access-Control-Allow-Origin"] == "*", "wildcard phai tra '*'")
check(
    h["Access-Control-Allow-Methods"] == "GET, POST, PUT, DELETE, OPTIONS",
    "Allow-Methods thieu",
)
check(
    h["Access-Control-Allow-Headers"] == "Content-Type, Authorization",
    "Allow-Headers thieu Authorization",
)
check(h["Access-Control-Max-Age"] == "86400", "Max-Age phai la 86400")

# --- 9. make_cors_headers: danh sach origin cu the ---
strict = ["http://phone:8080"]
h_ok = make_cors_headers("http://phone:8080", strict)
check(
    h_ok["Access-Control-Allow-Origin"] == "http://phone:8080",
    "origin hop le phai duoc reflect",
)
check(h_ok.get("Vary") == "Origin", "reflect origin phai kem Vary: Origin")
h_bad = make_cors_headers("http://evil.example", strict)
check(
    "Access-Control-Allow-Origin" not in h_bad,
    "origin la phai KHONG co Allow-Origin (fail closed)",
)
h_none = make_cors_headers(None, strict)
check(
    "Access-Control-Allow-Origin" not in h_none,
    "thieu Origin phai KHONG co Allow-Origin",
)

# --- 10. handle_preflight ---
status, ph = handle_preflight({"Origin": "http://192.168.1.20:5173"})
check(status == 204, "preflight phai tra 204")
check(isinstance(ph, dict), "preflight headers phai la dict")
check(ph["Access-Control-Allow-Origin"] == "*", "preflight thieu Allow-Origin")
check(
    ph["Access-Control-Allow-Methods"] == "GET, POST, PUT, DELETE, OPTIONS",
    "preflight thieu Allow-Methods day du",
)
check(ph["Access-Control-Max-Age"] == "86400", "preflight thieu Max-Age")
status2, ph2 = handle_preflight({})
check(status2 == 204, "preflight khong Origin van 204")
check(ph2["Access-Control-Allow-Origin"] == "*", "preflight mac dinh van '*'")

# preflight voi origin bi han che
status3, ph3 = handle_preflight({"Origin": "http://phone:8080"}, strict)
check(
    ph3["Access-Control-Allow-Origin"] == "http://phone:8080",
    "preflight strict phai reflect origin hop le",
)

# --- 11. apply_to_uvicorn_kwargs ---
kw = apply_to_uvicorn_kwargs(BindConfig(host="0.0.0.0", port=8788))
check(kw == {"host": "0.0.0.0", "port": 8788}, "uvicorn kwargs sai")
kw2 = apply_to_uvicorn_kwargs(BindConfig.from_env())
check(kw2["host"] == "0.0.0.0" and kw2["port"] == 8788, "kwargs tu from_env sai")
check(set(kw.keys()) == {"host", "port"}, "kwargs chi duoc co host va port")

# --- 12. Khong chia se list default giua cac instance ---
src = ["http://x"]
c9 = BindConfig(cors_origins=src)
src.append("http://y")
check(c9.cors_origins == ["http://x"], "BindConfig phai copy list dau vao")

print("=" * 50)
print("PASS: %d  FAIL: %d  (tong %d assertions)" % (passed, failed, passed + failed))
sys.exit(1 if failed else 0)
