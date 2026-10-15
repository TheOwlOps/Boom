"""Google Drive connector tests — no network.

A fake httpx transport serves realistic Drive API v3 fixtures, so every tool
is exercised end to end (request building, auth header, response parsing,
error mapping) without touching the real API.
"""

import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from connectors.google_drive import TOKEN_ENV, GoogleDriveConnector

LIST_FIXTURE = {
    "kind": "drive#fileList",
    "files": [
        {"kind": "drive#file", "id": "1AbC2dEfGhIjKlMnOp",
         "name": "notes.txt", "mimeType": "text/plain",
         "modifiedTime": "2026-10-14T08:12:33.000Z"},
        {"kind": "drive#file", "id": "2XyZ9wVuTsRqPoNmLk",
         "name": "report Q3", "mimeType": "application/vnd.google-apps.document",
         "modifiedTime": "2026-10-13T17:45:00.000Z"},
    ],
    "nextPageToken": "CgtnZXh0LXBhZ2U=",
}

SEARCH_FIXTURE = {
    "kind": "drive#fileList",
    "files": [
        {"kind": "drive#file", "id": "1AbC2dEfGhIjKlMnOp",
         "name": "notes.txt", "mimeType": "text/plain",
         "modifiedTime": "2026-10-14T08:12:33.000Z"},
    ],
}

UPLOAD_FIXTURE = {
    "kind": "drive#file",
    "id": "9NewFileIdAbC123",
    "name": "hello.txt",
    "mimeType": "text/plain",
    "size": "11",
}

DOCS_EXPORT_ERROR = {
    "error": {"code": 403,
              "message": "Only files with binary content can be downloaded. "
                         "Use Export with Docs Editors files.",
              "errors": [{"domain": "global", "reason": "fileNotDownloadable"}]},
}


def make_transport(calls, mode="ok"):
    """mode: ok | binary | docs_error | auth_error"""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        url = str(request.url)
        if mode == "auth_error":
            return httpx.Response(401, json={
                "error": {"code": 401, "message": "Invalid Credentials",
                          "errors": [{"reason": "authError"}]}})
        if request.method == "GET" and "/drive/v3/files/" in url \
                and "alt=media" in url:
            if mode == "binary":
                return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n\x00\x01")
            if mode == "docs_error":
                return httpx.Response(403, json=DOCS_EXPORT_ERROR)
            return httpx.Response(200, content="hello drive".encode("utf-8"))
        if request.method == "GET" and url.startswith(
                "https://www.googleapis.com/drive/v3/files"):
            params = parse_qs(urlparse(url).query)
            q = params.get("q", [""])[0]
            if "name contains" in q:
                return httpx.Response(200, json=SEARCH_FIXTURE)
            return httpx.Response(200, json=LIST_FIXTURE)
        if request.method == "POST" and "upload/drive/v3/files" in url:
            return httpx.Response(200, json=UPLOAD_FIXTURE)
        return httpx.Response(404, json={"error": {"message": "unexpected"}})

    return httpx.MockTransport(handler)


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(f"FAILED: {name}")


def with_token(token="test-token"):
    """Run fn with TOKEN_ENV set; restore afterwards."""
    def deco(fn):
        def wrapper():
            old = os.environ.get(TOKEN_ENV)
            os.environ[TOKEN_ENV] = token
            try:
                fn()
            finally:
                if old is None:
                    os.environ.pop(TOKEN_ENV, None)
                else:
                    os.environ[TOKEN_ENV] = old
        return wrapper
    return deco


def without_token(fn):
    def wrapper():
        old = os.environ.get(TOKEN_ENV)
        os.environ.pop(TOKEN_ENV, None)
        try:
            fn()
        finally:
            if old is not None:
                os.environ[TOKEN_ENV] = old
    return wrapper


@with_token()
def test_status_connected():
    st = GoogleDriveConnector().status()
    check("status connected", st["connected"] is True)
    check("status detail mentions env", TOKEN_ENV in st["detail"])


