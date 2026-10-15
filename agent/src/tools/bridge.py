import asyncio
import json
import socket
from typing import Any, Dict, List, Optional

class IpcBridgeClient:
    """Client for native Rust Core IPC service supporting both async and sync dispatch."""

    def __init__(self, host: str = "127.0.0.1", port: int = 51234):
        self.host = host
        self.port = port
        self._seq = 1
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        # One connection is shared, so request/response pairs must not interleave.
        self._lock = asyncio.Lock()

    async def connect(self):
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)

    async def close(self):
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
            self._writer = None
            self._reader = None

    @staticmethod
    def _decode(line: bytes, req_id: int) -> Dict[str, Any]:
        try:
            return json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return {"id": req_id, "error": f"Malformed response from core: {e}"}

    async def send_rpc_async(self, method: str, params: Any = None) -> Dict[str, Any]:
        async with self._lock:
            if not self._writer:
                await self.connect()

            req_id = self._seq
            self._seq += 1
            req = {"id": req_id, "method": method, "params": params or {}}
            payload = json.dumps(req) + "\n"

            self._writer.write(payload.encode("utf-8"))
            await self._writer.drain()

            line = await self._reader.readline()
            if not line:
                return {"id": req_id, "error": "No response from core"}
            return self._decode(line, req_id)

    def send_rpc(self, method: str, params: Any = None) -> Dict[str, Any]:
        req_id = self._seq
        self._seq += 1
        req = {
            "id": req_id,
            "method": method,
            "params": params or {}
        }
        payload = json.dumps(req) + "\n"

        try:
            with socket.create_connection((self.host, self.port), timeout=5.0) as s:
                s.sendall(payload.encode("utf-8"))
                reader = s.makefile("r", encoding="utf-8")
                line = reader.readline()
        except OSError as e:
            return {"id": req_id, "error": f"Cannot reach core at {self.host}:{self.port}: {e}"}

        if not line:
            return {"id": req_id, "error": "No response from core"}
        return self._decode(line.encode("utf-8"), req_id)

    async def ping(self) -> str:
        res = await self.send_rpc_async("ping")
        return res.get("result", {}).get("status", "error")

    @staticmethod
    def _shape(res: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize an RPC reply, preserving the core's real exit status."""
        if res.get("error"):
            return {
                "error": res["error"],
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
                "timed_out": False,
                "success": False,
            }
        result = res.get("result") or {}
        exit_code = result.get("exit_code", 0)
        return {
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "exit_code": exit_code,
            "timed_out": result.get("timed_out", False),
            "success": exit_code == 0,
        }

    async def execute(self, cmd: str, args: Optional[List[str]] = None) -> Dict[str, Any]:
        res = await self.send_rpc_async("sandbox_exec", {"cmd": cmd, "args": args or []})
        return self._shape(res)

    def execute_sync(self, cmd: str, args: Optional[List[str]] = None) -> Dict[str, Any]:
        res = self.send_rpc("sandbox_exec", {"cmd": cmd, "args": args or []})
        return self._shape(res)
