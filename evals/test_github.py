"""Tests for agent/src/connectors/github.py — no network, no real credentials.

httpx.MockTransport serves realistic GitHub REST API fixtures; every assertion
checks real parsing behavior (issue/PR lists, base64 file decode, error bodies).
"""

import base64
import json
import os
import sys
from pathlib import Path

# Import root is agent/src (repo convention).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

import httpx

from connectors.github import CONNECTOR, GithubConnector, build_tools


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit("FAILED: " + name)


TOKEN = "ghp_faketoken123"

ISSUES_FIXTURE = [
    {
        "number": 12,
        "title": "Zalo adapter drops messages",
        "state": "open",
        "labels": [{"name": "bug"}, {"name": "p0"}],
        "created_at": "2026-10-14T02:00:00Z",
    },
    {
        "number": 9,
        "title": "Add TikTok channel",
        "state": "open",
        "labels": [],
        "created_at": "2026-10-13T01:00:00Z",
        # pull requests also appear on the /issues endpoint — must be filtered out
        "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/9"},
    },
    {
        "number": 3,
        "title": "Old closed issue",
        "state": "closed",
        "labels": [{"name": "wontfix"}],
        "created_at": "2026-10-10T01:00:00Z",
    },
]

ISSUE_DETAIL_FIXTURE = {
    "number": 12,
    "title": "Zalo adapter drops messages",
    "state": "open",
    "labels": [{"name": "bug"}],
    "body": "Messages sent within 1s of each other are lost.",
    "created_at": "2026-10-14T02:00:00Z",
}

COMMENTS_FIXTURE = [
    {
        "user": {"login": "son"},
        "body": "Reproduced on staging.",
        "created_at": "2026-10-14T03:00:00Z",
    },
    {
        "user": {"login": "bot"},
        "body": "Fixed in 6d8a4894.",
        "created_at": "2026-10-14T04:00:00Z",
    },
]

PRS_FIXTURE = [
    {
        "number": 21,
        "title": "P0-1 channels",
        "state": "open",
        "draft": False,
        "labels": [],
        "created_at": "2026-10-15T01:00:00Z",
    },
    {
        "number": 20,
        "title": "WIP: nix",
        "state": "open",
        "draft": True,
        "labels": [{"name": "infra"}],
        "created_at": "2026-10-14T01:00:00Z",
    },
]

FILE_TEXT = "# Boom\n\nMulti-channel agent.\n"
FILE_FIXTURE = {
    "type": "file",
    "encoding": "base64",
    "size": len(FILE_TEXT),
    "sha": "abc123",
    "content": base64.b64encode(FILE_TEXT.encode("utf-8")).decode("ascii"),
}

SEARCH_FIXTURE = {
    "total_count": 2,
    "items": [
        {"path": "agent/src/connectors/github.py", "repository": {"full_name": "TheOwlOps/Boom"}},
        {"path": "agent/src/cli.py", "repository": {"full_name": "TheOwlOps/Boom"}},
    ],
}


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_handler(request):
    path = request.url.path
    if path == "/repos/o/r/issues" and request.method == "GET":
        return httpx.Response(200, json=ISSUES_FIXTURE)
    if path == "/repos/o/r/issues/12" and request.method == "GET":
        return httpx.Response(200, json=ISSUE_DETAIL_FIXTURE)
    if path == "/repos/o/r/issues/12/comments" and request.method == "GET":
        return httpx.Response(200, json=COMMENTS_FIXTURE)
    if path == "/repos/o/r/pulls" and request.method == "GET":
        return httpx.Response(200, json=PRS_FIXTURE)
    if path == "/repos/o/r/contents/README.md" and request.method == "GET":
        return httpx.Response(200, json=FILE_FIXTURE)
    if path == "/search/code" and request.method == "GET":
        return httpx.Response(200, json=SEARCH_FIXTURE)
    return httpx.Response(404, json={"message": "Not Found"})


def authed_client():
    conn = GithubConnector(token=TOKEN, client=make_client(ok_handler))
    return conn


def test_status():
    check("status disconnected without token", GithubConnector(token="").status()["connected"] is False)
    check("status connected with token", GithubConnector(token=TOKEN).status()["connected"] is True)
    # env fallback
    os.environ["BOOM_GITHUB_TOKEN"] = TOKEN
    try:
        check("status reads env", GithubConnector().status()["connected"] is True)
    finally:
        del os.environ["BOOM_GITHUB_TOKEN"]


def test_contract():
    from connectors.base import Connector
    check("is Connector", isinstance(CONNECTOR, Connector))
    check("name", CONNECTOR.name == "github")
    tools = CONNECTOR.build_tools()
    expected = {"github_list_issues", "github_get_issue", "github_list_prs",
                "github_read_file", "github_search_code"}
    check("five tools", set(tools) == expected)
    for name, tool in tools.items():
        check(f"tool {name} shape",
              tool["name"] == name and callable(tool["func"])
              and tool["description"] and "properties" in tool["schema"])
        check(f"tool {name} prefixed", name.startswith("github_"))
    check("module build_tools", set(build_tools()) == expected)


