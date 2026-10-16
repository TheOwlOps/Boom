"""terminal.process — long-lived background process management for Boom.

Inherits from agent/src/tools/process_manager.py: ProcessManager tracks
processes through a JSON ledger (survives agent restarts), unlike shell.py's
background jobs (which only live in the current process).

Tool API: list_processes() / kill_process() (+ poll/start for full use).
The default ledger stays the same as the old one (~/AppData/Local/boom/processes.json)
for compatibility; change it via configure() or the BOOM_PROCESS_LEDGER env var.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

# An unread pipe blocks its writer at ~64 KiB -> output is drained off-thread.
MAX_CAPTURED_LINES = 500

# Env var to change the ledger file without editing code.
LEDGER_ENV = "BOOM_PROCESS_LEDGER"


class ProcessManager:
    """Background process management + JSON ledger (mirroring processes.json)."""

    def __init__(self, ledger_file: Optional[Path] = None):
        if ledger_file is None:
            env = os.environ.get(LEDGER_ENV)
            ledger_file = Path(env) if env else (
                Path.home() / "AppData" / "Local" / "boom" / "processes.json")
        self.ledger_file = Path(ledger_file)
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        self._active_procs: Dict[str, subprocess.Popen] = {}
        self._captured: Dict[str, Dict[str, Deque[str]]] = {}

    # -- ledger ----------------------------------------------------------
    def _load_ledger(self) -> Dict[str, Any]:
        if self.ledger_file.exists():
            try:
                return json.loads(self.ledger_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_ledger(self, data: Dict[str, Any]) -> None:
        self.ledger_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _set_ledger_status(self, session_id: str, status: str, **extra) -> bool:
        """Updates 1 ledger entry. False when the session isn't in the ledger."""
        ledger = self._load_ledger()
        if session_id not in ledger:
            return False
        ledger[session_id]["status"] = status
        ledger[session_id].update(extra)
        self._save_ledger(ledger)
        return True

    # -- output draining ---------------------------------------------------
    @staticmethod
    def _drain(buffers: Dict[str, Deque[str]], stream, key: str) -> None:
        try:
            for line in iter(stream.readline, ""):
                buffers[key].append(line)
        except (ValueError, OSError):
            pass  # pipe closed mid-read

    @staticmethod
    def _take(buf: Optional[Deque[str]]) -> str:
        """Takes (and clears) all buffered output since the last poll."""
        if not buf:
            return ""
        chunks = []
        while buf:
            chunks.append(buf.popleft())
        return "".join(chunks)

    # -- public API ------------------------------------------------------
    def start_process(self, session_id: str, cmd: List[str],
                      cwd: Optional[str] = None) -> Dict[str, Any]:
        """Starts a background process, records it in the ledger.

        Returns: {"session_id", "pid", "status": "running"}
                 or {"session_id", "error"} when cmd is empty/unlaunchable.
        """
        if not session_id or not isinstance(session_id, str):
            return {"session_id": session_id, "error": "session_id must be non-empty"}
        if not cmd:
            return {"session_id": session_id,
                    "error": "cmd must be a non-empty argv list"}
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=cwd)
        except (FileNotFoundError, OSError) as exc:
            return {"session_id": session_id, "error": str(exc)}
        self._active_procs[session_id] = proc
        self._captured[session_id] = {
            "stdout": deque(maxlen=MAX_CAPTURED_LINES),
            "stderr": deque(maxlen=MAX_CAPTURED_LINES),
        }
        for key, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            threading.Thread(target=self._drain,
                             args=(self._captured[session_id], stream, key),
                             daemon=True).start()
        ledger = self._load_ledger()
        ledger[session_id] = {"pid": proc.pid, "cmd": cmd,
                              "started_at": time.time(), "status": "running"}
        self._save_ledger(ledger)
        return {"session_id": session_id, "pid": proc.pid, "status": "running"}

    def poll_process(self, session_id: str) -> Dict[str, Any]:
        """Reads new output + process state (running/exited/unknown/not_found)."""
        proc = self._active_procs.get(session_id)
        captured = self._captured.get(session_id)
        out = self._take(captured["stdout"]) if captured else ""
        err = self._take(captured["stderr"]) if captured else ""
        if proc is not None:
            ret = proc.poll()
            if ret is None:
                return {"session_id": session_id, "status": "running",
                        "pid": proc.pid, "stdout": out, "stderr": err}
            # Keep the buffer: the caller still needs to read the exit tail.
            del self._active_procs[session_id]
            self._set_ledger_status(session_id, "exited", exit_code=ret)
            return {"session_id": session_id, "status": "exited",
                    "exit_code": ret, "stdout": out, "stderr": err}
        info = self._load_ledger().get(session_id)
        if not info:
            return {"session_id": session_id, "status": "not_found",
                    "stdout": out, "stderr": err}
        if info.get("status") == "running":
            # A process started by an older agent process, its pipes no longer
            # held -> don't guess, report unknown.
            info["status"] = "unknown"
        return {**info, "session_id": session_id, "stdout": out, "stderr": err}

    def kill_process(self, session_id: str) -> Dict[str, Any]:
        """Stops a process (SIGTERM, SIGKILL after 2s), updates the ledger."""
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
            return {"session_id": session_id, "status": "killed",
                    "stdout": out, "stderr": err}
        return {"session_id": session_id, "status": "not_found",
                "stdout": out, "stderr": err}

    def list_processes(self) -> List[Dict[str, Any]]:
        """Lists every process in the ledger."""
        ledger = self._load_ledger()
        return [{"session_id": k, **v} for k, v in ledger.items()]


