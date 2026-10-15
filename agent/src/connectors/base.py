"""Connector contract for Boom agent/src/connectors/.

A connector bridges the agent to an external service (Gmail, Calendar, ...).
Credentials come ONLY from the environment (BOOM_<NAME>_*); nothing here ever
prompts for secrets or hardcodes one. status() must be honest: connected=False
when the credential is absent — never a fake success.

Tool names are prefixed with the connector name (gmail_search, calendar_list, ...)
so merged registries never collide.

Shared HTTP plumbing lives here too (SyncBearerConnector / AsyncBearerConnector)
so the eight service connectors don't each reimplement the same Bearer-token
request envelope. NOTE: httpx is imported lazily inside those methods — the
stdlib-only connectors (weather, news) must stay importable where httpx
isn't installed.
"""

import contextlib
import os
from typing import Any, Dict, Iterator, Optional

from .credentials import get_token_for


class Connector:
    """One external service. Subclass, set `name`, implement both methods."""

    name: str = "base"

    def status(self) -> Dict[str, Any]:
        """{"connected": bool, "detail": str}. Config check only, no network needed."""
        raise NotImplementedError

    def build_tools(self) -> Dict[str, Dict[str, Any]]:
        """Tool dicts like every other Boom tool module.

        {tool_name: {"name":..., "func":..., "description":..., "schema":...}}
        func may be a plain function or `async def`. Errors are returned as
        {"error": ...}, never raised to the caller, never faked.
        """
        raise NotImplementedError


def _schema(properties=None, required=None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or []}


def sync_bearer_request(conn, method, path, params=None, json_body=None):
    """Shared sync Bearer-token request envelope.

    `conn` provides: _token(), _api_base, _session(), _api_error(resp), plus
    class attrs SERVICE, TOKEN_ENV, TOKEN_HINT, EXTRA_HEADERS,
    STRICT_DICT_RESPONSE. Returns parsed JSON or {"error": ...}; never raises.

    Used by SyncBearerConnector._request and directly by connectors that must
    keep `Connector` as their direct parent class.
    """
    import httpx  # lazy: keep base.py importable without httpx

    token = conn._token()
    if not token:
        return {"error": f"{conn.TOKEN_ENV} is not set: {conn.TOKEN_HINT}"}
    url = f"{conn._api_base}{path}"
    headers = {"Authorization": f"Bearer {token}", **conn.EXTRA_HEADERS}
    try:
        with conn._session() as client:
            resp = client.request(
                method, url, params=params, json=json_body, headers=headers
            )
    except httpx.RequestError as exc:
        return {"error": f"{conn.SERVICE} request failed: {type(exc).__name__}: {exc}"}
    if resp.status_code >= 400:
        return {"error": conn._api_error(resp)}
    try:
        data = resp.json()
    except ValueError:
        return {"error": f"{conn.SERVICE} returned non-JSON (HTTP {resp.status_code})."}
    if conn.STRICT_DICT_RESPONSE and not isinstance(data, dict):
        return {"error": f"{conn.SERVICE} returned unexpected data."}
    return data


class SyncBearerConnector(Connector):
    """Synchronous httpx Bearer-token JSON API.

    Subclass sets the class attrs; the request envelope (token check, headers,
    transport errors, HTTP >= 400, JSON parse) is shared via
    sync_bearer_request(). Override _api_error when the service has its own
    error-body shape.
    """

    TOKEN_ENV: str = ""          # e.g. "BOOM_GMAIL_ACCESS_TOKEN" (for messages)
    API_BASE: str = ""           # e.g. "https://gmail.googleapis.com/gmail/v1"
    SERVICE: str = "service"     # short name for error messages, e.g. "Gmail"
    CRED_SERVICE: str = ""       # get_token_for(CRED_SERVICE, CRED_KEY)
    CRED_KEY: str = "access_token"
    TOKEN_HINT: str = ""         # "export a ... token to use X."
    EXTRA_HEADERS: Dict[str, str] = {}  # merged with the Authorization header
    STRICT_DICT_RESPONSE: bool = True   # False: return lists as-is (e.g. GitHub)

    def __init__(
        self,
        token: Optional[str] = None,
        client: Any = None,
        api_base: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        # token=None means "read the env var lazily at call time", so an env
        # change after import is still picked up. A non-None value is an
        # explicit override (used by tests with a fake transport).
        self._token_override = token
        self._injected_client = client
        self._api_base = (api_base or self.API_BASE).rstrip("/")
        self._timeout = timeout

    def _token(self) -> str:
        if self._token_override is not None:
            return self._token_override.strip()
        return get_token_for(self.CRED_SERVICE, self.CRED_KEY)

    @contextlib.contextmanager
    def _session(self) -> Iterator[Any]:
        """Yield a client; an injected one is never closed by us."""
        import httpx  # lazy: keep base.py importable without httpx

        if self._injected_client is not None:
            yield self._injected_client
        else:
            with httpx.Client(timeout=self._timeout) as client:
                yield client

    def _api_error(self, resp: Any) -> str:
        """Human-readable error from a failed API response. Override per service."""
        return f"{self.SERVICE} HTTP {resp.status_code}"


    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """One API call. Parsed JSON on success, {"error": ...} otherwise."""
        return sync_bearer_request(self, method, path, params, json_body)


class AsyncBearerConnector(Connector):
    """Async httpx Bearer-token JSON API. Subclass sets the class attrs."""

    TOKEN_ENV: str = ""
    API_BASE: str = ""
    SERVICE: str = "service"

    def __init__(
        self,
        token: Optional[str] = None,
        transport: Any = None,
        timeout: float = 30.0,
    ) -> None:
        self.token = token if token is not None else os.environ.get(self.TOKEN_ENV, "")
        self._transport = transport
        self._timeout = timeout

    def _client(self) -> Any:
        import httpx  # lazy: keep base.py importable without httpx

        return httpx.AsyncClient(
            base_url=self.API_BASE,
            headers={"Authorization": f"Bearer {self.token}"},
            transport=self._transport,
            timeout=self._timeout,
        )

    def _guard(self) -> Optional[Dict[str, str]]:
        if not self.token:
            return {"error": f"not connected: set env {self.TOKEN_ENV}"}
        return None

    def _api_error(self, resp: Any) -> str:
        """Human-readable error from a failed API response. Override per service."""
        return f"{self.SERVICE} HTTP {resp.status_code}"

    async def _arequest(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """One async API call. Returns the httpx response, or {"error": ...}.

        Callers detect the error case with `isinstance(resp, dict)`, then apply
        their own status-code checks and parsing (200 vs 201 vs 204 differs per
        endpoint, so that part stays per-tool).
        """
        import httpx  # lazy: keep base.py importable without httpx

        err = self._guard()
        if err:
            return err
        try:
            async with self._client() as client:
                return await client.request(
                    method, path, params=params, json=json_body
                )
        except httpx.RequestError as e:
            return {"error": f"network error: {type(e).__name__}: {e}"}
        except Exception as e:  # never let a transport surprise escape as a raise
            return {"error": f"request failed: {type(e).__name__}: {e}"}
