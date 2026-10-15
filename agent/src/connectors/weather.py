"""Weather connector for Boom — Open-Meteo, free, no API key.

Talks to the Open-Meteo forecast API (https://api.open-meteo.com/v1/forecast)
with plain stdlib urllib: no credential, no signup, no extra dependency.
status() is always connected=True because there is nothing to configure;
a network failure is reported per-call as {"error": ...}, never faked.

Import root is agent/src, so this lives at agent/src/connectors/weather.py
and is imported as `from connectors.weather import WeatherConnector`.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from connectors.base import Connector, _schema

API_BASE = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "BoomWeather/1.0"


def describe_weathercode(code: Any) -> str:
    """WMO weather code as a short Vietnamese description.

    Groupings follow the connector spec; codes outside it (85/86 snow showers,
    96/99 thunderstorm with hail) get their plain meaning. An unknown code is
    reported as-is, never guessed.
    """
    try:
        n = int(code)
    except (TypeError, ValueError):
        return "Không rõ"
    if n == 0:
        return "Trời quang"
    if 1 <= n <= 3:
        return "Có mây"
    if n in (45, 48):
        return "Sương mù"
    if 51 <= n <= 57:
        return "Mưa phùn"
    if 61 <= n <= 67:
        return "Mưa"
    if 71 <= n <= 77:
        return "Tuyết"
    if 80 <= n <= 82:
        return "Mưa rào"
    if n in (85, 86):
        return "Mưa tuyết"
    if n == 95:
        return "Dông"
    if n in (96, 99):
        return "Dông kèm mưa đá"
    return f"Mã thời tiết {n}"


def _urllib_get(url: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """One GET request, parsed JSON on success. Raises on transport/HTTP/parse failure."""
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _coords(latitude: Any, longitude: Any) -> Tuple[Optional[Tuple[float, float]], Optional[Dict[str, str]]]:
    """Validated (lat, lon) floats, or (None, {"error": ...})."""
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return None, {"error": "latitude and longitude are required, e.g. Hanoi: 21.0285, 105.8542."}
    if not -90 <= lat <= 90:
        return None, {"error": f"latitude {lat} is outside [-90, 90]."}
    if not -180 <= lon <= 180:
        return None, {"error": f"longitude {lon} is outside [-180, 180]."}
    return (lat, lon), None


class WeatherConnector(Connector):
    """Open-Meteo over stdlib urllib. `request_fn` is injectable so tests never
    touch the network: `request_fn(url, params) -> parsed JSON dict`."""

    name = "weather"

    def __init__(
        self,
        request_fn: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
        timeout: float = 20.0,
    ):
        self._request_fn = request_fn or (lambda url, params: _urllib_get(url, params, timeout))

    # ------------------------------------------------------------------ contract

    def status(self) -> Dict[str, Any]:
        return {"connected": True, "detail": "Open-Meteo forecast API, no key required."}

    def build_tools(self) -> Dict[str, Dict[str, Any]]:
        return {
            "weather_now": {
                "name": "weather_now",
                "func": self.weather_now,
                "description": (
                    "Current weather at a coordinate: temperature (C), humidity (%), "
                    "wind speed (km/h), WMO code and a short Vietnamese description. "
                    "Example: Hanoi is latitude 21.0285, longitude 105.8542."
                ),
                "schema": _schema(
                    {
                        "latitude": {"type": "number", "description": "Latitude, -90..90 (Hanoi: 21.0285)."},
                        "longitude": {"type": "number", "description": "Longitude, -180..180 (Hanoi: 105.8542)."},
                    },
                    ["latitude", "longitude"],
                ),
            },
            "weather_daily": {
                "name": "weather_daily",
                "func": self.weather_daily,
                "description": (
                    "Daily forecast at a coordinate: max/min temperature (C) and a short "
                    "Vietnamese description per day."
                ),
                "schema": _schema(
                    {
                        "latitude": {"type": "number", "description": "Latitude, -90..90 (Hanoi: 21.0285)."},
                        "longitude": {"type": "number", "description": "Longitude, -180..180 (Hanoi: 105.8542)."},
                        "days": {"type": "integer", "description": "Forecast days, 1-7 (default 3)."},
                    },
                    ["latitude", "longitude"],
                ),
            },
        }

    # ------------------------------------------------------------------ http

    def _fetch(self, params: Dict[str, Any]) -> Any:
        """Parsed API payload, or {"error": ...} — never raises."""
        try:
            data = self._request_fn(API_BASE, params)
        except urllib.error.HTTPError as e:
            return {"error": f"Open-Meteo HTTP {e.code}: {e.reason}"}
        except urllib.error.URLError as e:
            return {"error": f"network error: {e.reason}"}
        except (ValueError, json.JSONDecodeError) as e:
            return {"error": f"could not parse the weather response: {e}"}
        except Exception as e:
            return {"error": f"weather request failed: {type(e).__name__}: {e}"}
        if not isinstance(data, dict):
            return {"error": "weather service returned unexpected data."}
        if "error" in data and "current" not in data and "daily" not in data:
            # Open-Meteo's own error envelope: {"error": true, "reason": "..."}
            return {"error": f"Open-Meteo: {data.get('reason', 'request rejected')}"}
        return data

    # ------------------------------------------------------------------ tools

    def weather_now(self, latitude: Any, longitude: Any) -> Dict[str, Any]:
        coords, err = _coords(latitude, longitude)
        if err:
            return err
        lat, lon = coords
        data = self._fetch({
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
            "timezone": "auto",
        })
        if "error" in data:
            return data
        cur = data.get("current") or {}
        if not isinstance(cur, dict) or "temperature_2m" not in cur:
            return {"error": "weather service returned no current conditions."}
        code = cur.get("weather_code")
        return {
            "latitude": data.get("latitude", lat),
            "longitude": data.get("longitude", lon),
            "timezone": data.get("timezone", ""),
            "observed_at": cur.get("time", ""),
            "temperature_c": cur.get("temperature_2m"),
            "humidity_pct": cur.get("relative_humidity_2m"),
            "wind_kmh": cur.get("wind_speed_10m"),
            "weathercode": code,
            "description": describe_weathercode(code),
        }

    def weather_daily(self, latitude: Any, longitude: Any, days: int = 3) -> Dict[str, Any]:
        coords, err = _coords(latitude, longitude)
        if err:
            return err
        try:
            n = int(days)
        except (TypeError, ValueError):
            return {"error": "days must be an integer 1-7."}
        if not 1 <= n <= 7:
            return {"error": f"days must be 1-7, got {days!r}."}
        lat, lon = coords
        data = self._fetch({
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,weather_code",
            "timezone": "auto",
            "forecast_days": n,
        })
        if "error" in data:
            return data
        daily = data.get("daily") or {}
        if not isinstance(daily, dict):
            return {"error": "weather service returned no daily forecast."}
        times: List[Any] = daily.get("time") or []
        tmax: List[Any] = daily.get("temperature_2m_max") or []
        tmin: List[Any] = daily.get("temperature_2m_min") or []
        codes: List[Any] = daily.get("weather_code") or []
        out = []
        for i, day in enumerate(times[:n]):
            code = codes[i] if i < len(codes) else None
            out.append({
                "date": day,
                "temp_max_c": tmax[i] if i < len(tmax) else None,
                "temp_min_c": tmin[i] if i < len(tmin) else None,
                "weathercode": code,
                "description": describe_weathercode(code),
            })
        if not out:
            return {"error": "weather service returned an empty forecast."}
        return {
            "latitude": data.get("latitude", lat),
            "longitude": data.get("longitude", lon),
            "timezone": data.get("timezone", ""),
            "days": out,
        }


# Module-level instance picked up by connectors.registry.load_default_registry().
CONNECTOR = WeatherConnector()
