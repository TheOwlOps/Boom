"""Generic HTTP API connector: call any user-defined REST API as agent tools.

Endpoint definitions live in a JSON file (default ``memories/http_apis.json``,
overridable with the ``BOOM_HTTP_API_DEFS`` env var):

.. code-block:: json

    {"apis": [{
        "name": "crm",
        "base_url": "https://crm.example.com/api",
        "headers": {"Authorization": "Bearer ..."},
        "timeout": 15.0,
        "endpoints": [
            {"name": "get_user", "method": "GET", "path": "/users/{id}"},
            {"name": "create_user", "method": "POST", "path": "/users"}
        ]
    }]}

Every valid endpoint becomes one tool named ``http_<api>_<endpoint>``.
``{param}`` segments in the path are taken from the tool args; the remaining
args become the query string (GET/DELETE) or the JSON body (POST/PUT/PATCH).

A definition file that is absent, unreadable or malformed yields zero APIs —
never a crash, never a fake success.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from connectors.base import Connector, _schema

logger = logging.getLogger("boom.connectors.http_api")

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_PATH_PARAM_RE = re.compile(r"\{(\w+)\}")
_DEFAULT_TIMEOUT = 30.0


def _defs_path() -> Path:
    return Path(os.getenv("BOOM_HTTP_API_DEFS", "memories/http_apis.json"))


def _sanitize(name: Any) -> str:
    """Lowercase [a-z0-9_]; anything else becomes _, collapsed and trimmed."""
    text = re.sub(r"[^a-z0-9_]", "_", str(name or "").lower())
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def _load_raw_defs() -> List[Dict[str, Any]]:
    """The raw api list from the definitions file. [] when absent or unusable."""
    path = _defs_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("http_api defs %s unreadable (%s); no apis loaded", path, e)
        return []
    if not isinstance(data, dict) or not isinstance(data.get("apis"), list):
        logger.warning("http_api defs %s needs {\"apis\": [...]}; no apis loaded", path)
        return []
    return [a for a in data["apis"] if isinstance(a, dict)]


def _validate_api(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A normalised api dict, or None when the definition is unusable."""
    name = _sanitize(raw.get("name"))
    base_url = str(raw.get("base_url") or "").strip().rstrip("/")
    if not name or not base_url:
        logger.warning("http_api: skipping api with bad name/base_url: %r", raw.get("name"))
        return None
    headers = raw.get("headers") or {}
    if not isinstance(headers, dict):
        logger.warning("http_api: api %r headers must be a mapping; ignoring", name)
        headers = {}
    try:
        timeout = float(raw.get("timeout", _DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        logger.warning("http_api: api %r bad timeout; using default", name)
        timeout = _DEFAULT_TIMEOUT
    endpoints: List[Dict[str, Any]] = []
    seen: set = set()
    for ep in raw.get("endpoints") or []:
        norm = _validate_endpoint(name, ep)
        if norm is None:
            continue
        if norm["tool"] in seen:
            logger.warning("http_api: api %r duplicate endpoint name %r; skipping",
                           name, ep.get("name"))
            continue
        seen.add(norm["tool"])
        endpoints.append(norm)
    return {"name": name, "base_url": base_url, "headers": headers,
            "timeout": timeout, "endpoints": endpoints}


def _validate_endpoint(api_name: str, raw: Any) -> Optional[Dict[str, Any]]:
    """A normalised endpoint dict, or None when the definition is unusable."""
    if not isinstance(raw, dict):
        logger.warning("http_api: api %r has a non-mapping endpoint; skipping", api_name)
        return None
    ep_name = _sanitize(raw.get("name"))
    method = str(raw.get("method") or "").strip().upper()
    path = str(raw.get("path") or "").strip()
    if not ep_name:
        logger.warning("http_api: api %r endpoint needs a valid name; skipping", api_name)
        return None
    if method not in METHODS:
        logger.warning("http_api: api %r endpoint %r bad method %r; skipping",
                       api_name, ep_name, raw.get("method"))
        return None
    if not path.startswith("/"):
        logger.warning("http_api: api %r endpoint %r path must start with /; skipping",
                       api_name, ep_name)
        return None
    return {
        "name": ep_name,
        "tool": f"http_{api_name}_{ep_name}",
        "method": method,
        "path": path,
        "params": _PATH_PARAM_RE.findall(path),
    }


class HttpApiConnector(Connector):
    """One connector serving every user-defined REST API from the defs file."""

    name = "http_api"

    def __init__(self, transport: Optional[httpx.BaseTransport] = None):
        # transport is a test seam (httpx.MockTransport); production passes None.
        self._transport = transport
        self._clients: Dict[str, httpx.Client] = {}

    # ------------------------------------------------------------------ defs

    def _apis(self) -> List[Dict[str, Any]]:
        apis = []
        for raw in _load_raw_defs():
            api = _validate_api(raw)
            if api is not None and api["endpoints"]:
                apis.append(api)
        return apis

    def status(self) -> Dict[str, Any]:
        apis = self._apis()
        n_eps = sum(len(a["endpoints"]) for a in apis)
        if not apis:
            return {"connected": False,
                    "detail": f"no apis defined ({_defs_path()})"}
        return {"connected": True,
                "detail": f"{len(apis)} api(s), {n_eps} endpoint(s) from {_defs_path()}"}

    # ------------------------------------------------------------------ tools

    def build_tools(self) -> Dict[str, Dict[str, Any]]:
        tools: Dict[str, Dict[str, Any]] = {}
        for api in self._apis():
            for ep in api["endpoints"]:
                func = self._make_func(api, ep)
                props = {p: {"type": "string", "description": f"Path parameter '{p}'"}
                         for p in ep["params"]}
                if ep["method"] in ("GET", "DELETE"):
                    extra = "Extra args become the query string."
                else:
                    extra = "Extra args become the JSON body."
                tools[ep["tool"]] = {
                    "name": ep["tool"],
                    "func": func,
                    "description": (
                        f"{ep['method']} {api['base_url']}{ep['path']} "
                        f"(api '{api['name']}', http_api connector). {extra}"
                    ),
                    "schema": _schema(props, ep["params"]),
                }
        return tools

    def _make_func(self, api: Dict[str, Any], ep: Dict[str, Any]):
        def call(**kwargs: Any) -> Dict[str, Any]:
            return self._call(api, ep, kwargs)
        call.__name__ = ep["tool"]
        return call

    def _client(self, api: Dict[str, Any]) -> httpx.Client:
        client = self._clients.get(api["name"])
        if client is None:
            client = httpx.Client(timeout=api["timeout"], transport=self._transport)
            self._clients[api["name"]] = client
        return client

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    # ------------------------------------------------------------------ call

    def _call(self, api: Dict[str, Any], ep: Dict[str, Any],
              args: Dict[str, Any]) -> Dict[str, Any]:
        args = dict(args or {})
        path = ep["path"]
        for param in ep["params"]:
            if param not in args or args[param] is None:
                return {"error": f"missing path param: {param}"}
            path = path.replace("{" + param + "}", quote(str(args.pop(param)), safe=""))
        url = api["base_url"] + path
        request_kwargs: Dict[str, Any] = {"headers": api["headers"]}
        if ep["method"] in ("GET", "DELETE"):
            try:
                request_kwargs["params"] = {k: self._query_value(v)
                                            for k, v in args.items()}
            except (TypeError, ValueError) as e:
                return {"error": f"bad query args: {e}"}
        else:
            try:
                json.dumps(args)  # fail fast on non-serialisable bodies
            except (TypeError, ValueError) as e:
                return {"error": f"body is not JSON-serialisable: {e}"}
            request_kwargs["json"] = args
        try:
            resp = self._client(api).request(ep["method"], url, **request_kwargs)
        except httpx.HTTPError as e:
            return {"error": f"{type(e).__name__}: {e}"}
        except Exception as e:  # never let a transport surprise escape as a raise
            return {"error": f"{type(e).__name__}: {e}"}
        return self._interpret(resp)

    @staticmethod
    def _query_value(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return json.dumps(value)

    @staticmethod
    def _interpret(resp: httpx.Response) -> Dict[str, Any]:
        try:
            body: Any = resp.json()
        except ValueError:
            body = None
        if resp.status_code >= 400:
            detail = body if body is not None else resp.text
            return {"error": f"HTTP {resp.status_code}", "status": resp.status_code,
                    "body": detail}
        if body is not None:
            return body if isinstance(body, dict) else {"result": body}
        return {"raw": resp.text, "status": resp.status_code}


# Module-level instance picked up by connectors.registry.load_default_registry().
CONNECTOR = HttpApiConnector()
