"""HTTP API for the Boom web UI (v1).

Implements ``~/workspace/boom-mods/webui/API_CONTRACT.md``. Stdlib only:
``http.server.ThreadingHTTPServer`` + ``json`` + ``threading`` + ``urllib``
(plus ``os``/``sys``/``time``/``asyncio`` — all stdlib).

Seams (constructor-injected, so tests never touch the network or an LLM):
  brain  - object with ``await run(prompt, strategy, session_id=None, **kw)``
           returning a BrainResult-like (attrs: ``strategy``, ``text``,
           ``node_outputs``, ``agent_outputs``, ``errors``)
  loop   - ReActLoop-like with ``load_session(session_id)``
  daemon - GatewayDaemon-like with ``can_deliver(channel)``
  model  - LLM model name reported by ``GET /api/health``

Production wiring is lazy (see ``build_real_api`` / ``__main__``) so importing
this module never imports agent code.

v1 has no auth: the server binds to 127.0.0.1 only (localhost-only).
"""

import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

VERSION = "0.1.0"
DEFAULT_PORT = 18080
MAX_BODY_BYTES = 1024 * 1024  # 1 MB Content-Length cap
ALLOWED_ORIGINS = ("http://localhost:5173", "http://localhost:1420")  # Vite, Tauri
CHAT_STRATEGIES = ("auto", "react", "dag", "mass")

# GET /api/channels/status: fixed channel list (contract) + the credential
# each adapter actually checks, so `detail` stays honest when disconnected.
KNOWN_CHANNELS = (
    "telegram", "discord", "slack", "whatsapp",
    "line", "feishu", "zalo", "messenger",
)
CHANNEL_CREDENTIAL_HINT = {
    "telegram": "TELEGRAM_BOT_TOKEN not set",
    "discord": "discord token not given to the gateway daemon",
    "slack": "BOOM_SLACK_BOT_TOKEN not set",
    "whatsapp": "BOOM_WHATSAPP_TOKEN not set",
    "line": "BOOM_LINE_CHANNEL_TOKEN not set",
    "feishu": "BOOM_FEISHU_APP_ID / BOOM_FEISHU_APP_SECRET not set",
    "zalo": "BOOM_ZALO_OA_ACCESS_TOKEN not set",
    "messenger": "BOOM_MESSENGER_PAGE_TOKEN not set",
}


class BoomAPI:
    """Holds the injected seams and the chat-turn serialization lock."""

    def __init__(self, brain=None, loop=None, daemon=None, model=None):
        self.brain = brain
        self.loop = loop
        self.daemon = daemon
        self.model = model
        # ReAct is single-turn-at-a-time by design: turns are serialized;
        # a second concurrent turn gets 429 "agent busy".
        self.turn_lock = threading.Lock()

    # ---------------------------------------------------------------- logic

    async def run_turn(self, prompt, strategy="auto", session_id=None):
        """Run one chat turn. Returns (reply, used_strategy, errors)."""
        result = await self.brain.run(
            prompt, strategy=strategy, session_id=session_id
        )
        return (
            str(getattr(result, "text", "") or ""),
            str(getattr(result, "strategy", None) or strategy or "react"),
            dict(getattr(result, "errors", None) or {}),
        )

    def channel_status(self):
        """Contract: GET /api/channels/status."""
        channels = []
        for name in KNOWN_CHANNELS:
            connected = False
            if self.daemon is not None:
                try:
                    connected = bool(self.daemon.can_deliver(name))
                except Exception:
                    connected = False
            if connected:
                detail = None
            elif self.daemon is None:
                detail = "gateway daemon not wired"
            else:
                detail = CHANNEL_CREDENTIAL_HINT.get(name, "not configured")
            channels.append(
                {"name": name, "connected": connected, "detail": detail}
            )
        return {"channels": channels}

    def dag_status(self):
        """Contract: GET /api/dag/status.

        Today's Brain records no last-run state, so this honestly returns [].
        The lookup is forward-compatible: if a future Brain exposes
        ``last_result`` (nodes + per-node statuses), it is surfaced in the
        contract shape instead of being invented.
        """
        nodes = []
        last = getattr(self.brain, "last_result", None) if self.brain else None
        if last is not None:
            statuses = getattr(last, "node_statuses", None) or {}
            defs = getattr(last, "nodes", None) or []
            outputs = getattr(last, "node_outputs", None) or {}
            seen = set()
            for node in defs:
                if isinstance(node, dict):
                    nid = node.get("id")
                    name = node.get("name") or nid
                    deps = list(node.get("dependencies") or [])
                else:
                    nid = getattr(node, "id", None)
                    name = getattr(node, "name", None) or nid
                    deps = list(getattr(node, "dependencies", None) or [])
                if nid is None:
                    continue
                seen.add(nid)
                nodes.append(
                    {
                        "id": str(nid),
                        "name": str(name),
                        "status": statuses.get(
                            nid, "completed" if nid in outputs else "idle"
                        ),
                        "dependencies": [str(d) for d in deps],
                    }
                )
            for nid in outputs:
                if nid not in seen:
                    nodes.append(
                        {
                            "id": str(nid),
                            "name": str(nid),
                            "status": statuses.get(nid, "completed"),
                            "dependencies": [],
                        }
                    )
        return {"nodes": nodes}


