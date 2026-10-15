"""Minimal MCP (Model Context Protocol) client over stdio or streamable HTTP.

Boom speaks MCP with the standard library alone: the reference `mcp` package pulls in
anyio task groups and an httpx stack to do what is newline-delimited JSON-RPC over a
pipe. Same wire format, one server per process, calls serialized per server.

An entry with a `url` speaks the streamable-HTTP transport instead: one POST per JSON-RPC
message, the reply in a JSON body or an SSE stream, and the server's `Mcp-Session-Id`
echoed back on every later request. A transport failure or an expired session costs one
reconnect, not the turn.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

PROTOCOL_VERSION = "2025-06-18"
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
DEFAULT_TIMEOUT = 30.0
STDERR_TAIL_LINES = 20


class MCPError(RuntimeError):
    """A JSON-RPC error from the server, or a transport failure reaching it."""

    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


def content_text(blocks: List[Dict[str, Any]]) -> str:
    """Flatten an MCP content array into the text a tool observation can carry."""
    parts = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text") or "")
        elif kind == "resource":
            resource = block.get("resource") or {}
            parts.append(resource.get("text") or f"[resource {resource.get('uri', '')}]")
        else:
            parts.append(f"[{kind or 'unknown'} content omitted]")
    return "\n".join(p for p in parts if p)


def expand_env(value: str) -> str:
    """`${VAR}` in a header value comes from the environment; a token never goes in mcp.json."""
    return ENV_REF.sub(lambda m: os.getenv(m.group(1), ""), str(value))


class MCPClient:
    """One MCP server: spawn or connect, handshake, list tools, call tools."""

    def __init__(
        self,
        name: str,
        command: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        url: str = "",
        headers: Optional[Dict[str, str]] = None,
        oauth: Optional[Dict[str, str]] = None,
    ):
        self.name = name
        self.timeout = float(timeout)
        self.command = list(command or [])
        self.env = dict(env or {})
        self.cwd = str(cwd) if cwd else None
        self.url = str(url or "")
        self.headers = {k: expand_env(v) for k, v in (headers or {}).items()}
        # Client-credentials OAuth: the token is fetched on first use and fetched again when a
        # server answers 401, so a rotated token does not need a restart.
        self.oauth = {k: expand_env(v) for k, v in (oauth or {}).items()}
        self._access_token = ""
        self._token_expiry = 0.0
        self.tools: List[Dict[str, Any]] = []
        self.server_info: Dict[str, Any] = {}

        self._proc: Optional[subprocess.Popen] = None
        self._queue: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._lock = threading.Lock()
        self._next_id = 1
        self._stderr: List[str] = []
        self._http: Optional[httpx.Client] = None
        self._session_id = ""

    # ------------------------------------------------------------------ process

    def start(self) -> "MCPClient":
        """Connect to the server and complete the initialize handshake."""
        if self.url:
            return self._start_http()
        if self._proc is not None:
            return self
        if not self.command:
            raise MCPError(f"MCP server '{self.name}' declares no command")

        exe = shutil.which(self.command[0]) or self.command[0]
        env = None
        if self.env:
            env = dict(os.environ)
            env.update(self.env)

        try:
            self._proc = subprocess.Popen(
                [exe] + self.command[1:],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self.cwd,
                env=env,
            )
        except OSError as e:
            raise MCPError(f"MCP server '{self.name}' could not start '{exe}': {e}") from e

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "boom", "version": "0.1"},
            },
        )
        self._notify("notifications/initialized")
        self.server_info = result.get("serverInfo") or {}
        self.tools = self._list_tools()
        return self

    # ------------------------------------------------------------------- http
    def _start_http(self) -> "MCPClient":
        if self._http is not None:
            return self
        result = self._handshake()
        self._notify("notifications/initialized")
        self.server_info = result.get("serverInfo") or {}
        self.tools = self._list_tools()
        return self

    def _handshake(self) -> dict:
        return self._request_http("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "boom", "version": "0.1"},
        })

    def _reconnect(self):
        """New session for a server that dropped us or forgot the old one.

        The tool list is not fetched again: registration belongs to the registry, and what a
        reconnecting turn needs is for this one call to go through. shortcut: a server that
        restarts with a different tool set is picked up on the next daemon start.
        """
        self._session_id = ""
        self._discard_http()
        self._handshake()
        self._notify("notifications/initialized")

    def _discard_http(self):
        http, self._http = self._http, None
        if http is not None:
            try:
                http.close()
            except Exception:
                pass

    def _fetch_token(self, force: bool = False) -> str:
        """A client-credentials token from the server's own token endpoint.

        Client credentials only: the interactive authorization-code flow needs a browser and a
        redirect the daemon does not have. shortcut: a server that only offers authorization-code
        cannot be reached this way.
        """
        if self._access_token and not force and time.monotonic() < self._token_expiry:
            return self._access_token
        token_url = self.oauth.get("token_url", "")
        if not token_url:
            raise MCPError(f"MCP server '{self.name}' declares oauth without a token_url")
        data = {
            "grant_type": "client_credentials",
            "client_id": self.oauth.get("client_id", ""),
            "client_secret": self.oauth.get("client_secret", ""),
        }
        if self.oauth.get("scope"):
            data["scope"] = self.oauth["scope"]
        try:
            resp = httpx.post(token_url, data=data, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise MCPError(f"MCP server '{self.name}' token endpoint unreachable: {e}") from e
        if resp.status_code >= 400:
            raise MCPError(
                f"MCP server '{self.name}' token endpoint answered HTTP "
                f"{resp.status_code}: {resp.text[:200]}"
            )
        try:
            body = resp.json() or {}
        except ValueError as e:
            raise MCPError(f"MCP server '{self.name}' token endpoint sent no JSON: {e}") from e
        token = body.get("access_token")
        if not token:
            raise MCPError(f"MCP server '{self.name}' token endpoint returned no access_token")
        self._access_token = str(token)
        # A minute of slack, so a token is not used up in the middle of a call.
        try:
            lifetime = float(body.get("expires_in", 3600))
        except (TypeError, ValueError):
            lifetime = 3600.0
        self._token_expiry = time.monotonic() + max(0.0, lifetime - 60.0)
        return self._access_token

    def _post_http(self, payload: dict, expect_id: Optional[int]) -> Optional[dict]:
        """One POST. A dead connection, an expired session or a stale token is retried once."""
        for attempt in (1, 2):
            headers = dict(self.headers)
            if self.oauth:
                headers["Authorization"] = f"Bearer {self._fetch_token()}"
            if self._session_id:
                headers["mcp-session-id"] = self._session_id
            # Both encodings are legal replies to one POST; the server picks.
            headers["Accept"] = "application/json, text/event-stream"
            if self._http is None:
                self._http = httpx.Client(timeout=self.timeout)
            try:
                resp = self._http.post(self.url, json=payload, headers=headers)
            except httpx.HTTPError as e:
                self._discard_http()
                if attempt == 2 or payload.get("method") == "initialize":
                    raise MCPError(
                        f"MCP server '{self.name}' unreachable at {self.url}: {e}"
                    ) from e
                self._reconnect()
                continue
            if resp.status_code == 401 and self.oauth and attempt == 1:
                # The token the server last handed out is no longer good for it: get a new one.
                self._fetch_token(force=True)
                continue
            if resp.status_code == 404 and self._session_id and payload.get("method") != "initialize":
                self._discard_http()
                if attempt == 2:
                    raise MCPError(f"MCP server '{self.name}' rejected session {self._session_id}")
                self._reconnect()
                continue
            if resp.status_code >= 400:
                raise MCPError(
                    f"MCP server '{self.name}' answered HTTP {resp.status_code} to "
                    f"{payload.get('method')}: {resp.text[:200]}"
                )
            session = resp.headers.get("mcp-session-id")
            if session:
                self._session_id = session
            if expect_id is None:
                return None  # a notification is answered with 202 and no body
            return self._decode(resp, expect_id, payload.get("method"))
        return None

    def _decode(self, resp: "httpx.Response", expect_id: int, method: str) -> dict:
        """The reply, whether the server sent a JSON body or an SSE stream."""
        if "text/event-stream" in resp.headers.get("content-type", ""):
            # The whole body is read before parsing: a server-sent request would need a live
            # stream to answer mid-call, and no server Boom talks to sends one.
            for line in resp.text.splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if msg.get("id") == expect_id:
                    return msg
            raise MCPError(f"MCP server '{self.name}' sent no reply to {method} in its event stream")
        try:
            return resp.json()
        except ValueError as e:
            raise MCPError(
                f"MCP server '{self.name}' answered {method} with a non-JSON body: {resp.text[:200]}"
            ) from e

    def close(self):
        self._discard_http()
        proc, self._proc = self._proc, None
        if proc is None:
            return
        # Terminate BEFORE touching the pipes. A reader thread blocked on stdout holds that
        # stream's buffer lock, so closing it first blocks until the child happens to write —
        # which is never, for a server hung inside a tool call.
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except (OSError, ValueError):
                pass

    def _ensure_alive(self):
        if self.url:
            if self._http is None:
                raise MCPError(f"MCP server '{self.name}' is not connected")
            return
        if self._proc is None:
            raise MCPError(f"MCP server '{self.name}' is not running")
        if self._proc.poll() is not None:
            detail = self.stderr_tail()
            raise MCPError(
                f"MCP server '{self.name}' exited with code {self._proc.returncode}"
                + (f": {detail}" if detail else "")
            )

    def stderr_tail(self, lines: int = STDERR_TAIL_LINES) -> str:
        """Last stderr lines: the only explanation a crashed child leaves behind."""
        return "\n".join(self._stderr[-lines:])

    # -------------------------------------------------------------------- reader

    def _read_stdout(self):
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._queue.put(json.loads(line))
                except ValueError:
                    continue  # banner on stdout, cannot be a response
        except (ValueError, OSError):
            pass
        finally:
            # EOF. Every waiter must stop waiting, or a dead server hangs the agent.
            self._queue.put(None)

    def _read_stderr(self):
        try:
            for line in self._proc.stderr:
                self._stderr.append(line.rstrip())
                del self._stderr[:-STDERR_TAIL_LINES]
        except (ValueError, OSError):
            pass

    # ---------------------------------------------------------------------- rpc

    def _write(self, payload: dict):
        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            raise MCPError(f"MCP server '{self.name}' stdin closed: {e}") from e

    def _notify(self, method: str, params: Optional[dict] = None):
        if self.url:
            self._post_http({"jsonrpc": "2.0", "method": method, "params": params or {}}, None)
            return
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _take(self, deadline: float, method: str) -> dict:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MCPError(f"MCP server '{self.name}' timed out answering {method}")
        try:
            msg = self._queue.get(timeout=remaining)
        except queue.Empty:
            raise MCPError(
                f"MCP server '{self.name}' timed out after {self.timeout:g}s on {method}"
                + (f" (stderr: {self.stderr_tail(3)})" if self._stderr else "")
            )
        if msg is None:
            detail = self.stderr_tail(3)
            raise MCPError(
                f"MCP server '{self.name}' closed stdout during {method}"
                + (f": {detail}" if detail else "")
            )
        return msg

    def _handle_unsolicited(self, msg: dict):
        """A server->client request must be answered, or the server blocks on it."""
        if "id" not in msg or "method" not in msg:
            return  # notification
        self._write(
            {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {"code": -32601, "message": f"Method not found: {msg['method']}"},
            }
        )

    def _request_http(self, method: str, params: Optional[dict] = None) -> dict:
        req_id = self._next_id
        self._next_id += 1
        msg = self._post_http(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}, req_id
        ) or {}
        if "error" in msg:
            err = msg.get("error") or {}
            raise MCPError(
                f"MCP server '{self.name}' error on {method}: {err.get('message')}",
                int(err.get("code") or 0),
            )
        return msg.get("result") or {}

    def _request(self, method: str, params: Optional[dict] = None) -> dict:
        """Send a request and return its result. Calls are serialized: one waiter, one id."""
        with self._lock:
            if self.url:
                return self._request_http(method, params)
            self._ensure_alive()
            req_id = self._next_id
            self._next_id += 1
            self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
            deadline = time.monotonic() + self.timeout
            while True:
                msg = self._take(deadline, method)
                if msg.get("id") != req_id:
                    self._handle_unsolicited(msg)
                    continue
                if "error" in msg:
                    err = msg.get("error") or {}
                    raise MCPError(
                        f"MCP server '{self.name}' error on {method}: {err.get('message')}",
                        int(err.get("code") or 0),
                    )
                return msg.get("result") or {}

    # ------------------------------------------------------------------- methods

    def _list_tools(self) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        cursor = None
        while True:
            result = self._request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """Call one tool and return its text, or the JSON error envelope Boom tools use."""
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        text = content_text(result.get("content") or [])
        if result.get("isError"):
            return json.dumps({"error": text or f"tool '{name}' reported failure"})
        return text
