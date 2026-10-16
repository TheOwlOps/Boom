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
from pathlib import Path


def _sanitize_proxy_env():
    """Remove malformed no_proxy entries that crash httpx.

    httpx 0.28.x fails with InvalidURL when no_proxy contains bracketed
    IPv6 literals (e.g. "[::1]") because it builds keys like
    "all://*[::1]". Strip bracketed entries; bare IPv6 without brackets
    is left alone (httpx handles those).
    """
    for key in ("no_proxy", "NO_PROXY"):
        val = os.environ.get(key)
        if not val:
            continue
        parts = [p.strip() for p in val.split(",")]
        clean = [p for p in parts if not (p.startswith("[") and p.endswith("]"))]
        if len(clean) != len(parts):
            os.environ[key] = ",".join(clean)


_sanitize_proxy_env()


def _repo_root():
    """Repo root for ``__file__``-relative resources (skills/, data/, ...).

    In a PyInstaller one-file bundle ``__file__`` points inside the
    extraction temp dir, so ``parent.parent`` would resolve to the temp
    dir's parent (e.g. /tmp) instead of the bundled tree. Use
    ``sys._MEIPASS`` when frozen so bundled resources are found.
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
    return Path(__file__).resolve().parent.parent


# Ensure agent/src is on path for `tools.*` imports (e.g. tools.ws_client).
# This makes `python -m gateway.api` work without manual PYTHONPATH.
_root = _repo_root()
for _p in (str(_root), str(_root / "agent" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

VERSION = "0.1.0"

import time
_API_START_TIME = time.time()

# In-memory log buffer for /api/logs (also useful for debugging)
_LOG_BUFFER: list = []
_LOG_MAX = 200


def api_log(msg: str):
    """Append a log entry to the buffer (keeps the newest 200 lines at most)."""
    import datetime
    _LOG_BUFFER.append({
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "msg": str(msg)[:500],
    })
    if len(_LOG_BUFFER) > _LOG_MAX:
        del _LOG_BUFFER[:-_LOG_MAX]


_ACTIVITY_FRESH_SEC = 30  # newest event within this window counts as "current"


def _activity_root():
    return _repo_root()


def _parse_ts_iso(ts):
    """Parse an ISO-8601 timestamp to epoch seconds; None on failure."""
    import datetime
    try:
        return datetime.datetime.fromisoformat(str(ts)).timestamp()
    except Exception:
        return None


def _build_activity_payload():
    """Build the /api/activity payload from real data only. Never invents events."""
    import datetime
    import glob
    import json
    import sqlite3
    import time

    activities = []

    # source 1: trajectory exports
    try:
        files = sorted(
            glob.glob(str(_activity_root() / "trajectories" / "trajectory_*.json")),
            reverse=True,
        )
        if files:
            # Use a context manager so the file handle is always closed,
            # even if the JSON is malformed.
            with open(files[0], encoding="utf-8") as fh:
                data = json.loads(fh.read())
            steps = data.get("steps") or []
            for s in steps[-20:]:
                tool = s.get("action_tool")
                thought = (s.get("thought") or "").strip()
                if tool:
                    act_type = "tool_call"
                    msg = f"called tool: {tool}"
                else:
                    act_type = "thinking"
                    msg = thought[:120] or "thinking"
                ts_epoch = float(s.get("timestamp") or 0)
                activities.append({
                    "id": f"traj-{data.get('session_id', '?')}-{s.get('step_id', 0)}",
                    "timestamp": datetime.datetime.fromtimestamp(ts_epoch).isoformat()
                    if ts_epoch else datetime.datetime.now().isoformat(timespec="seconds"),
                    "type": act_type,
                    "message": msg,
                    "detail": json.dumps(
                        {"thought": thought[:500], "args": s.get("action_args"),
                         "observation": (s.get("observation") or "")[:500]},
                        ensure_ascii=False,
                    ),
                })
    except Exception:
        pass

    # source 2: audit log tail (read-only)
    if not activities:
        db = _activity_root() / "memories" / "audit.db"
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT id, ts, actor, action, target, channel, details_json, result"
                " FROM audit_events ORDER BY ts DESC, id DESC LIMIT 20"
            )
            rows = cur.fetchall()
            conn.close()
            for r in reversed(rows):
                action = (r["action"] or "").lower()
                result = (r["result"] or "ok").lower()
                if result == "error":
                    act_type = "error"
                elif "tool" in action:
                    act_type = "tool_call"
                else:
                    act_type = "done"
                activities.append({
                    "id": str(r["id"]),
                    "timestamp": str(r["ts"]),
                    "type": act_type,
                    "message": f"{r['action']} ({r['actor']})",
                    "detail": r["details_json"],
                })
        except Exception:
            pass

    # source 3: in-memory gateway log buffer keyword match
    if not activities:
        try:
            keywords = ("tool_call", "thinking", "tool", "error", "fail")
            matches = [
                e for e in _LOG_BUFFER
                if any(k in str(e.get("msg", "")).lower() for k in keywords)
            ]
            for e in matches[-20:]:
                msg = str(e.get("msg", ""))
                low = msg.lower()
                if "error" in low or "fail" in low:
                    act_type = "error"
                elif "tool" in low:
                    act_type = "tool_call"
                else:
                    act_type = "thinking"
                activities.append({
                    "id": f"log-{e.get('ts', '')}",
                    "timestamp": str(e.get("ts", "")),
                    "type": act_type,
                    "message": msg[:160],
                    "detail": msg,
                })
        except Exception:
            pass

    current = {"type": "idle", "message": "idle"}
    now = time.time()
    if activities:
        newest = activities[-1]
        started = _parse_ts_iso(newest["timestamp"])
        if started and (now - started) <= _ACTIVITY_FRESH_SEC:
            current = {
                "type": newest["type"],
                "message": newest["message"],
                "started_at": newest["timestamp"],
            }
    return {"activities": activities, "current": current}


_GROUPS_DB = _repo_root() / "data" / "groups.db"


def _groups_manager():
    """Builds a business.groups.GroupManager. Import is lazy on purpose:
    importing this module must never import agent code (see module docstring).
    """
    from agent.src.business.groups import GroupManager
    return GroupManager(db_path=_GROUPS_DB)


_BOTS_DB = _repo_root() / "data" / "bots.db"


def _bots_manager():
    """Builds the custom-bot BotManager. Import is lazy on purpose (same
    reason as _groups_manager: importing this module must never import bot
    code at load time)."""
    from agent.src.business.bots import BotManager
    return BotManager(db_path=_BOTS_DB)


def _providers_manager():
    """Builds the provider ProviderManager. Import is lazy on purpose (same
    reason as _bots_manager: importing this module must never import agent
    code at load time)."""
    from providers.manager import ProviderManager
    return ProviderManager()


def _marketplace_backend():
    """Builds the marketplace MarketplaceBackend. Import is lazy on purpose
    (same reason as _bots_manager: importing this module must never import
    agent code at load time)."""
    from agent.src.marketplace import MarketplaceBackend
    return MarketplaceBackend()


# --- Ollama local-LLM status --------------------------------------------------
# Ollama runs its own HTTP daemon on localhost:11434. Querying it with the
# stdlib keeps this check free of extra dependencies. The timeout is short
# on purpose: a hung localhost call should never stall the gateway.

_OLLAMA_BASE_URL = "http://localhost:11434"
_OLLAMA_TIMEOUT_S = 3.0


def _check_ollama_status():
    """Probe the local Ollama daemon and list installed models.

    Returns ``{"running": True, "models": ["llama3.1", ...]}`` when Ollama
    answers ``GET /api/tags``, otherwise ``{"running": False, "models": []}``.

    Never raises: connection errors, timeouts, non-2xx responses and
    malformed JSON all degrade to ``running: False``.
    """
    import urllib.request

    try:
        req = urllib.request.Request(_OLLAMA_BASE_URL + "/api/tags")
        with urllib.request.urlopen(req, timeout=_OLLAMA_TIMEOUT_S) as resp:
            if resp.status != 200:
                return {"running": False, "models": []}
            data = json.loads(resp.read().decode("utf-8"))
        models = []
        tags = data.get("models", [])
        if isinstance(tags, list):
            for entry in tags:
                if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    # "llama3.1:latest" -> display just "llama3.1"; keep the
                    # tag when it is not "latest" (e.g. "codellama:7b").
                    name, _, tag = entry["name"].partition(":")
                    models.append(name if tag in ("", "latest") else f"{name}:{tag}")
    except Exception:
        # URLError, TimeoutError, socket errors, JSONDecodeError, ...
        return {"running": False, "models": []}
    return {"running": True, "models": models}


def _zalo_personal_adapter():
    """Builds a ZaloPersonalAdapter. Import is lazy on purpose (same reason
    as _groups_manager): importing this module must never break when the
    optional ``zlapi`` dependency is missing — that failure surfaces only
    when login is attempted, as a clean RuntimeError the handler reports."""
    from gateway.channels.zalo_personal import ZaloPersonalAdapter
    return ZaloPersonalAdapter()


DEFAULT_PORT = 18080
MAX_BODY_BYTES = 1024 * 1024  # 1 MB Content-Length cap
ALLOWED_ORIGINS = ("http://localhost:5173", "http://localhost:1420", "http://127.0.0.1:5173")  # Vite, Tauri
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

    async def run_turn(self, prompt, strategy="auto", session_id=None,
                       bot_system_prompt=None, bot_tools=None):
        """Run one chat turn. Returns (reply, used_strategy, errors)."""
        result = await self.brain.run(
            prompt, strategy=strategy, session_id=session_id,
            system_prompt=bot_system_prompt, tools=bot_tools,
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
        # Allow any local origin (localhost, 127.0.0.1) across any dev port
        origin = self.headers.get("Origin")
        if origin and (
            origin.startswith("http://localhost:")
            or origin.startswith("http://127.0.0.1:")
            or origin in ALLOWED_ORIGINS
        ):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, PUT, DELETE")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, Authorization")
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
            # Count real tools/skills from the environment
            tools_count = 0
            skills_count = 0
            try:
                from agent.src.environment import AgentEnvironment
                env = AgentEnvironment()
                tools_count = len(env.get_tools() or {})
            except Exception:
                pass
            try:
                skills_dir = _repo_root() / "skills"
                if skills_dir.exists():
                    skills_count = sum(1 for p in skills_dir.iterdir() if p.is_dir())
            except Exception:
                pass
            self._send_json(
                200,
                {
                    "status": "ok",
                    "version": VERSION,
                    "model": self.api.model or "unconfigured",
                    "uptime_sec": int(time.time() - _API_START_TIME),
                    "tools_count": tools_count,
                    "skills_count": skills_count,
                },
            )
        elif path == "/api/version":
            try:
                from agent.src.version import get_current_version

                version = get_current_version()
            except Exception:
                version = "0.1.0"
            self._send_json(200, {"version": version})
        elif path == "/api/updates/check":
            try:
                from agent.src.version import check_github_update, UpdateCheckError

                self._send_json(200, check_github_update())
            except UpdateCheckError as e:
                self._send_json(502, {"error": str(e)[:500]})
            except Exception as e:
                self._send_json(502, {"error": f"update check failed: {e}"[:500]})
        elif path == "/api/logs":
            from urllib.parse import parse_qs

            qs = parse_qs(urlparse(self.path).query)

            def _as_int(values, default):
                try:
                    return int(values[0])
                except (ValueError, TypeError, IndexError):
                    return default

            limit = _as_int(qs.get("limit"), 50)
            offset = _as_int(qs.get("offset"), 0)
            limit = max(1, min(limit, _LOG_MAX))
            offset = max(0, offset)

            total = len(_LOG_BUFFER)
            end = max(0, total - offset)
            start = max(0, end - limit)
            self._send_json(
                200,
                {
                    "logs": _LOG_BUFFER[start:end],
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                },
            )
        elif path == "/api/activity":
            try:
                self._send_json(200, _build_activity_payload())
            except Exception as e:
                self._send_json(500, {"error": f"/api/activity failed: {e}"})
        elif path == "/api/sessions":
            # ReActLoop's SQLite session store exposes load_session but no
            # listing API — honest empty state, never invented sessions.
            self._send_json(200, {"sessions": []})
        elif path == "/api/channels/status":
            self._send_json(200, self.api.channel_status())
        elif path == "/api/channels/zalo-personal/status":
            self._handle_zalo_personal_status()
        elif path == "/api/dag/status":
            self._send_json(200, self.api.dag_status())
        elif path == "/api/tools":
            # Real tool list from the environment
            tools = []
            try:
                from agent.src.environment import AgentEnvironment
                env = AgentEnvironment()
                for name, t in (env.get_tools() or {}).items():
                    tools.append({
                        "name": name,
                        "description": getattr(t, "description", "") or "",
                        "group": getattr(t, "group", "other") or "other",
                    })
            except Exception as e:
                api_log(f"/api/tools error: {e}")
            self._send_json(200, {"tools": tools, "count": len(tools)})
        elif path == "/api/skills":
            # Scan the repo's real skills/ directory
            skills = []
            try:
                skills_dir = _repo_root() / "skills"
                if skills_dir.exists():
                    for p in sorted(skills_dir.iterdir()):
                        if not p.is_dir():
                            continue
                        desc = ""
                        md = p / "SKILL.md"
                        if md.exists():
                            try:
                                lines = md.read_text(encoding="utf-8").split("\n")
                                for ln in lines[1:10]:
                                    ln = ln.strip()
                                    if ln and not ln.startswith("#"):
                                        desc = ln[:200]
                                        break
                            except Exception:
                                pass
                        skills.append({"name": p.name, "description": desc})
            except Exception as e:
                api_log(f"/api/skills error: {e}")
            self._send_json(200, {"skills": skills, "count": len(skills)})
        elif path == "/api/groups":
            self._handle_groups_list()
        elif path == "/api/bots":
            self._handle_bots_list()
        elif path == "/api/providers":
            self._handle_providers_list()
        elif path == "/api/providers/ollama/status":
            self._handle_ollama_status()
        elif path == "/api/marketplace":
            self._handle_marketplace_catalog()
        elif path == "/api/marketplace/installed":
            self._handle_marketplace_installed()
        else:
            self._error(404, "not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/chat":
            self._handle_chat()
        elif path == "/api/tools/run":
            self._handle_tool_run()
        elif path == "/api/groups/mute":
            self._handle_groups_mute()
        elif path == "/api/groups/policy":
            self._handle_groups_policy()
        elif path == "/api/bots":
            self._handle_bots_create()
        elif path == "/api/bots/active":
            self._handle_bots_set_active()
        elif path == "/api/providers/config":
            self._handle_providers_config()
        elif path == "/api/providers/test":
            self._handle_providers_test()
        elif path == "/api/marketplace/install":
            self._handle_marketplace_install()
        elif path == "/api/marketplace/uninstall":
            self._handle_marketplace_uninstall()
        elif path == "/api/channels/zalo-personal/login":
            self._handle_zalo_personal_login()
        elif path == "/api/channels/zalo-personal/logout":
            self._handle_zalo_personal_logout()
        else:
            self._error(404, "not found")

    def do_PUT(self):
        path = urlparse(self.path).path
        if path.startswith("/api/bots/"):
            from urllib.parse import unquote
            bot_id = unquote(path[len("/api/bots/"):])
            # "/api/bots/active" is a POST-only action, not a bot id.
            if not bot_id or bot_id == "active":
                return self._error(404, "not found")
            self._handle_bots_update(bot_id)
        else:
            self._error(404, "not found")

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/bots/"):
            from urllib.parse import unquote
            bot_id = unquote(path[len("/api/bots/"):])
            if not bot_id or bot_id == "active":
                return self._error(404, "not found")
            self._handle_bots_delete(bot_id)
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

    def _handle_tool_run(self):
        """Try running one tool via the API (uses SandboxedToolRunner if available)."""
        data, err = self._read_json_body()
        if err:
            status, payload = err
            return self._send_json(status, payload)
        tool_name = data.get("tool")
        args = data.get("args", {})
        if not isinstance(tool_name, str) or not tool_name:
            return self._error(400, "tool is required")
        if not isinstance(args, dict):
            return self._error(400, "args must be an object")
        try:
            from agent.src.environment import AgentEnvironment
            env = AgentEnvironment()
            tools = env.get_tools() or {}
            if tool_name not in tools:
                return self._error(404, f"tool not found: {tool_name}")
            tool = tools[tool_name]
            func = getattr(tool, "func", None) or tool
            # Run through the sandbox when available
            try:
                from agent.src.security.toolsandbox import SandboxedToolRunner
                runner = SandboxedToolRunner()
                result = runner.run(tool_name, func, args, user="api")
            except ImportError:
                result = func(**args) if callable(func) else {"error": "not callable"}
            api_log(f"tool_run: {tool_name} ok")
            return self._send_json(200, {"ok": True, "result": str(result)[:2000]})
        except Exception as e:
            api_log(f"tool_run: {tool_name} error: {e}")
            return self._send_json(200, {"ok": False, "error": str(e)[:500]})

    def _handle_groups_list(self):
        """GET /api/groups — list all registered groups with stats + policy."""
        try:
            mgr = _groups_manager()
            groups = mgr.list_groups()
        except Exception as e:
            api_log(f"/api/groups error: {e}")
            return self._send_json(200, {"groups": [], "count": 0})
        self._send_json(200, {"groups": groups, "count": len(groups)})

    def _handle_groups_mute(self):
        """POST /api/groups/mute — {channel, group_id, muted: bool}."""
        data, err = self._read_json_body()
        if err:
            status, payload = err
            return self._send_json(status, payload)
        channel = data.get("channel")
        group_id = data.get("group_id")
        muted = data.get("muted")
        if not isinstance(channel, str) or not channel.strip():
            return self._error(400, "channel is required")
        if not isinstance(group_id, str) or not group_id.strip():
            return self._error(400, "group_id is required")
        if not isinstance(muted, bool):
            return self._error(400, "muted must be a boolean")
        channel, group_id = channel.strip(), group_id.strip()
        try:
            mgr = _groups_manager()
            group = mgr.get_group_stats(channel, group_id)
            if group is None:
                return self._error(404, f"group not registered: {channel}/{group_id}")
            settings = group.get("settings")
            if not isinstance(settings, dict):
                settings = {}
            settings["muted"] = muted
            with mgr._lock, mgr._connect() as conn:
                conn.execute(
                    "UPDATE groups SET settings_json = ? WHERE channel = ? AND group_id = ?",
                    (json.dumps(settings), channel, group_id),
                )
        except Exception as e:
            api_log(f"/api/groups/mute error: {e}")
            return self._error(500, "failed to update mute state")
        self._send_json(200, {"ok": True, "channel": channel,
                              "group_id": group_id, "muted": muted})

    def _handle_groups_policy(self):
        """POST /api/groups/policy — {channel, group_id, policy: {...}}."""
        data, err = self._read_json_body()
        if err:
            status, payload = err
            return self._send_json(status, payload)
        channel = data.get("channel")
        group_id = data.get("group_id")
        policy = data.get("policy")
        if not isinstance(channel, str) or not channel.strip():
            return self._error(400, "channel is required")
        if not isinstance(group_id, str) or not group_id.strip():
            return self._error(400, "group_id is required")
        if not isinstance(policy, dict):
            return self._error(400, "policy must be an object")
        try:
            mgr = _groups_manager()
            result = mgr.set_group_policy(channel.strip(), group_id.strip(), policy)
        except Exception as e:
            api_log(f"/api/groups/policy error: {e}")
            return self._error(500, "failed to save policy")
        if isinstance(result, dict) and result.get("error"):
            msg = str(result["error"])
            status = 404 if "not registered" in msg else 400
            return self._error(status, msg)
        self._send_json(200, {"ok": True, "policy": result.get("policy", policy)})

    # ------------------------------------------------------------------
    # Custom bots (Grok-style): user-created bots with their own
    # personality (system prompt) and tool permissions.
    # ------------------------------------------------------------------
    def _handle_bots_list(self):
        """GET /api/bots — list all custom bots (active bot first)."""
        try:
            mgr = _bots_manager()
            bots = mgr.list_bots()
        except Exception as e:
            api_log(f"/api/bots error: {e}")
            return self._send_json(200, {"bots": [], "count": 0})
        self._send_json(200, {"bots": bots, "count": len(bots)})

    def _handle_bots_create(self):
        """POST /api/bots — create a bot."""
        data, err = self._read_json_body()
        if err:
            status, payload = err
            return self._send_json(status, payload)
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            return self._error(400, "name is required")
        try:
            mgr = _bots_manager()
            result = mgr.create_bot(
                name=name.strip(),
                description=data.get("description", "") or "",
                system_prompt=data.get("system_prompt", "") or "",
                avatar_emoji=data.get("avatar_emoji", "🤖") or "🤖",
                allowed_tools=data.get("allowed_tools"),
                denied_tools=data.get("denied_tools"),
                channel_bindings=data.get("channel_bindings"),
            )
        except Exception as e:
            api_log(f"/api/bots create error: {e}")
            return self._error(500, "failed to create bot")
        if isinstance(result, dict) and result.get("error"):
            return self._error(400, str(result["error"]))
        self._send_json(201, {"ok": True, "bot": result["bot"]})

    def _handle_bots_update(self, bot_id):
        """PUT /api/bots/<id> — partial update."""
        data, err = self._read_json_body()
        if err:
            status, payload = err
            return self._send_json(status, payload)
        try:
            mgr = _bots_manager()
            result = mgr.update_bot(bot_id, **data)
        except Exception as e:
            api_log(f"/api/bots update error: {e}")
            return self._error(500, "failed to update bot")
        if isinstance(result, dict) and result.get("error"):
            msg = str(result["error"])
            status = 404 if "not found" in msg else 400
            return self._error(status, msg)
        self._send_json(200, {"ok": True, "bot": result["bot"]})

    def _handle_bots_delete(self, bot_id):
        """DELETE /api/bots/<id> — delete a bot."""
        try:
            mgr = _bots_manager()
            deleted = mgr.delete_bot(bot_id)
        except Exception as e:
            api_log(f"/api/bots delete error: {e}")
            return self._error(500, "failed to delete bot")
        if not deleted:
            return self._error(404, f"bot not found: {bot_id}")
        self._send_json(200, {"ok": True})

    def _handle_bots_set_active(self):
        """POST /api/bots/active — body {"bot_id": "..."} marks that bot active."""
        data, err = self._read_json_body()
        if err:
            status, payload = err
            return self._send_json(status, payload)
        bot_id = data.get("bot_id")
        if not isinstance(bot_id, str) or not bot_id.strip():
            return self._error(400, "bot_id is required")
        try:
            mgr = _bots_manager()
            ok = mgr.set_active_bot(bot_id.strip())
            bot = mgr.get_active_bot() if ok else None
        except Exception as e:
            api_log(f"/api/bots/active error: {e}")
            return self._error(500, "failed to set active bot")
        if not ok:
            return self._error(404, f"bot not found: {bot_id}")
        self._send_json(200, {"ok": True, "bot": bot})

    # ------------------------------------------------------- providers

    def _handle_providers_list(self):
        """GET /api/providers — provider metadata + active id + has_key flags.

        Never returns API keys.
        """
        try:
            mgr = _providers_manager()
            providers = []
            for meta in mgr.list_providers():
                meta = dict(meta)
                meta["has_key"] = mgr.has_key(meta["id"])
                providers.append(meta)
            active = mgr.get_active_provider()
        except Exception as e:
            api_log(f"/api/providers error: {e}")
            return self._error(500, "failed to list providers")
        self._send_json(200, {"providers": providers, "active": active})

    def _handle_providers_config(self):
        """POST /api/providers/config — body {provider_id, api_key, model, base_url?}."""
        data, err = self._read_json_body()
        if err:
            status, payload = err
            return self._send_json(status, payload)
        provider_id = data.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id.strip():
            return self._error(400, "provider_id is required")
        api_key = data.get("api_key")
        model = data.get("model")
        base_url = data.get("base_url")
        for field, value in (("api_key", api_key), ("model", model)):
            if value is not None and not isinstance(value, str):
                return self._error(400, f"{field} must be a string")
        if base_url is not None and not isinstance(base_url, str):
            return self._error(400, "base_url must be a string")
        try:
            from providers.manager import UnknownProviderError
            mgr = _providers_manager()
            mgr.save_config(
                provider_id.strip(),
                api_key or "",
                model or "",
                base_url,
            )
        except UnknownProviderError:
            return self._error(400, f"unknown provider: {provider_id}")
        except ValueError as e:
            return self._error(400, str(e))
        except Exception as e:
            api_log(f"/api/providers/config error: {e}")
            return self._error(500, "failed to save provider config")
        self._send_json(200, {"ok": True, "provider_id": provider_id.strip()})

    def _handle_providers_test(self):
        """POST /api/providers/test — body {provider_id} -> {ok, message}."""
        data, err = self._read_json_body()
        if err:
            status, payload = err
            return self._send_json(status, payload)
        provider_id = data.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id.strip():
            return self._error(400, "provider_id is required")
        try:
            mgr = _providers_manager()
            ok, message = mgr.test_connection(provider_id.strip())
        except Exception as e:
            api_log(f"/api/providers/test error: {e}")
            return self._error(500, "failed to test provider connection")
        self._send_json(200, {"ok": ok, "message": message})

    def _handle_ollama_status(self):
        """GET /api/providers/ollama/status -> {running, models}.

        ``_check_ollama_status`` never raises, so this handler always
        answers 200 — even when Ollama is not installed/running.
        """
        self._send_json(200, _check_ollama_status())

    def _handle_marketplace_catalog(self):
        """GET /api/marketplace — installable-items catalog."""
        try:
            backend = _marketplace_backend()
            items = backend.get_catalog()
        except Exception as e:
            api_log(f"/api/marketplace error: {e}")
            return self._error(500, "failed to build marketplace catalog")
        self._send_json(200, {"items": items, "count": len(items)})

    def _handle_marketplace_installed(self):
        """GET /api/marketplace/installed — installed plugin/tool ids."""
        try:
            backend = _marketplace_backend()
            installed = backend.get_installed()
        except Exception as e:
            api_log(f"/api/marketplace/installed error: {e}")
            return self._error(500, "failed to list installed items")
        self._send_json(200, {"installed": installed})

    def _handle_marketplace_install(self):
        """POST /api/marketplace/install — body: {"id": "<kind>:<name>" / bare name}."""
        data, err = self._read_json_body()
        if err:
            status, payload = err
            return self._send_json(status, payload)
        item_id = data.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            return self._error(400, "id is required")
        try:
            from agent.src.marketplace import MarketplaceError
            backend = _marketplace_backend()
            result = backend.install_item(item_id.strip())
        except MarketplaceError as e:
            return self._error(e.status, str(e))
        except Exception as e:
            api_log(f"/api/marketplace/install error: {e}")
            return self._error(500, "install failed")
        self._send_json(200, result)

    def _handle_marketplace_uninstall(self):
        """POST /api/marketplace/uninstall — body: {"id": "<kind>:<name>" / bare name}."""
        data, err = self._read_json_body()
        if err:
            status, payload = err
            return self._send_json(status, payload)
        item_id = data.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            return self._error(400, "id is required")
        try:
            from agent.src.marketplace import MarketplaceError
            backend = _marketplace_backend()
            result = backend.uninstall_item(item_id.strip())
        except MarketplaceError as e:
            return self._error(e.status, str(e))
        except Exception as e:
            api_log(f"/api/marketplace/uninstall error: {e}")
            return self._error(500, "uninstall failed")
        self._send_json(200, result)

    # ------------------------------------------------------- zalo-personal

    def _handle_zalo_personal_status(self):
        """GET /api/channels/zalo-personal/status.

        Contract: {"connected": bool, "phone_masked": str | null}. The raw
        phone number is never exposed.
        """
        try:
            adapter = _zalo_personal_adapter()
            connected = bool(adapter.is_logged_in())
            phone_masked = adapter.get_masked_phone() or None
        except Exception as e:
            api_log("zalo-personal status error: %s" % type(e).__name__)
            return self._error(500, "failed to read zalo-personal status")
        self._send_json(200, {"connected": connected,
                              "phone_masked": phone_masked})

    def _handle_zalo_personal_logout(self):
        """POST /api/channels/zalo-personal/logout — always safe to call,
        even when no session exists."""
        try:
            _zalo_personal_adapter().logout()
        except Exception as e:
            api_log("zalo-personal logout error: %s" % type(e).__name__)
            return self._error(500, "failed to log out of zalo-personal")
        self._send_json(200, {"ok": True})

    def _handle_zalo_personal_login(self):
        """POST /api/channels/zalo-personal/login — body {phone, password}.

        Contract: {ok: true, phone_masked: "..."} on success. Empty/missing
        phone or password -> 400. Missing optional ``zlapi`` dependency or a
        failed login attempt -> 500 with an actionable message. The password
        is never echoed into the response or the logs.
        """
        data, err = self._read_json_body()
        if err:
            status, payload = err
            return self._send_json(status, payload)
        phone = data.get("phone")
        password = data.get("password")
        if not isinstance(phone, str) or not phone.strip():
            return self._error(400, "phone is required")
        if not isinstance(password, str) or not password:
            return self._error(400, "password is required")
        phone = phone.strip()

        def _scrub(text):
            # Credentials must never reach the response or the logs.
            return str(text).replace(password, "***").replace(phone, "***")

        try:
            adapter = _zalo_personal_adapter()
        except Exception as e:
            api_log("zalo-personal login error: adapter unavailable "
                    "(%s)" % type(e).__name__)
            return self._error(500, "zalo-personal adapter is unavailable")
        try:
            result = adapter.login(phone, password)
        except ValueError as e:
            return self._error(400, _scrub(str(e)) or "invalid credentials")
        except RuntimeError as e:
            msg = _scrub(str(e)) or "zalo-personal login failed"
            api_log("zalo-personal login failed: %s" % msg)
            return self._error(500, msg)
        except Exception as e:
            api_log("zalo-personal login error: %s" % type(e).__name__)
            return self._error(500, "zalo-personal login failed")
        finally:
            # Drop the cleartext reference as soon as the call returns.
            password = ""
        phone_masked = result.get("phone") if isinstance(result, dict) else None
        self._send_json(200, {"ok": True, "phone_masked": phone_masked})

    def _resolve_bot_context(self, bot_id):
        """Resolve (system_prompt, tool_names) for a bot id.

        tool_names is an allowlist (allowed minus denied), the
        ("__all_minus__", denied) sentinel for deny-only bots, or None for
        the full toolset. Raises LookupError on unknown bot_id. Returns
        (None, None) when bot_id is falsy or the bots module is unavailable
        (backward compatible).
        """
        if not bot_id:
            return None, None
        try:
            mgr = _bots_manager()
            bot = mgr.get_bot(bot_id)
        except Exception:
            return None, None
        if not bot:
            raise LookupError("unknown bot_id: %r" % (bot_id,))
        system_prompt = bot.get("system_prompt") or None
        allowed = bot.get("allowed_tools")
        denied = set(bot.get("denied_tools") or [])
        if allowed:
            tool_names = [t for t in allowed if t not in denied]
        elif denied:
            tool_names = ("__all_minus__", sorted(denied))
        else:
            tool_names = None
        return system_prompt, tool_names

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
        bot_id = data.get("bot_id")
        if bot_id is not None and not isinstance(bot_id, str):
            return self._error(400, "bot_id must be a string")
        try:
            bot_system_prompt, bot_tools = self._resolve_bot_context(bot_id)
        except LookupError as err:
            return self._error(404, str(err))

        if self.api.loop is not None:
            try:
                self.api.loop.load_session(session_id)
            except Exception as err:
                import traceback
                traceback.print_exc()
                return self._error(500, f"failed to load session: {err}")
        if self.api.brain is None:
            return self._error(503, "agent brain not configured")

        if not self.api.turn_lock.acquire(blocking=False):
            return self._error(429, "agent busy")
        try:
            if "text/event-stream" in (self.headers.get("Accept") or ""):
                self._chat_sse(prompt, strategy, session_id,
                               bot_system_prompt, bot_tools)
            else:
                try:
                    reply, used_strategy, errors = asyncio.run(
                        self.api.run_turn(prompt, strategy, session_id,
                                          bot_system_prompt=bot_system_prompt,
                                          bot_tools=bot_tools)
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

    def _chat_sse(self, prompt, strategy, session_id,
                  bot_system_prompt=None, bot_tools=None):
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
                self.api.run_turn(prompt, strategy, session_id,
                                  bot_system_prompt=bot_system_prompt,
                                  bot_tools=bot_tools)
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