@without_token
def test_status_disconnected():
    st = GoogleDriveConnector().status()
    check("status disconnected", st["connected"] is False)
    check("status detail mentions env", TOKEN_ENV in st["detail"])


@with_token()
def test_drive_list():
    calls = []
    tools = GoogleDriveConnector(transport=make_transport(calls)).build_tools()
    res = tools["drive_list"]["func"]()
    check("list no error", "error" not in res)
    check("list count", res["count"] == 2)
    check("list first id", res["files"][0]["id"] == "1AbC2dEfGhIjKlMnOp")
    check("list fields", res["files"][0]["mimeType"] == "text/plain"
          and res["files"][0]["modifiedTime"] == "2026-10-14T08:12:33.000Z")
    check("list next page token", res.get("next_page_token") == "CgtnZXh0LXBhZ2U=")
    req = calls[0]
    check("list auth header", req.headers.get("authorization") == "Bearer test-token")
    q = parse_qs(urlparse(str(req.url)).query)
    check("list default query", q.get("q", [""])[0] == "trashed=false")
    check("list fields param", "files(id,name,mimeType,modifiedTime)" in q.get("fields", [""])[0])


@with_token()
def test_drive_list_custom_query():
    calls = []
    tools = GoogleDriveConnector(transport=make_transport(calls)).build_tools()
    res = tools["drive_list"]["func"](query="mimeType='image/png'", page_size=5)
    check("custom query no error", "error" not in res)
    q = parse_qs(urlparse(str(calls[0].url)).query)
    check("custom query passed", q.get("q", [""])[0] == "mimeType='image/png'")
    check("custom page size", q.get("pageSize", [""])[0] == "5")


@with_token()
def test_drive_search():
    calls = []
    tools = GoogleDriveConnector(transport=make_transport(calls)).build_tools()
    res = tools["drive_search"]["func"](name="notes")
    check("search no error", "error" not in res)
    check("search count", res["count"] == 1)
    check("search name", res["files"][0]["name"] == "notes.txt")
    q = parse_qs(urlparse(str(calls[0].url)).query).get("q", [""])[0]
    check("search query shape",
          q == "name contains 'notes' and trashed=false")


@with_token()
def test_drive_search_escapes_quotes():
    calls = []
    tools = GoogleDriveConnector(transport=make_transport(calls)).build_tools()
    res = tools["drive_search"]["func"](name="it's")
    check("search quote no error", "error" not in res)
    q = parse_qs(urlparse(str(calls[0].url)).query).get("q", [""])[0]
    check("search quote escaped", q == "name contains 'it\\'s' and trashed=false")


@with_token()
def test_drive_search_empty_name():
    tools = GoogleDriveConnector(
        transport=make_transport([])).build_tools()
    res = tools["drive_search"]["func"](name="   ")
    check("search empty -> error", "error" in res)


@with_token()
def test_drive_download_text():
    calls = []
    tools = GoogleDriveConnector(transport=make_transport(calls)).build_tools()
    res = tools["drive_download"]["func"](file_id="1AbC2dEfGhIjKlMnOp")
    check("download no error", "error" not in res)
    check("download text", res["text"] == "hello drive")
    check("download size", res["size"] == len("hello drive"))
    check("download not truncated", res["truncated"] is False)
    check("download not binary", res["binary"] is False)
    check("download alt=media",
          "alt=media" in str(calls[0].url))


@with_token()
def test_drive_download_binary():
    tools = GoogleDriveConnector(
        transport=make_transport([], mode="binary")).build_tools()
    res = tools["drive_download"]["func"](file_id="bin1")
    check("binary no error", "error" not in res)
    check("binary flagged", res["binary"] is True)
    check("binary text None", res["text"] is None)
    check("binary size", res["size"] == 10)


@with_token()
def test_drive_download_docs_error():
    tools = GoogleDriveConnector(
        transport=make_transport([], mode="docs_error")).build_tools()
    res = tools["drive_download"]["func"](file_id="2XyZ9wVuTsRqPoNmLk")
    check("docs file -> error", "error" in res)
    check("docs error mentions export", "Export" in res["error"])