def test_list_issues_filters_prs():
    conn = authed_client()
    res = conn.github_list_issues("o", "r")
    check("no error", "error" not in res)
    check("PR filtered out", res["total"] == 2)
    check("fields parsed", res["issues"][0]["number"] == 12
          and res["issues"][0]["labels"] == ["bug", "p0"])
    check("second issue labels", res["issues"][1]["number"] == 3
          and res["issues"][1]["labels"] == ["wontfix"])


def test_list_issues_validation():
    conn = authed_client()
    check("missing owner", "error" in conn.github_list_issues("", "r"))
    check("bad state", "error" in conn.github_list_issues("o", "r", state="bogus"))


def test_get_issue():
    conn = authed_client()
    res = conn.github_get_issue("o", "r", 12)
    check("no error", "error" not in res)
    check("title", res["title"] == "Zalo adapter drops messages")
    check("body kept", "lost" in res["body"])
    check("two comments", len(res["comments"]) == 2)
    check("comment author", res["comments"][0]["author"] == "son")
    check("bad number", "error" in conn.github_get_issue("o", "r", "abc"))
    check("404 issue", "error" in conn.github_get_issue("o", "r", 999))


def test_list_prs():
    conn = authed_client()
    res = conn.github_list_prs("o", "r")
    check("no error", "error" not in res)
    check("two prs", res["total"] == 2)
    check("draft flag", res["pull_requests"][1]["draft"] is True)
    check("labels", res["pull_requests"][1]["labels"] == ["infra"])


def test_read_file():
    conn = authed_client()
    res = conn.github_read_file("o", "r", "README.md")
    check("no error", "error" not in res)
    check("content decoded", res["content"] == FILE_TEXT)
    check("sha kept", res["sha"] == "abc123")
    check("not truncated", res["truncated"] is False)
    check("abs path rejected", "error" in conn.github_read_file("o", "r", "/etc/passwd"))
    check("traversal rejected", "error" in conn.github_read_file("o", "r", "../secret"))
    check("404 file", "error" in conn.github_read_file("o", "r", "NOPE.md"))


def test_read_file_directory():
    def handler(request):
        return httpx.Response(200, json=[{"name": "a.py", "type": "file"}])
    conn = GithubConnector(token=TOKEN, client=make_client(handler))
    check("directory rejected", "error" in conn.github_read_file("o", "r", "agent"))


def test_search_code():
    conn = authed_client()
    res = conn.github_search_code("BOOM_GITHUB_TOKEN")
    check("no error", "error" not in res)
    check("total_count", res["total_count"] == 2)
    check("path+repo", res["items"][0]["path"] == "agent/src/connectors/github.py"
          and res["items"][0]["repository"] == "TheOwlOps/Boom")
    check("empty query", "error" in conn.github_search_code("  "))


def test_auth_errors():
    def handler_401(request):
        return httpx.Response(401, json={"message": "Bad credentials"})
    conn = GithubConnector(token="bad", client=make_client(handler_401))
    res = conn.github_list_issues("o", "r")
    check("401 becomes error", "error" in res and "Bad credentials" in res["error"])

    def handler_403(request):
        return httpx.Response(403, json={"message": "API rate limit exceeded"})
    conn = GithubConnector(token=TOKEN, client=make_client(handler_403))
    res = conn.github_search_code("x")
    check("403 becomes error", "error" in res and "rate limit" in res["error"])


def test_no_token_no_request():
    def exploding_handler(request):
        raise AssertionError("no HTTP request should happen without a token")
    conn = GithubConnector(token="", client=make_client(exploding_handler))
    for fn in (lambda: conn.github_list_issues("o", "r"),
               lambda: conn.github_get_issue("o", "r", 1),
               lambda: conn.github_list_prs("o", "r"),
               lambda: conn.github_read_file("o", "r", "f"),
               lambda: conn.github_search_code("x")):
        check("no token -> error, no request", "error" in fn())


def test_network_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")
    conn = GithubConnector(token=TOKEN, client=make_client(handler))
    res = conn.github_list_prs("o", "r")
    check("ConnectError becomes error", "error" in res and "ConnectError" in res["error"])


def test_headers_sent():
    seen = {}
    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["accept"] = request.headers.get("accept")
        seen["api_version"] = request.headers.get("x-github-api-version")
        return httpx.Response(200, json=[])
    conn = GithubConnector(token=TOKEN, client=make_client(handler))
    conn.github_list_prs("o", "r")
    check("bearer header", seen["auth"] == f"Bearer {TOKEN}")
    check("accept header", seen["accept"] == "application/vnd.github+json")
    check("api version header", seen["api_version"] == "2022-11-28")


if __name__ == "__main__":
    test_status()
    test_contract()
    test_list_issues_filters_prs()
    test_list_issues_validation()
    test_get_issue()
    test_list_prs()
    test_read_file()
    test_read_file_directory()
    test_search_code()
    test_auth_errors()
    test_no_token_no_request()
    test_network_error()
    test_headers_sent()
    print("ALL GITHUB TESTS PASSED")
