"""Offline tests for the generic HTTP API connector.

No network: definitions are written to a tmpdir JSON file and all HTTP goes
through httpx.MockTransport. Run: python3 evals/test_http_api.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# repo layout: evals/test_http_api.py, agent/src is the import root
sys.path.insert(0, str(HERE.parent / "agent" / "src"))

import httpx

from connectors.http_api import HttpApiConnector

PASS = 0


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        raise SystemExit(f"FAILED: {name} {extra}")


class Ctx:
    """Tmp defs file + env var, restored afterwards."""

    def __init__(self, defs):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "http_apis.json"
        if isinstance(defs, str):
            self.path.write_text(defs, encoding="utf-8")
        else:
            self.path.write_text(json.dumps(defs), encoding="utf-8")
        self.old = os.environ.get("BOOM_HTTP_API_DEFS")
        os.environ["BOOM_HTTP_API_DEFS"] = str(self.path)

    def close(self):
        if self.old is None:
            os.environ.pop("BOOM_HTTP_API_DEFS", None)
        else:
            os.environ["BOOM_HTTP_API_DEFS"] = self.old
        self.tmp.cleanup()


def crm_defs():
    return {"apis": [{
        "name": "crm",
        "base_url": "https://crm.example.com/api",
        "headers": {"Authorization": "Bearer s3cret"},
        "endpoints": [
            {"name": "get_user", "method": "GET", "path": "/users/{id}"},
            {"name": "create_user", "method": "POST", "path": "/users"},
            {"name": "delete_user", "method": "DELETE", "path": "/users/{id}"},
        ],
    }]}


def test_get_path_query_headers():
    ctx = Ctx(crm_defs())
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"id": 42, "name": "Ada"})

    try:
        conn = HttpApiConnector(transport=httpx.MockTransport(handler))
        tools = conn.build_tools()
        check("tool names", set(tools) == {"http_crm_get_user", "http_crm_create_user",
                                           "http_crm_delete_user"}, str(set(tools)))
        out = tools["http_crm_get_user"]["func"](id="42", verbose="true")
        check("parsed json returned", out == {"id": 42, "name": "Ada"}, str(out))
        check("method", seen["method"] == "GET")
        check("path param substituted", seen["path"] == "/api/users/42", seen["path"])
        check("query from extra args", seen["query"] == {"verbose": "true"}, str(seen["query"]))
        check("auth header sent", seen["auth"] == "Bearer s3cret", str(seen["auth"]))
        conn.close()
    finally:
        ctx.close()


def test_post_json_body():
    ctx = Ctx(crm_defs())
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["body"] = json.loads(request.content.decode())
        seen["ctype"] = request.headers.get("content-type")
        return httpx.Response(201, json={"id": 7})

    try:
        conn = HttpApiConnector(transport=httpx.MockTransport(handler))
        out = conn.build_tools()["http_crm_create_user"]["func"](name="Bo", role="admin")
        check("post returned", out == {"id": 7}, str(out))
        check("post method", seen["method"] == "POST")
        check("json body", seen["body"] == {"name": "Bo", "role": "admin"}, str(seen["body"]))
        check("json content-type", seen["ctype"] == "application/json", str(seen["ctype"]))
        conn.close()
    finally:
        ctx.close()


def test_delete_query_and_encoding():
    ctx = Ctx(crm_defs())
    seen = {}

    def handler(request):
        seen["raw_path"] = request.url.raw_path
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True})

    try:
        conn = HttpApiConnector(transport=httpx.MockTransport(handler))
        out = conn.build_tools()["http_crm_delete_user"]["func"](id="a b", force="1")
        check("delete ok", out == {"ok": True}, str(out))
        check("path param url-encoded",
              seen["raw_path"].startswith(b"/api/users/a%20b?"), repr(seen["raw_path"]))
        check("delete query", seen["query"] == {"force": "1"}, str(seen["query"]))
        conn.close()
    finally:
        ctx.close()


def test_missing_path_param():
    ctx = Ctx(crm_defs())
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    try:
        conn = HttpApiConnector(transport=httpx.MockTransport(handler))
        out = conn.build_tools()["http_crm_get_user"]["func"](verbose="1")
        check("missing param is error", out == {"error": "missing path param: id"}, str(out))
        check("no request made", calls == [])
        conn.close()
    finally:
        ctx.close()


def test_invalid_defs_skipped():
    ctx = Ctx({"apis": [
        {"name": "bad1", "base_url": "https://x.example", "endpoints": [
            {"name": "nope", "method": "FETCH", "path": "/a"},
            {"name": "nope2", "method": "GET", "path": "no-leading-slash"},
            {"name": "   ", "method": "GET", "path": "/a"},
            {"name": "ok", "method": "get", "path": "/a"},
            {"name": "ok", "method": "GET", "path": "/b"},
        ]},
        {"name": "noname-base", "endpoints": [{"name": "e", "method": "GET", "path": "/"}]},
        {"name": "", "base_url": "https://x.example", "endpoints": []},
    ]})

    def handler(request):
        return httpx.Response(200, json={})

    try:
        conn = HttpApiConnector(transport=httpx.MockTransport(handler))
        tools = conn.build_tools()
        check("only valid endpoint built", set(tools) == {"http_bad1_ok"}, str(set(tools)))
        st = conn.status()
        check("status connected", st["connected"] is True, str(st))
        check("status detail counts", st["detail"].startswith("1 api(s), 1 endpoint(s)"), st["detail"])
        conn.close()
    finally:
        ctx.close()


def test_name_sanitization():
    ctx = Ctx({"apis": [{
        "name": "My CRM!",
        "base_url": "https://x.example",
        "endpoints": [{"name": "Get-User", "method": "GET", "path": "/u"}],
    }]})

    def handler(request):
        return httpx.Response(200, json={})

    try:
        tools = HttpApiConnector(transport=httpx.MockTransport(handler)).build_tools()
        check("sanitized tool name", set(tools) == {"http_my_crm_get_user"}, str(set(tools)))
    finally:
        ctx.close()


def test_missing_file():
    old = os.environ.get("BOOM_HTTP_API_DEFS")
    os.environ["BOOM_HTTP_API_DEFS"] = "/tmp/boom-mods/http-api/does-not-exist-xyz.json"
    try:
        conn = HttpApiConnector()
        st = conn.status()
        check("missing file -> not connected", st["connected"] is False, str(st))
        check("missing file -> no tools", conn.build_tools() == {})
    finally:
        if old is None:
            os.environ.pop("BOOM_HTTP_API_DEFS", None)
        else:
            os.environ["BOOM_HTTP_API_DEFS"] = old


def test_corrupt_file():
    ctx = Ctx("{not json")
    try:
        conn = HttpApiConnector()
        st = conn.status()
        check("corrupt file -> not connected", st["connected"] is False, str(st))
        check("corrupt file -> no tools", conn.build_tools() == {})
    finally:
        ctx.close()


def test_non_json_response():
    ctx = Ctx(crm_defs())

    def handler(request):
        return httpx.Response(200, text="<html>hi</html>")

    try:
        conn = HttpApiConnector(transport=httpx.MockTransport(handler))
        out = conn.build_tools()["http_crm_get_user"]["func"](id="1")
        check("raw envelope", out == {"raw": "<html>hi</html>", "status": 200}, str(out))
        conn.close()
    finally:
        ctx.close()


def test_http_error_envelope():
    ctx = Ctx(crm_defs())

    def handler(request):
        return httpx.Response(500, json={"message": "boom"})

    try:
        conn = HttpApiConnector(transport=httpx.MockTransport(handler))
        out = conn.build_tools()["http_crm_get_user"]["func"](id="1")
        check("http error envelope",
              out == {"error": "HTTP 500", "status": 500, "body": {"message": "boom"}}, str(out))
        conn.close()
    finally:
        ctx.close()


def test_transport_error():
    ctx = Ctx(crm_defs())

    def handler(request):
        raise httpx.ConnectError("dns failed")

    try:
        conn = HttpApiConnector(transport=httpx.MockTransport(handler))
        out = conn.build_tools()["http_crm_get_user"]["func"](id="1")
        check("transport error envelope",
              out.get("error", "").startswith("ConnectError:"), str(out))
        conn.close()
    finally:
        ctx.close()


def test_non_dict_json_wrapped():
    ctx = Ctx(crm_defs())

    def handler(request):
        return httpx.Response(200, json=[1, 2, 3])

    try:
        conn = HttpApiConnector(transport=httpx.MockTransport(handler))
        out = conn.build_tools()["http_crm_get_user"]["func"](id="1")
        check("non-dict json wrapped", out == {"result": [1, 2, 3]}, str(out))
        conn.close()
    finally:
        ctx.close()


def test_schema_has_path_params():
    ctx = Ctx(crm_defs())
    try:
        tools = HttpApiConnector().build_tools()
        schema = tools["http_crm_get_user"]["schema"]
        check("schema required path param", schema["required"] == ["id"], str(schema))
        check("schema property", "id" in schema["properties"], str(schema))
        check("connector name", tools["http_crm_get_user"]["name"] == "http_crm_get_user")
        check("prefix", all(n.startswith("http_") for n in tools))
    finally:
        ctx.close()


if __name__ == "__main__":
    # default env must not leak between tests
    os.environ.pop("BOOM_HTTP_API_DEFS", None)
    test_get_path_query_headers()
    test_post_json_body()
    test_delete_query_and_encoding()
    test_missing_path_param()
    test_invalid_defs_skipped()
    test_name_sanitization()
    test_missing_file()
    test_corrupt_file()
    test_non_json_response()
    test_http_error_envelope()
    test_transport_error()
    test_non_dict_json_wrapped()
    test_schema_has_path_params()
    print(f"ALL HTTP API TESTS PASSED ({PASS} checks)")
