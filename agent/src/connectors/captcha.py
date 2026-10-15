"""2captcha connector — paid CAPTCHA solving for the data-scraping pipeline.

Technical honesty, read first:
- This module does NOT solve captchas itself (no homegrown ML — heavy, poor
  accuracy). The only real approach is a commercial solving service; this is a
  thin wrapper around the 2captcha API (https://2captcha.com).
- REQUIRES A PAID API KEY: export BOOM_2CAPTCHA_KEY="<key>". The user funds
  their own 2captcha account; each solve costs real credit.
- ONLY use against sites the user has the right to scrape. Bypassing a captcha
  on a site you are not allowed to access may be illegal.

2captcha API v1 (https://2captcha.com/2captcha-api):
- POST /in.php  (key, method=base64, body=<base64>, json=1)
    -> {"status": 1, "request": "<task_id>"} | {"status": 0, "request": "ERROR_..."}
- GET  /res.php (key, action=get, id=<task_id>, json=1)
    -> {"status": 1, "request": "OK|<solution>"} |
       {"status": 0, "request": "CAPCHA_NOT_READY"} |
       {"status": 0, "request": "ERROR_..."}
- GET  /res.php (key, action=getbalance, json=1)
    -> {"status": 1, "request": "<balance>"} | {"status": 0, "request": "ERROR_..."}
"""

import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

from connectors.base import Connector, _schema

log = logging.getLogger(__name__)

API_BASE = "https://2captcha.com"
KEY_ENV = "BOOM_2CAPTCHA_KEY"

DEFAULT_TIMEOUT = 120.0   # max seconds to wait for a solution
DEFAULT_POLL_INTERVAL = 5.0


