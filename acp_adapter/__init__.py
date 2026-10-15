"""ACP (Agent Client Protocol) adapter for Boom.

This module is the adapter that works: a minimal JSON-RPC surface that drives the
real agent loop. The hermes-derived modules beside it (server.py, session.py,
tools.py, events.py, permissions.py, commands.py, model_catalog.py, content.py,
entry.py) do not import — they need the `acp` SDK plus hermes_cli / hermes_constants
/ hermes_state_registry, none of which is installed or vendored here. They are
porting material until that dependency story is settled.
"""

import time
from typing import Any, Dict, List, Optional

class AcpServer:
    def __init__(self, react_loop: Any = None):
        self.react_loop = react_loop
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self._seq = 1

    async def handle_request(self, req: Dict[str, Any]) -> Dict[str, Any]:
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "0.1.0",
                    "serverInfo": {"name": "harness-acp-adapter", "version": "0.1.0"},
                    "capabilities": {"sessions": True, "streaming": True}
                }
            }

        elif method == "session/new":
            s_id = f"acp_sess_{self._seq}"
            self._seq += 1
            self.sessions[s_id] = {
                "sessionId": s_id,
                "model": params.get("model", "default"),
                "messages": 0,
                "created_at": time.time()
            }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"sessionId": s_id}
            }

        elif method == "session/prompt":
            s_id = params.get("sessionId")
            prompt = params.get("prompt", "")
            if s_id not in self.sessions:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Invalid sessionId"}}
            if self.react_loop is None:
                # Previously this returned "Acknowledged: <prompt>", which read like an
                # answer while no model was ever called.
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32603,
                        "message": "No agent loop attached; construct AcpServer(react_loop=ReActLoop(env, llm))",
                    },
                }
            self.react_loop.session_id = s_id
            turn = await self.react_loop.step(prompt)
            self.sessions[s_id]["messages"] += 2
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "sessionId": s_id,
                    "reply": turn.get("action", ""),
                    "steps": turn.get("steps", 0),
                    "success": turn.get("success", False),
                }
            }

        elif method == "session/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"sessions": list(self.sessions.values())}
            }

        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method {method} not found"}}