# ---------------------------------------------------------------------------
# singleton + tool functions (compact API for the agent)
# ---------------------------------------------------------------------------

_MANAGER: Optional[ProcessManager] = None
_LEDGER_OVERRIDE: Optional[Path] = None


def configure(ledger_file: Optional[str] = None) -> Dict[str, Any]:
    """Changes the singleton's ledger file (call before first use).

    Args:
        ledger_file: path to the JSON ledger file, None for the default.
    Returns:
        {"ok": True, "ledger_file": str}.
    """
    global _MANAGER, _LEDGER_OVERRIDE
    _LEDGER_OVERRIDE = Path(ledger_file) if ledger_file else None
    _MANAGER = None  # force recreation on the next get_manager()
    return {"ok": True,
            "ledger_file": str(_LEDGER_OVERRIDE) if _LEDGER_OVERRIDE else "(default)"}


def get_manager() -> ProcessManager:
    """Gets the singleton ProcessManager (created lazily to avoid import-time side effects)."""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ProcessManager(ledger_file=_LEDGER_OVERRIDE)
    return _MANAGER


def start_process(session_id: str, cmd: List[str],
                  cwd: Optional[str] = None) -> Dict[str, Any]:
    """Tool: starts a ledger-tracked background process.

    Returns: {"session_id", "pid", "status"} or {"session_id", "error"}.
    """
    return get_manager().start_process(session_id, cmd, cwd)


def poll_process(session_id: str) -> Dict[str, Any]:
    """Tool: reads new output + process state."""
    return get_manager().poll_process(session_id)


def list_processes() -> Dict[str, Any]:
    """Tool: lists every process in the ledger.

    Returns: {"ok": True, "processes": [...]}.
    """
    try:
        return {"ok": True, "processes": get_manager().list_processes()}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def kill_process(session_id: str) -> Dict[str, Any]:
    """Tool: stops a process by session_id.

    Returns: {"session_id", "status": "killed"|"not_found", ...}.
    """
    if not session_id or not isinstance(session_id, str):
        return {"session_id": session_id, "status": "error",
                "error": "session_id must be non-empty"}
    try:
        return get_manager().kill_process(session_id)
    except OSError as exc:
        return {"session_id": session_id, "status": "error", "error": str(exc)}


__all__ = ["ProcessManager", "configure", "get_manager", "start_process",
           "poll_process", "list_processes", "kill_process",
           "MAX_CAPTURED_LINES"]
