"""Offline test of the CDP browser tool: no real Chrome required.

Everything runs against real listening sockets on 127.0.0.1: a WebSocket echo
server for the RFC6455 client, and a fake Chrome DevTools server (HTTP /json
endpoints + a page socket) for the CDP path. A machine with no browser at all
still passes: the last case asserts the tools report an error instead of a
fabricated success.
"""
import base64
import hashlib
import json
import os
import socket
import sys
import threading
from pathlib import Path
from urllib.parse import unquote

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from tools import browser_tool
from tools.browser_tool import BrowserTool, find_chrome
from tools.ws_client import (
    GUID,
    OP_CLOSE,
    OP_CONT,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    FrameReader,
    WSError,
    WebSocket,
    decode_frame,
    encode_frame,
    parse_ws_url,
)


class Server:
    """Real listening socket on 127.0.0.1. One thread per connection."""

    def __init__(self, handler):
        self.handler = handler
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._threads = []
        self._accept = threading.Thread(target=self._serve, daemon=True)
        self._accept.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            thread = threading.Thread(target=self._handle, args=(conn,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _handle(self, conn):
        try:
            conn.settimeout(10)
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                request += chunk
            head, _, rest = request.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            self.handler(conn, lines[0], headers, rest)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        try:
            self.sock.close()
        except OSError:
            pass
        for thread in self._threads:
            thread.join(timeout=2)
        self._accept.join(timeout=2)


def accept_ws(conn, headers):
    key = headers["sec-websocket-key"]
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
    conn.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii")
    )


def echo_handler(seen):
    """Echo text back, fragment on demand, ping on demand."""

    def handle(conn, request_line, headers, rest):
        accept_ws(conn, headers)
        reader = FrameReader(conn, 10)
        reader.buf = rest
        while True:
            fin, opcode, payload, masked = decode_frame(reader.read)
            seen.setdefault("masked", []).append(masked)
            if opcode == OP_CLOSE:
                return
            if opcode == OP_PING:
                conn.sendall(encode_frame(payload, OP_PONG, mask=False))
                continue
            if opcode != OP_TEXT:
                continue
            text = payload.decode("utf-8")
            if text == "fragment":
                conn.sendall(encode_frame("part-one-", OP_TEXT, fin=False, mask=False))
                conn.sendall(encode_frame("part-two", OP_CONT, fin=True, mask=False))
                continue
            if text == "ping-me":
                conn.sendall(encode_frame(b"p", OP_PING, mask=False))
                _, pong_opcode, pong_payload, _ = decode_frame(reader.read)
                seen["pong"] = (pong_opcode, pong_payload)
                conn.sendall(encode_frame("after-ping", OP_TEXT, mask=False))
                continue
            conn.sendall(encode_frame(payload, OP_TEXT, mask=False))

    return handle


def test_frame_codec():
    seen = {}
    server = Server(echo_handler(seen))
    try:
        ws = WebSocket.connect(f"ws://127.0.0.1:{server.port}/echo", timeout=10)
        small = "small payload"
        ws.send_text(small)
        assert ws.recv(timeout=10) == small, "7-bit length payload lost"

        medium = "m" * 200
        ws.send_text(medium)
        assert ws.recv(timeout=10) == medium, "16-bit length payload lost"

        large = "L" * 70000
        ws.send_text(large)
        assert ws.recv(timeout=10) == large, "64-bit length payload lost"

        ws.send_text("fragment")
        assert ws.recv(timeout=10) == "part-one-part-two", "continuation frames not reassembled"

        ws.send_text("ping-me")
        assert ws.recv(timeout=10) == "after-ping", "a server ping was not answered"
        assert seen["pong"] == (OP_PONG, b"p"), f"wrong pong: {seen.get('pong')!r}"
        assert seen["masked"] and all(seen["masked"]), "client frames were not masked"
        ws.close()

        assert parse_ws_url("ws://host:1234/a/b?c=1") == ("host", 1234, "/a/b?c=1")
        assert parse_ws_url("ws://host") == ("host", 80, "/")
        assert parse_ws_url("wss://host/x") == ("host", 443, "/x")
        assert parse_ws_url("wss://host:8443") == ("host", 8443, "/")
        for bad in ("http://host/x", "//host/x"):
            try:
                parse_ws_url(bad)
                raise AssertionError(f"{bad} must be rejected")
            except WSError:
                pass
    finally:
        server.stop()
    print("[PASS] RFC6455 frame codec round-trip, masking, ping/pong and fragmentation.")


def cdp_result(message, state):
    method = message.get("method")
    params = message.get("params") or {}
    if method == "Page.navigate":
        return {"result": {"frameId": "F1"}}
    if method.startswith("Input."):
        state.setdefault("input", []).append((method, params))
        return {"result": {}}
    if method == "Runtime.evaluate":
        expression = params.get("expression", "")
        # The click/fill scripts are recognised by what they ask the page for, and answer
        # with the state this test sets: a box, or a field that kept a different value.
        if "getBoundingClientRect" in expression:
            box = state["box"]
            # Mirrors the real script: the element's centre, measured after scrollIntoView.
            centre = {"x": box["x"] + box["w"] / 2, "y": box["y"] + box["h"] / 2,
                      "w": box["w"], "h": box["h"]}
            return {"result": {"result": {"type": "object",
                                          "value": None if state.get("no_element") else centre}}}
        if "dispatchEvent" in expression:
            return {"result": {"result": {"type": "object", "value": {
                "tag": state.get("fill_tag", "input"), "value": state["fill_value"]}}}}
        if "readyState" in expression and "location.href" not in expression:
            return {"result": {"result": {"type": "string", "value": "complete"}}}
        if "location.href" in expression:
            return {
                "result": {
                    "result": {
                        "type": "object",
                        "value": {
                            "url": "http://example.test/",
                            "title": "Fake Page",
                            "state": "complete",
                        },
                    }
                }
            }
        return {"result": {"result": state["eval_result"]}}
    if method == "Browser.close":
        return {"result": {}}
    return {"error": {"code": -32601, "message": f"not implemented: {method}"}}


def cdp_http_body(method, path, state):
    port = state["port"]
    targets = state.setdefault("targets", [])
    if path.startswith("/json/version"):
        return {"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/fake"}
    if path.startswith("/json/list"):
        return targets
    if path.startswith("/json/close/"):
        target_id = path.rsplit("/", 1)[-1]
        state["targets"] = [t for t in targets if t["id"] != target_id]
        return {"closed": target_id}
    if path.startswith("/json/new"):
        url = unquote(path.split("url=", 1)[1]) if "url=" in path else "about:blank"
        number = len(targets) + 1
        target = {"id": f"tab{number}", "type": "page", "title": f"Tab {number}", "url": url,
                  "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/tab{number}"}
        targets.append(target)
        return target
    raise AssertionError(f"unexpected HTTP request: {method} {path}")


def cdp_handler(state):
    def handle(conn, request_line, headers, rest):
        state.setdefault("paths", []).append(request_line.split(" ")[1])
        if headers.get("upgrade", "").lower() == "websocket":
            accept_ws(conn, headers)
            reader = FrameReader(conn, 10)
            reader.buf = rest
            while True:
                _, opcode, payload, _ = decode_frame(reader.read)
                if opcode == OP_CLOSE:
                    return
                if opcode != OP_TEXT:
                    continue
                message = json.loads(payload.decode("utf-8"))
                state.setdefault("methods", []).append(message.get("method"))
                if state.get("error_next"):
                    state["error_next"] = False
                    reply = {"id": message["id"], "error": {"code": -32000, "message": "boom: no such node"}}
                else:
                    reply = {"id": message["id"], **cdp_result(message, state)}
                conn.sendall(encode_frame(json.dumps(reply), OP_TEXT, mask=False))
            return
        method, path = request_line.split(" ")[:2]
        state.setdefault("http", []).append(f"{method} {path}")
        body = json.dumps(cdp_http_body(method, path, state)).encode("utf-8")
        conn.sendall(
            (
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
            + body
        )

    return handle


def test_cdp_tools():
    state = {"eval_result": {"type": "number", "value": 42}}
    server = Server(cdp_handler(state))
    state["port"] = server.port
    try:
        browser_tool._SESSION = BrowserTool(port=server.port, timeout=10)
        tools = browser_tool.build_tools()

        assert set(tools) == {
            "browser_navigate",
            "browser_eval",
            "browser_screenshot",
            "browser_close",
            "browser_click",
            "browser_fill",
            "browser_tabs",
            "browser_open_tab",
            "browser_use_tab",
            "browser_close_tab",
        }, f"unexpected tool names: {sorted(tools)}"
        for name, tool in tools.items():
            assert tool["name"] == name and callable(tool["func"]), f"malformed tool {name}"
            schema = tool["schema"]
            assert schema["type"] == "object" and isinstance(schema["properties"], dict)
            assert isinstance(schema["required"], list)

        navigated = tools["browser_navigate"]["func"]("http://example.test/")
        assert navigated == {
            "status": "navigated",
            "url": "http://example.test/",
            "title": "Fake Page",
        }, f"navigate did not reach the page: {navigated}"
        assert "Page.navigate" in state.get("methods", []), f"no Page.navigate sent: {state.get('methods')}"
        assert any(entry.startswith("PUT /json/new") for entry in state.get("http", [])), (
            f"page was not opened with PUT /json/new: {state.get('http')}"
        )

        evaluated = tools["browser_eval"]["func"]("6*7")
        assert evaluated == {"result": 42}, f"Runtime.evaluate value lost: {evaluated}"

        state["error_next"] = True
        failed = tools["browser_eval"]["func"]("boom()")
        assert isinstance(failed, dict) and "error" in failed, f"CDP error not surfaced: {failed}"
        assert "boom" in failed["error"], f"CDP error text lost: {failed}"

        recovered = tools["browser_eval"]["func"]("1+1")
        assert recovered == {"result": 42}, f"session unusable after a CDP error: {recovered}"

        # Tabs are real targets: opening one attaches to it, and the page socket shows it.
        listed = tools["browser_tabs"]["func"]()
        assert listed["active"] == 0 and len(listed["tabs"]) == 1, listed
        opened = tools["browser_open_tab"]["func"]("http://example.test/second")
        assert (opened["status"], opened["tab"], opened["index"]) == ("opened", "tab2", 1), opened
        assert state["targets"][1]["url"] == "http://example.test/second", state["targets"]

        assert tools["browser_use_tab"]["func"](0)["status"] == "switched"
        assert state["paths"][-1] == "/devtools/page/tab1", state["paths"][-1]
        for bad in ("browser_use_tab", "browser_close_tab"):
            assert "no tab at index 9" in tools[bad]["func"](9)["error"], (bad, state)
        closed = tools["browser_close_tab"]["func"](1)
        assert closed == {"status": "closed", "tab": "tab2"}, closed
        assert [t["id"] for t in tools["browser_tabs"]["func"]()["tabs"]] == ["tab1"]
        print("[PASS] CDP tabs: list, open, switch (real socket), close, and out-of-range refused.")

        # A click goes to the element's real centre, as mouse events the page receives.
        state["box"] = {"x": 40.0, "y": 20.0, "w": 100.0, "h": 30.0}
        clicked = tools["browser_click"]["func"]("#go")
        assert clicked == {"status": "clicked", "selector": "#go", "x": 90.0, "y": 35.0}, clicked
        kinds = [params.get("type") for method, params in state["input"]]
        assert kinds == ["mouseMoved", "mousePressed", "mouseReleased"], kinds
        assert all(params["x"] == 90.0 for _, params in state["input"]), state["input"]

        state["no_element"] = True
        assert "no element matches" in tools["browser_click"]["func"]("#ghost")["error"]
        state["no_element"] = False
        state["box"] = {"x": 1.0, "y": 1.0, "w": 0.0, "h": 0.0}
        assert "not visible" in tools["browser_click"]["func"]("#hidden")["error"]

        # A field that keeps the text is filled; one that keeps something else is an error.
        state["fill_value"] = "hello bot"
        assert tools["browser_fill"]["func"]("#name", "hello bot") == {
            "status": "filled", "selector": "#name", "tag": "input", "value": "hello bot"}
        state["fill_value"] = ""
        refused = tools["browser_fill"]["func"]("#readonly", "hello bot")
        assert "error" in refused and "kept" in refused["error"], refused
        print("[PASS] CDP click/fill act on the element's box and never report a missed fill.")

        assert tools["browser_close"]["func"]() == {"status": "closed"}
        assert browser_tool._SESSION is None, "browser_close left the session registered"
    finally:
        browser_tool._SESSION = None
        server.stop()
    print("[PASS] CDP navigate/evaluate/error path over a fake DevTools server.")


def test_no_browser():
    real_which = browser_tool.shutil.which
    real_paths = browser_tool._WINDOWS_PATHS
    real_override = os.environ.get("BOOM_CHROME_PATH")
    browser_tool._SESSION = None
    try:
        os.environ["BOOM_CHROME_PATH"] = str(root_dir / "no-such-chrome-here.exe")
        browser_tool.shutil.which = lambda *args, **kwargs: None
        browser_tool._WINDOWS_PATHS = ()
        assert find_chrome() is None, "a missing BOOM_CHROME_PATH must not fall through to a real browser"

        tools = browser_tool.build_tools()
        navigated = tools["browser_navigate"]["func"]("http://example.test/")
        assert isinstance(navigated, dict) and "error" in navigated, navigated
        assert "no Chrome" in navigated["error"], navigated
        evaluated = tools["browser_eval"]["func"]("1+1")
        assert "error" in evaluated, evaluated
        screenshot = tools["browser_screenshot"]["func"]()
        assert "error" in screenshot, screenshot
        for tool, args in (("browser_tabs", ()), ("browser_open_tab", ("http://x.test/",)),
                           ("browser_use_tab", (0,)), ("browser_close_tab", (0,)),
                           ("browser_click", ("#x",)), ("browser_fill", ("#x", "y"))):
            out = tools[tool]["func"](*args)
            assert "error" in out, (tool, out)
        assert tools["browser_close"]["func"]() == {"status": "closed"}

        direct = BrowserTool().goto_url("http://example.test/")
        assert isinstance(direct, dict) and "error" in direct, direct
    finally:
        browser_tool._SESSION = None
        browser_tool.shutil.which = real_which
        browser_tool._WINDOWS_PATHS = real_paths
        if real_override is None:
            os.environ.pop("BOOM_CHROME_PATH", None)
        else:
            os.environ["BOOM_CHROME_PATH"] = real_override
    print("[PASS] No browser discoverable: tools return an error and do not raise.")


def main():
    test_frame_codec()
    test_cdp_tools()
    test_no_browser()
    print("ALL BROWSER CDP TESTS PASSED.")


if __name__ == "__main__":
    main()
