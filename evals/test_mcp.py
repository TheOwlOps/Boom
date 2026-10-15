"""End-to-end check of the MCP client against the fixtures it starts itself.

Both fixtures are written by this eval and use the standard library only: a stdio server
spawned as a child process, and a streamable-HTTP server on a loopback socket that answers
`initialize` over SSE and everything else as JSON, then forgets its session so the client
has to reconnect. So the suite needs no network, no `mcp` SDK and no third-party server.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from environment import AgentEnvironment
from mcp.client import MCPError
from mcp.registry import load_server_configs, register_mcp_tools
from security.approval import SAFE_TOOLS

SERVER_SOURCE = r'''
import json, sys, time

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

# A server that banners on stdout is tolerated: the line below is not JSON.
print("fixture server starting")

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture", "version": "1.0"}}})
        # A server->client request: the client must answer it, not wait on it.
        send({"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"})
        # A notification carries no id and must not be mistaken for a response.
        send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}})
    elif method == "tools/list":
        if not (msg.get("params") or {}).get("cursor"):
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "tools": [{"name": "echo", "description": "Echo text back.",
                           "inputSchema": {"type": "object",
                                           "properties": {"text": {"type": "string"}},
                                           "required": ["text"]}}],
                "nextCursor": "page2"}})
        else:
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "tools": [{"name": "explode", "description": "Always fails.",
                           "inputSchema": {"type": "object", "properties": {}}},
                          {"name": "hang", "description": "Never answers.",
                           "inputSchema": {"type": "object", "properties": {}}}]}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        if name == "echo":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "content": [{"type": "text",
                             "text": (params.get("arguments") or {}).get("text", "")}]}})
        elif name == "explode":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "content": [{"type": "text", "text": "boom"}], "isError": True}})
        elif name == "hang":
            time.sleep(300)
    # notifications/initialized, and our own reply to the client: no reply either way.
'''


HTTP_TOKEN = "fixture-token"
_SESSIONS = set()
_INITS = []  # every handshake, so a test can prove a call did not secretly reconnect


class HTTPFixture(BaseHTTPRequestHandler):
    """A streamable-HTTP MCP server: SSE for initialize, JSON afterwards, 404 once forgotten."""

    def log_message(self, *args):
        pass

    def _reply(self, code, payload=None, headers=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        if self.headers.get("authorization") != f"Bearer {HTTP_TOKEN}":
            return self._reply(401, {"error": "bad token"})
        method, rid = msg.get("method"), msg.get("id")
        if method != "initialize" and self.headers.get("mcp-session-id") not in _SESSIONS:
            return self._reply(404)  # this is what makes the client reconnect
        if method == "initialize":
            _SESSIONS.add("sess-1")
            _INITS.append(rid)
            body = json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "http-fixture", "version": "1.0"}}}).encode()
            payload = b"event: message\ndata: " + body + b"\n\n"
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("mcp-session-id", "sess-1")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if rid is None:
            return self._reply(202)  # a notification is accepted, never answered
        if method == "tools/list":
            return self._reply(200, {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "echo", "description": "Echo over HTTP.",
                 "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
                {"name": "forget", "description": "Forgets the session.",
                 "inputSchema": {"type": "object", "properties": {}}},
                {"name": "sse_echo", "description": "Echoes, answered as an event stream.",
                 "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
                {"name": "server_error", "description": "Always answers HTTP 500.",
                 "inputSchema": {"type": "object", "properties": {}}}]}})
        params = msg.get("params") or {}
        if params.get("name") == "forget":
            _SESSIONS.clear()
        if params.get("name") == "server_error":
            return self._reply(500, {"jsonrpc": "2.0", "id": rid,
                                     "error": {"code": -32000, "message": "fixture exploded"}})
        if params.get("name") == "sse_echo":
            # A plain call answered over SSE, not JSON: the client has to read the stream.
            body = json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                {"type": "text", "text": (params.get("arguments") or {}).get("text", "")}]}}).encode()
            payload = b"event: message\ndata: " + body + b"\n\n"
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("mcp-session-id", "sess-1")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        text = (params.get("arguments") or {}).get("text") or "forgotten"
        return self._reply(200, {"jsonrpc": "2.0", "id": rid,
                                 "result": {"content": [{"type": "text", "text": text}]}})


async def test_mcp_client():
    work = Path(tempfile.mkdtemp(prefix="boom_mcp_"))
    server_py = work / "fixture_server.py"
    server_py.write_text(SERVER_SOURCE, encoding="utf-8")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), HTTPFixture)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    http_url = f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
    # The header value is a reference, not the token: a secret in mcp.json is a secret in the repo.
    os.environ["BOOM_TEST_MCP_TOKEN"] = HTTP_TOKEN
    (work / "mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                # timeout is short so the hung-tool case does not cost the suite 30s
                "fixture": {"command": [sys.executable, str(server_py)], "timeout": 1.0},
                "remote": {"url": http_url,
                           "headers": {"Authorization": "Bearer ${BOOM_TEST_MCP_TOKEN}"}},
                "remote_bad": {"url": http_url, "headers": {"Authorization": "Bearer wrong"}},
            }
        }),
        encoding="utf-8",
    )

    configs = load_server_configs(work)
    assert list(configs) == ["fixture", "remote", "remote_bad"], configs

    env = AgentEnvironment(root_dir=root_dir, db_path=work / "state.db", yolo=True)
    try:
        # echo + explode + hang on stdio (paginated) and echo + forget over HTTP; the server
        # whose token does not match never registers at all.
        count = register_mcp_tools(env, work)
        assert count == 7, count

        tools = env.get_tools()
        for name in ("mcp__fixture__echo", "mcp__fixture__explode", "mcp__fixture__hang"):
            assert name in tools, sorted(tools)

        # The server's inputSchema must reach the provider as a real parameter definition.
        schemas = {s["name"]: s for s in env.get_tools_schema()}
        assert schemas["mcp__fixture__echo"]["parameters"]["properties"]["text"]["type"] == "string"
        assert schemas["mcp__fixture__echo"]["description"] == "Echo text back."

        # A discovered tool is a third-party tool: it must not be on the approval allow-list.
        assert "mcp__fixture__echo" not in SAFE_TOOLS

        echoed = await tools["mcp__fixture__echo"]["func"](text="hello mcp")
        assert echoed == "hello mcp", echoed

        # An isError result must arrive as the error envelope, not as success text.
        failed = await tools["mcp__fixture__explode"]["func"]()
        assert json.loads(failed) == {"error": "boom"}, failed

        # A server that never answers must raise, not hang the turn.
        try:
            await tools["mcp__fixture__hang"]["func"]()
            raise AssertionError("hung tool call did not raise")
        except MCPError as e:
            assert "timed out" in str(e), e
        print("[PASS] MCP discovery, schema forwarding, call and isError verified.")

        # Over HTTP: an SSE handshake, a JSON tool list, and a call that carries the session
        # the server handed out. The wrong-token server was refused and never registered.
        assert "mcp__remote_bad__echo" not in tools, sorted(tools)
        assert await tools["mcp__remote__echo"]["func"](text="over http") == "over http"

        # The server forgets the session mid-conversation and answers 404. The client must
        # start a new one and finish the call rather than hand the turn a transport error.
        assert (await tools["mcp__remote__forget"]["func"]()) == "forgotten"
        assert _SESSIONS == set(), _SESSIONS
        assert await tools["mcp__remote__echo"]["func"](text="after reconnect") == "after reconnect"
        print("[PASS] MCP over streamable HTTP: SSE handshake, session header, reconnect on 404.")

        # A tool call answered as an event stream, not a JSON body: the reply is in the stream.
        handshakes = len(_INITS)
        assert await tools["mcp__remote__sse_echo"]["func"](text="over sse") == "over sse"
        assert len(_INITS) == handshakes, "an SSE reply is a reply, not a reason to reconnect"

        # A 5xx is the server failing, not a session to re-establish: it must surface as an
        # error without a second handshake, and never as an empty success.
        try:
            await tools["mcp__remote__server_error"]["func"]()
            raise AssertionError("an HTTP 500 did not raise")
        except MCPError as e:
            assert "500" in str(e) and "tools/call" in str(e), e
        assert len(_INITS) == handshakes, "a 500 must not be answered by re-handshaking"
        print("[PASS] SSE tool call read from the stream; a 500 raised without reconnecting.")

        # BOOM_MCP_SERVERS overrides the file for one-off runs.
        os.environ["BOOM_MCP_SERVERS"] = json.dumps({"mcpServers": {
            "inline": {"command": [sys.executable, str(server_py)]}}})
        try:
            inline = load_server_configs(work)
            assert list(inline) == ["inline"], inline
        finally:
            del os.environ["BOOM_MCP_SERVERS"]
        print("[PASS] MCP config precedence verified (env over mcp.json).")

        # A server that cannot start must be reported and skipped, not fatal.
        broken_dir = work / "broken"
        broken_dir.mkdir()
        (broken_dir / "mcp.json").write_text(
            json.dumps({"mcpServers": {"nope": {"command": ["definitely-not-a-real-binary"]}}}),
            encoding="utf-8",
        )
        assert register_mcp_tools(env, broken_dir) == 0
        print("[PASS] Unavailable MCP server skipped without raising.")
    finally:
        httpd.shutdown()
        httpd.server_close()
        os.environ.pop("BOOM_TEST_MCP_TOKEN", None)
        for client in env.mcp_clients.values():
            client.close()
        for closer in ("memory_store", "state_db", "delivery_ledger"):
            if hasattr(env, closer):
                getattr(env, closer).close()
        shutil.rmtree(work, ignore_errors=True)

    print("ALL MCP TESTS PASSED.")


if __name__ == "__main__":
    asyncio.run(test_mcp_client())