class _Handler(BaseHTTPRequestHandler):
    server_version = "BoomAPI/" + VERSION

    # ------------------------------------------------------------- plumbing

    @property
    def api(self):
        return self.server.api

    def log_message(self, fmt, *args):  # keep the console readable
        sys.stderr.write("[api] %s - %s\n" % (self.address_string(), fmt % args))

    def send_error(self, code, message=None, explain=None):
        # Keep every error in the contract's JSON shape; never HTML.
        self._send_json(code, {"error": message or "request failed"})

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        self._send_json(status, {"error": message})

    def _cors_headers(self):
        # Reflected only for the two dev origins, never "*".
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _read_json_body(self):
        """Returns (data, error) where error is (status, payload) or None."""
        raw_len = self.headers.get("Content-Length")
        try:
            n = int(raw_len) if raw_len is not None else 0
        except (TypeError, ValueError):
            return None, (400, {"error": "invalid Content-Length"})
        if n > MAX_BODY_BYTES:
            return None, (400, {"error": "request body too large"})
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw.strip():
            return {}, None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, (400, {"error": "request body is not valid JSON"})
        if not isinstance(data, dict):
            return None, (400, {"error": "request body must be a JSON object"})
        return data, None

    # --------------------------------------------------------------- routes

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "version": VERSION,
                    "model": self.api.model or "unconfigured",
                },
            )
        elif path == "/api/sessions":
            # ReActLoop's SQLite session store exposes load_session but no
            # listing API — honest empty state, never invented sessions.
            self._send_json(200, {"sessions": []})
        elif path == "/api/channels/status":
            self._send_json(200, self.api.channel_status())
        elif path == "/api/dag/status":
            self._send_json(200, self.api.dag_status())
        else:
            self._error(404, "not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/chat":
            self._handle_chat()
        else:
            self._error(404, "not found")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    # ----------------------------------------------------------------- chat

    def _handle_chat(self):
        data, err = self._read_json_body()
        if err:
            status, payload = err
            return self._send_json(status, payload)

        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return self._error(400, "prompt is required")
        strategy = data.get("strategy", "auto")
        if strategy not in CHAT_STRATEGIES:
            return self._error(
                400, "strategy must be one of: " + ", ".join(CHAT_STRATEGIES)
            )
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            session_id = "session_%d" % int(time.time())

        if self.api.loop is not None:
            try:
                self.api.loop.load_session(session_id)
            except Exception:
                return self._error(500, "failed to load session")
        if self.api.brain is None:
            return self._error(503, "agent brain not configured")

        if not self.api.turn_lock.acquire(blocking=False):
            return self._error(429, "agent busy")
        try:
            if "text/event-stream" in (self.headers.get("Accept") or ""):
                self._chat_sse(prompt, strategy, session_id)
            else:
                try:
                    reply, used_strategy, errors = asyncio.run(
                        self.api.run_turn(prompt, strategy, session_id)
                    )
                except Exception:
                    return self._error(500, "chat turn failed")
                self._send_json(
                    200,
                    {
                        "reply": reply,
                        "session_id": session_id,
                        "strategy": used_strategy,
                        "errors": errors,
                    },
                )
        finally:
            self.api.turn_lock.release()

    def _chat_sse(self, prompt, strategy, session_id):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors_headers()
        self.end_headers()

        def emit(event, payload):
            line = "event: %s\ndata: %s\n\n" % (
                event,
                json.dumps(payload, ensure_ascii=False),
            )
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()

        emit("status", {"phase": "thinking"})  # sent immediately
        try:
            reply, used_strategy, _errors = asyncio.run(
                self.api.run_turn(prompt, strategy, session_id)
            )
        except Exception:
            emit("error", {"error": "chat turn failed"})
            return
        # v1 honesty: the ReAct loop yields no live tokens, so `message`
        # carries the full reply once; the web client animates it client-side.
        emit("message", {"text": reply})
        emit("done", {"session_id": session_id, "strategy": used_strategy})


# ------------------------------------------------------------ real backend


def build_real_api():
    """Instantiate ONE BoomCLI for the server lifetime.

    On any failure (missing deps, no provider key) returns a degraded BoomAPI
    whose chat path answers 503 honestly — health and channel-status still
    work.
    """
    try:
        from agent.src.cli import BoomCLI
        from agent.src.config import BoomConfig

        cli = BoomCLI(BoomConfig.load())
    except Exception as e:
        print(
            "[api] backend unavailable (%s: %s) — chat returns 503"
            % (type(e).__name__, e),
            flush=True,
        )
        return BoomAPI()
    model = getattr(getattr(cli, "llm", None), "model", None)
    return BoomAPI(
        brain=cli.brain,
        loop=cli.loop,
        daemon=getattr(cli, "daemon", None),
        model=model,
    )


def main():
    port = int(os.environ.get("BOOM_API_PORT", str(DEFAULT_PORT)))
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.api = build_real_api()
    print(
        "[api] Boom web API listening on http://127.0.0.1:%d" % server.server_port,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