class CaptchaConnector(Connector):
    """2captcha over its HTTP API. Bearer-style key from BOOM_2CAPTCHA_KEY."""

    name = "captcha"

    def __init__(
        self,
        api_key: Optional[str] = None,
        client: Optional[httpx.Client] = None,
        api_base: str = API_BASE,
        timeout: float = 30.0,
    ) -> None:
        # api_key=None means "read BOOM_2CAPTCHA_KEY lazily at call time", so an
        # env change after import is still picked up. A non-None value is an
        # explicit override (used by tests with a fake transport).
        self._key_override = api_key
        self._injected_client = client
        self._api_base = (api_base or API_BASE).rstrip("/")
        self._timeout = timeout

    # -- config ------------------------------------------------------

    def _key(self) -> str:
        if self._key_override is not None:
            return self._key_override.strip()
        return os.environ.get(KEY_ENV, "").strip()

    def status(self) -> Dict[str, Any]:
        if self._key():
            return {
                "connected": True,
                "detail": f"{KEY_ENV} is set; 2captcha API ready (paid service).",
            }
        return {
            "connected": False,
            "detail": (
                f"{KEY_ENV} is not set: export a 2captcha API key "
                "(paid service, fund your own account) to enable CAPTCHA solving."
            ),
        }

    # -- HTTP --------------------------------------------------------

    def _post(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """One 2captcha call. Parsed envelope on success, {"error": ...} otherwise."""
        key = self._key()
        if not key:
            return {
                "error": (
                    f"{KEY_ENV} is not set: export a 2captcha API key to use CAPTCHA solving."
                )
            }
        url = f"{self._api_base}{path}"
        payload = {"key": key, "json": 1, **data}
        try:
            if self._injected_client is not None:
                resp = self._injected_client.post(url, data=payload, timeout=self._timeout)
            else:
                resp = httpx.post(url, data=payload, timeout=self._timeout)
        except httpx.RequestError as exc:
            return {"error": f"2captcha request failed: {type(exc).__name__}: {exc}"}
        if resp.status_code >= 400:
            return {"error": f"2captcha HTTP {resp.status_code}: {resp.text[:200]}"}
        try:
            body = resp.json()
        except ValueError:
            return {"error": f"2captcha returned non-JSON (HTTP {resp.status_code})."}
        if not isinstance(body, dict):
            return {"error": "2captcha returned unexpected data."}
        if body.get("status") != 1:
            return {"error": f"2captcha error: {body.get('request', 'unknown error')}"}
        return {"ok": True, "request": body.get("request")}

    def _get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        key = self._key()
        if not key:
            return {
                "error": (
                    f"{KEY_ENV} is not set: export a 2captcha API key to use CAPTCHA solving."
                )
            }
        url = f"{self._api_base}/res.php"
        query = {"key": key, "json": 1, **params}
        try:
            if self._injected_client is not None:
                resp = self._injected_client.get(url, params=query, timeout=self._timeout)
            else:
                resp = httpx.get(url, params=query, timeout=self._timeout)
        except httpx.RequestError as exc:
            return {"error": f"2captcha request failed: {type(exc).__name__}: {exc}"}
        if resp.status_code >= 400:
            return {"error": f"2captcha HTTP {resp.status_code}: {resp.text[:200]}"}
        try:
            body = resp.json()
        except ValueError:
            return {"error": f"2captcha returned non-JSON (HTTP {resp.status_code})."}
        if not isinstance(body, dict):
            return {"error": "2captcha returned unexpected data."}
        if body.get("status") != 1:
            return {"error": f"2captcha error: {body.get('request', 'unknown error')}"}
        return {"ok": True, "request": body.get("request")}

    # -- tools -------------------------------------------------------

    def captcha_balance(self) -> Dict[str, Any]:
        """Current 2captcha account balance (real credit)."""
        data = self._get({"action": "getbalance"})
        if "error" in data:
            return data
        try:
            balance = float(data["request"])
        except (TypeError, ValueError):
            return {"error": f"2captcha returned an unreadable balance: {data['request']!r}"}
        return {"balance": balance}

    def captcha_solve_image(
        self,
        image_base64: str,
        timeout: float = DEFAULT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> Dict[str, Any]:
        """Submit an image CAPTCHA (base64 PNG/JPEG) and wait for the solution.

        Polls /res.php every ``poll_interval`` seconds until 2captcha answers
        OK|<text> or ``timeout`` seconds pass. Never hangs forever.
        """
        image_base64 = (image_base64 or "").strip()
        if not image_base64:
            return {"error": "captcha_solve_image needs image_base64 (base64-encoded image)."}
        try:
            timeout = float(timeout)
            poll_interval = float(poll_interval)
        except (TypeError, ValueError):
            return {"error": "timeout and poll_interval must be numbers."}
        if timeout <= 0 or poll_interval <= 0:
            return {"error": "timeout and poll_interval must be positive."}

        sub = self._post("/in.php", {"method": "base64", "body": image_base64})
        if "error" in sub:
            return sub
        task_id = sub["request"]

        deadline = time.monotonic() + timeout
        while True:
            got = self._get({"action": "get", "id": task_id})
            if "error" in got:
                # CAPCHA_NOT_READY is not a real error: keep waiting.
                if "CAPCHA_NOT_READY" not in got["error"]:
                    return got
            else:
                answer = str(got["request"] or "")
                if answer.startswith("OK|"):
                    return {
                        "solved": True,
                        "text": answer[3:],
                        "task_id": task_id,
                    }
                return {"error": f"2captcha returned an unexpected answer: {answer!r}"}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "error": (
                        f"2captcha solve timed out after {timeout:g}s "
                        f"(task {task_id} still not ready)."
                    )
                }
            time.sleep(min(poll_interval, remaining))

    # -- contract ----------------------------------------------------

    def build_tools(self) -> Dict[str, Dict[str, Any]]:
        return {
            "captcha_solve_image": {
                "name": "captcha_solve_image",
                "func": self.captcha_solve_image,
                "description": (
                    "Solve an image CAPTCHA via the paid 2captcha service "
                    "(requires BOOM_2CAPTCHA_KEY). Submits the base64 image, "
                    "polls for the solution (default 120s). Only use on sites "
                    "you have the right to scrape."
                ),
                "schema": _schema(
                    {
                        "image_base64": {
                            "type": "string",
                            "description": "Base64-encoded CAPTCHA image (PNG/JPEG).",
                        },
                        "timeout": {
                            "type": "number",
                            "description": "Max seconds to wait for the solution (default 120).",
                        },
                        "poll_interval": {
                            "type": "number",
                            "description": "Seconds between result polls (default 5).",
                        },
                    },
                    ["image_base64"],
                ),
            },
            "captcha_balance": {
                "name": "captcha_balance",
                "func": self.captcha_balance,
                "description": (
                    "Check the current 2captcha account balance "
                    "(requires BOOM_2CAPTCHA_KEY)."
                ),
                "schema": _schema(),
            },
        }


CONNECTOR = CaptchaConnector()
