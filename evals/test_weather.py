"""Offline tests for the weather connector (Open-Meteo, no key).

No network: a fake request_fn serves fixtures in the real Open-Meteo shape.
Run: python3 evals/test_weather.py
"""

import sys
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
# repo layout: evals/test_weather.py, agent/src is the import root
sys.path.insert(0, str(HERE.parent / "agent" / "src"))

from connectors.base import Connector
from connectors.weather import CONNECTOR, WeatherConnector, describe_weathercode

PASS = 0


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        raise SystemExit(f"FAIL {name} {extra}")


NOW_FIXTURE = {
    "latitude": 21.0,
    "longitude": 105.8,
    "timezone": "Asia/Bangkok",
    "current": {
        "time": "2026-10-15T14:00",
        "temperature_2m": 28.5,
        "relative_humidity_2m": 75,
        "weather_code": 2,
        "wind_speed_10m": 12.3,
    },
}

DAILY_FIXTURE = {
    "latitude": 21.0,
    "longitude": 105.8,
    "timezone": "Asia/Bangkok",
    "daily": {
        "time": ["2026-10-15", "2026-10-16", "2026-10-17"],
        "temperature_2m_max": [31.0, 30.5, 29.8],
        "temperature_2m_min": [24.1, 23.9, 23.5],
        "weather_code": [2, 61, 95],
    },
}


def fake_now(url, params):
    assert url == "https://api.open-meteo.com/v1/forecast", url
    assert params["latitude"] == 21.0285, params
    assert "current" in params, params
    return NOW_FIXTURE


def fake_daily(url, params):
    assert params["forecast_days"] == 3, params
    assert "daily" in params, params
    return DAILY_FIXTURE


def test_contract():
    c = WeatherConnector()
    check("is Connector", isinstance(c, Connector))
    check("name", c.name == "weather")
    check("CONNECTOR instance", isinstance(CONNECTOR, WeatherConnector))
    st = c.status()
    check("always connected", st == {"connected": True, "detail": "Open-Meteo forecast API, no key required."}, st)
    tools = c.build_tools()
    check("two tools", set(tools) == {"weather_now", "weather_daily"}, set(tools))
    for tname, t in tools.items():
        check(f"{tname} shape", t["name"] == tname and callable(t["func"])
              and t["description"] and "properties" in t["schema"])
    check("lat/lon required",
          tools["weather_now"]["schema"]["required"] == ["latitude", "longitude"])


def test_weathercode_vi():
    cases = {0: "Trời quang", 1: "Có mây", 2: "Có mây", 3: "Có mây",
             45: "Sương mù", 48: "Sương mù",
             51: "Mưa phùn", 55: "Mưa phùn", 57: "Mưa phùn",
             61: "Mưa", 65: "Mưa", 67: "Mưa",
             71: "Tuyết", 75: "Tuyết", 77: "Tuyết",
             80: "Mưa rào", 82: "Mưa rào", 95: "Dông"}
    for code, want in cases.items():
        check(f"code {code}", describe_weathercode(code) == want, describe_weathercode(code))
    check("unknown code honest", describe_weathercode(999) == "Mã thời tiết 999")
    check("bad code honest", describe_weathercode(None) == "Không rõ")


def test_now_parsing():
    c = WeatherConnector(request_fn=fake_now)
    r = c.weather_now(21.0285, 105.8542)
    check("temp", r["temperature_c"] == 28.5, r)
    check("humidity", r["humidity_pct"] == 75, r)
    check("wind", r["wind_kmh"] == 12.3, r)
    check("code", r["weathercode"] == 2, r)
    check("description vi", r["description"] == "Có mây", r)
    check("observed_at", r["observed_at"] == "2026-10-15T14:00", r)
    check("no error key", "error" not in r, r)


def test_daily_parsing():
    c = WeatherConnector(request_fn=fake_daily)
    r = c.weather_daily(21.0285, 105.8542, days=3)
    check("3 days", len(r["days"]) == 3, r)
    d0, d1, d2 = r["days"]
    check("day0 max/min", (d0["temp_max_c"], d0["temp_min_c"]) == (31.0, 24.1), d0)
    check("day0 desc", d0["description"] == "Có mây", d0)
    check("day1 desc", d1["description"] == "Mưa", d1)
    check("day2 desc", d2["description"] == "Dông", d2)
    check("dates", [d["date"] for d in r["days"]] == ["2026-10-15", "2026-10-16", "2026-10-17"], r)


def test_validation():
    c = WeatherConnector(request_fn=fake_now)
    check("missing lat", "error" in c.weather_now(None, 105.0))
    check("missing lon", "error" in c.weather_now(21.0, None))
    check("non-numeric", "error" in c.weather_now("abc", "def"))
    check("lat out of range", "error" in c.weather_now(91, 105.0))
    check("lon out of range", "error" in c.weather_now(21.0, 181))
    check("string coords accepted", "error" not in
          WeatherConnector(request_fn=fake_now).weather_now("21.0285", "105.8542"))
    d = WeatherConnector(request_fn=fake_daily)
    check("days 0 rejected", "error" in d.weather_daily(21.0, 105.0, days=0))
    check("days 8 rejected", "error" in d.weather_daily(21.0, 105.0, days=8))
    check("days text rejected", "error" in d.weather_daily(21.0, 105.0, days="nhieu"))


def test_failures_never_raise():
    def boom(url, params):
        raise urllib.error.URLError("dns down")

    c = WeatherConnector(request_fn=boom)
    r = c.weather_now(21.0, 105.0)
    check("network error -> envelope", "error" in r and "network error" in r["error"], r)

    def api_rejects(url, params):
        return {"error": True, "reason": "Latitude must be in range of -90 to 90."}

    c2 = WeatherConnector(request_fn=api_rejects)
    r2 = c2.weather_now(21.0, 105.0)
    check("api error envelope", "error" in r2 and "Open-Meteo" in r2["error"], r2)

    def garbage(url, params):
        return ["not", "a", "dict"]

    c3 = WeatherConnector(request_fn=garbage)
    check("non-dict -> error", "error" in c3.weather_now(21.0, 105.0))

    def no_current(url, params):
        return {"latitude": 21.0, "longitude": 105.0}

    c4 = WeatherConnector(request_fn=no_current)
    check("missing current -> error", "error" in c4.weather_now(21.0, 105.0))


def test_tools_callable_via_build_tools():
    c = WeatherConnector(request_fn=fake_now)
    tools = c.build_tools()
    r = tools["weather_now"]["func"](21.0285, 105.8542)
    check("tool func works", r["temperature_c"] == 28.5, r)


if __name__ == "__main__":
    print("== weather connector ==")
    test_contract()
    test_weathercode_vi()
    test_now_parsing()
    test_daily_parsing()
    test_validation()
    test_failures_never_raise()
    test_tools_callable_via_build_tools()
    print(f"ALL WEATHER TESTS PASSED ({PASS} checks)")
