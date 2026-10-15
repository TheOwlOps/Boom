"""Agent Client Protocol (ACP) server for Boom, clean-room implementation.

Speaks a minimal JSON-RPC 2.0 dialect over stdin/stdout so any ACP-capable
IDE (Zed, VS Code extensions, ...) can drive the Boom agent:

    -> {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    <- {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, ...}}

    -> {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}}
    <- {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "..."}}

    -> {"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
        "params": {"sessionId": "...", "prompt": "hello"}}
    <- {"jsonrpc": "2.0", "id": 3, "result": {"text": "..."}}

The agent loop is *injected* as ``turn_fn`` -- an async callable
``(session_id: str, text: str) -> str`` -- never hardcoded. Without one,
``session/prompt`` answers with an honest JSON-RPC error instead of a
fabricated reply.

Stdlib only. One request can never kill the server: every line is handled
independently and failures become a JSON-RPC error for that request.
"""

import asyncio
import json
import sys
import time
import uuid

PROTOCOL_VERSION = 1


class AcpServer:
    """Minimal ACP server over line-delimited JSON-RPC 2.0."""

    def __init__(self, turn_fn=None, inp=None, out=None):
        """turn_fn: async (session_id, text) -> str, or None for honest errors."""
        self._turn_fn = turn_fn
        self._inp = inp if inp is not None else sys.stdin
        self._out = out if out is not None else sys.stdout
        self._sessions = {}  # session_id -> {"sessionId", "createdAt", "cancelled", "turns"}

    # ------------------------------------------------------------------ serve

    def serve(self):
        """Block serving stdin until EOF. Mirrors BoomCLI.serve_rpc framing."""
        asyncio.run(self._serve_async())

    async def _serve_async(self):
        loop = asyncio.get_running_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, self._inp.readline)
            except Exception as exc:
                self._log(f"stdin read failed: {type(exc).__name__}: {exc}")
                break
            if not line:
                break  # client closed the pipe
            line = line.strip()
            if not line:
                continue
            try:
                await self._handle_line(line)
            except Exception as exc:  # never let one line kill the server
                self._log(f"handler crashed: {type(exc).__name__}: {exc}")

    # --------------------------------------------------------------- protocol

    async def _handle_line(self, line):
        try:
            req = json.loads(line)
        except ValueError as exc:
            self._write({"jsonrpc": "2.0", "id": None,
                         "error": self._err(-32700, f"Parse error: {exc}")})
            return
        if not isinstance(req, dict):
            self._write({"jsonrpc": "2.0", "id": None,
                         "error": self._err(-32600, "Request must be a JSON object")})
            return

        rid = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        is_notification = rid is None
        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "initialized":
                return  # notification, no reply
            elif method == "session/new":
                result = self._session_new(params)
            elif method == "session/prompt":
                result = await self._session_prompt(params)
            elif method == "session/list":
                result = self._session_list(params)
            elif method == "session/cancel":
                result = self._session_cancel(params)
            else:
                raise _JsonRpcError(-32601, f"Method not found: {method}")
        except _JsonRpcError as exc:
            if not is_notification:
                self._write({"jsonrpc": "2.0", "id": rid,
                             "error": self._err(exc.code, exc.message)})
            return
        except Exception as exc:  # defensive: turn_fn raised something unexpected
            self._log(f"method {method} failed: {type(exc).__name__}: {exc}")
            if not is_notification:
                self._write({"jsonrpc": "2.0", "id": rid,
                             "error": self._err(-32603, f"Internal error: {exc}")})
            return

        if not is_notification:
            self._write({"jsonrpc": "2.0", "id": rid, "result": result})

    # ---------------------------------------------------------------- methods

    @staticmethod
    def _initialize(params):
        _ = params
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "promptCapabilities": {"text": True, "image": False, "audio": False},
                "sessionCapabilities": {"cancel": True},
            },
        }

    def _session_new(self, params):
        _ = params
        sid = uuid.uuid4().hex
        self._sessions[sid] = {
            "sessionId": sid,
            "createdAt": time.time(),
            "cancelled": False,
            "turns": 0,
        }
        return {"sessionId": sid}

    async def _session_prompt(self, params):
        sid = params.get("sessionId") or params.get("session_id")
        if not sid:
            raise _JsonRpcError(-32602, "session/prompt needs params.sessionId")
        session = self._sessions.get(sid)
        if session is None:
            raise _JsonRpcError(-32000, f"Unknown session: {sid}")
        text = _extract_text(params.get("prompt"))
        if not text:
            raise _JsonRpcError(-32602, "session/prompt needs a non-empty prompt")
        if self._turn_fn is None:
            raise _JsonRpcError(-32000, "No agent loop attached: cannot run prompt")
        session["cancelled"] = False  # a new prompt supersedes a stale cancel flag
        reply = await self._turn_fn(sid, text)
        session["turns"] += 1
        return {"text": reply if isinstance(reply, str) else str(reply)}

    def _session_list(self, params):
        _ = params
        return {"sessions": [
            {"sessionId": s["sessionId"], "createdAt": s["createdAt"],
             "turns": s["turns"], "cancelled": s["cancelled"]}
            for s in self._sessions.values()
        ]}

    def _session_cancel(self, params):
        sid = params.get("sessionId") or params.get("session_id")
        if not sid:
            raise _JsonRpcError(-32602, "session/cancel needs params.sessionId")
        session = self._sessions.get(sid)
        if session is None:
            raise _JsonRpcError(-32000, f"Unknown session: {sid}")
        # Best-effort: turns run synchronously one at a time, so a cancel
        # only marks the session; the next prompt starts fresh.
        session["cancelled"] = True
        return {"cancelled": True}

    # ------------------------------------------------------------------ utils

    @staticmethod
    def _err(code, message):
        return {"code": code, "message": str(message)}

    def _write(self, obj):
        try:
            self._out.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self._out.flush()
        except Exception as exc:
            self._log(f"stdout write failed: {type(exc).__name__}: {exc}")

    @staticmethod
    def _log(msg):
        # Protocol lines own stdout; diagnostics go to stderr.
        print(f"[acp] {msg}", file=sys.stderr, flush=True)


class _JsonRpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _extract_text(prompt):
    """Accept a plain string or ACP-style content blocks; return text or ''."""
    if isinstance(prompt, str):
        return prompt.strip()
    if isinstance(prompt, list):
        parts = []
        for block in prompt:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str) and t.strip():
                    parts.append(t.strip())
        return "\n".join(parts)
    return ""