@with_token()
def test_drive_upload():
    calls = []
    tools = GoogleDriveConnector(transport=make_transport(calls)).build_tools()
    res = tools["drive_upload"]["func"](
        name="hello.txt", content="hello world",
        folder_id="folder123")
    check("upload no error", "error" not in res)
    check("upload id", res["id"] == "9NewFileIdAbC123")
    check("upload name", res["name"] == "hello.txt")
    req = calls[0]
    ctype = req.headers.get("content-type", "")
    check("upload multipart/related", ctype.startswith("multipart/related"))
    body = req.content.decode("utf-8")
    meta = json.loads(body.split("\r\n\r\n", 1)[1].split("\r\n--", 1)[0])
    check("upload metadata name", meta["name"] == "hello.txt")
    check("upload metadata parents", meta.get("parents") == ["folder123"])
    check("upload metadata mime", meta["mimeType"] == "text/plain")
    check("upload body has content", "hello world" in body)
    check("upload auth header",
          req.headers.get("authorization") == "Bearer test-token")


@with_token()
def test_drive_upload_no_folder():
    calls = []
    tools = GoogleDriveConnector(transport=make_transport(calls)).build_tools()
    res = tools["drive_upload"]["func"](name="n.txt", content="x")
    check("upload no folder ok", "error" not in res)
    body = calls[0].content.decode("utf-8")
    meta = json.loads(body.split("\r\n\r\n", 1)[1].split("\r\n--", 1)[0])
    check("upload no parents key", "parents" not in meta)


@with_token()
def test_api_error_mapping():
    tools = GoogleDriveConnector(
        transport=make_transport([], mode="auth_error")).build_tools()
    res = tools["drive_list"]["func"]()
    check("401 -> error", "error" in res)
    check("401 status in message", "401" in res["error"])
    check("drive message kept", "Invalid Credentials" in res["error"])


@without_token
def test_tools_refuse_without_token():
    tools = GoogleDriveConnector().build_tools()
    for tname, kwargs in (("drive_list", {}),
                          ("drive_search", {"name": "x"}),
                          ("drive_download", {"file_id": "x"}),
                          ("drive_upload", {"name": "x", "content": "y"})):
        res = tools[tname]["func"](**kwargs)
        check(f"{tname} refuses without token",
              "error" in res and "not connected" in res["error"])


def test_explicit_token_override():
    old = os.environ.get(TOKEN_ENV)
    os.environ.pop(TOKEN_ENV, None)
    try:
        calls = []
        conn = GoogleDriveConnector(token="override-token",
                                    transport=make_transport(calls))
        check("override connected", conn.status()["connected"] is True)
        conn.build_tools()["drive_list"]["func"]()
        check("override token used",
              calls[0].headers.get("authorization") == "Bearer override-token")
    finally:
        if old is not None:
            os.environ[TOKEN_ENV] = old


def test_tool_names_prefixed():
    tools = GoogleDriveConnector().build_tools()
    check("four tools", set(tools) == {"drive_list", "drive_search",
                                       "drive_download", "drive_upload"})
    check("all prefixed", all(n.startswith("drive_") for n in tools))
    for meta in tools.values():
        check(f"{meta['name']} shape",
              meta["name"] and meta["func"] and meta["description"]
              and isinstance(meta["schema"], dict))


if __name__ == "__main__":
    print("== google drive connector ==")
    test_status_connected()
    test_status_disconnected()
    test_drive_list()
    test_drive_list_custom_query()
    test_drive_search()
    test_drive_search_escapes_quotes()
    test_drive_search_empty_name()
    test_drive_download_text()
    test_drive_download_binary()
    test_drive_download_docs_error()
    test_drive_upload()
    test_drive_upload_no_folder()
    test_api_error_mapping()
    test_tools_refuse_without_token()
    test_explicit_token_override()
    test_tool_names_prefixed()
    print("ALL GOOGLE DRIVE TESTS PASSED")
