import json
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

# A pipe nobody reads blocks its writer at ~64 KiB, so output is drained off-thread.
MAX_CAPTURED_LINES = 500

class ProcessManager:
    """Manages background long-running processes and daemon tracking (mirroring processes.json)."""

    def __init__(self, ledger_file: Optional[Path] = None):
        self.ledger_file = Path(ledger_file) if ledger_file else (Path.home() / "AppData" / "Local" / "boom" / "processes.json")
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        self._active_procs: Dict[str, subprocess.Popen] = {}
        self._captured: Dict[str, Dict[str, Deque[str]]] = {}

    def _load_ledger(self) -> Dict[str, Any]:
        if self.ledger_file.exists():
            try:
                return json.loads(self.ledger_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_ledger(self, data: Dict[str, Any]):
        self.ledger_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _set_ledger_status(self, session_id: str, status: str, **extra) -> bool:
        """Update one ledger entry in place. False if that session is not in the ledger."""
        ledger = self._load_ledger()
        if session_id not in ledger:
            return False
        ledger[session_id]["status"] = status
        ledger[session_id].update(extra)
        self._save_ledger(ledger)
        return True

    @staticmethod
    def _drain(buffers: Dict[str, Deque[str]], stream, key: str):
        try:
            for line in iter(stream.readline, ""):
                buffers[key].append(line)
        except (ValueError, OSError):
            pass  # pipe closed under us

    @staticmethod
    def _take(buf: Optional[Deque[str]]) -> str:
        """Consume and return everything buffered since the last poll."""
        if not buf:
            return ""
        chunks = []
        while buf:
            chunks.append(buf.popleft())
        return "".join(chunks)

    def start_process(self, session_id: str, cmd: List[str], cwd: Optional[str] = None) -> Dict[str, Any]:
        if not cmd:
            return {"session_id": session_id, "error": "cmd must be a non-empty argument list"}

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd
        )
        self._active_procs[session_id] = proc
        self._captured[session_id] = {
            "stdout": deque(maxlen=MAX_CAPTURED_LINES),
            "stderr": deque(maxlen=MAX_CAPTURED_LINES),
        }
        for key, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            threading.Thread(
                target=self._drain,
                args=(self._captured[session_id], stream, key),
                daemon=True,
            ).start()

        ledger = self._load_ledger()
        ledger[session_id] = {
            "pid": proc.pid,
            "cmd": cmd,
            "started_at": time.time(),
            "status": "running"
        }
        self._save_ledger(ledger)

        return {"session_id": session_id, "pid": proc.pid, "status": "running"}

    def poll_process(self, session_id: str) -> Dict[str, Any]:
        proc = self._active_procs.get(session_id)
        captured = self._captured.get(session_id)
        out = self._take(captured["stdout"]) if captured else ""
        err = self._take(captured["stderr"]) if captured else ""

        if proc is not None:
            ret = proc.poll()
            if ret is None:
                return {"session_id": session_id, "status": "running", "pid": proc.pid,
                        "stdout": out, "stderr": err}
            # Keep the buffers: the caller still needs to read the exit tail.
            del self._active_procs[session_id]
            self._set_ledger_status(session_id, "exited", exit_code=ret)
            return {"session_id": session_id, "status": "exited", "exit_code": ret,
                    "stdout": out, "stderr": err}

        info = self._load_ledger().get(session_id)
        if not info:
            return {"session_id": session_id, "status": "not_found", "stdout": out, "stderr": err}
        if info.get("status") == "running":
            # Started by an earlier agent process whose pipes we no longer hold, so this
            # process cannot know whether it is alive. Say so rather than guessing.
            info["status"] = "unknown"
        return {**info, "session_id": session_id, "stdout": out, "stderr": err}

    def kill_process(self, session_id: str) -> Dict[str, Any]:
        proc = self._active_procs.pop(session_id, None)
        captured = self._captured.pop(session_id, None)
        out = self._take(captured["stdout"]) if captured else ""
        err = self._take(captured["stderr"]) if captured else ""

        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

        if self._set_ledger_status(session_id, "killed"):
            return {"session_id": session_id, "status": "killed", "stdout": out, "stderr": err}
        return {"session_id": session_id, "status": "not_found", "stdout": out, "stderr": err}

    def list_processes(self) -> List[Dict[str, Any]]:
        ledger = self._load_ledger()
        return [{"session_id": k, **v} for k, v in ledger.items()]

    def manage(self, action: str, session_id: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        action = (action or "").strip().lower()
        if action == "start":
            return self.start_process(session_id or f"proc_{int(time.time()*1000)}", kwargs.get("cmd", []), kwargs.get("cwd"))
        elif action == "poll":
            return self.poll_process(session_id or "")
        elif action == "kill":
            return self.kill_process(session_id or "")
        elif action == "list":
            return {"processes": self.list_processes()}
        return {"error": f"Unknown process action '{action}'"}
