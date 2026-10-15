"""terminal.process — quản lý tiến trình nền dài hạn cho Boom.

Kế thừa từ agent/src/tools/process_manager.py: ProcessManager theo dõi
tiến trình qua ledger JSON (sống sót qua restart agent), khác với job nền
trong shell.py (chỉ sống trong tiến trình hiện tại).

API tool: list_processes() / kill_process() (+ poll/start để dùng đầy đủ).
Ledger mặc định giữ nguyên như bản cũ (~/AppData/Local/boom/processes.json)
để tương thích; có thể đổi bằng configure() hoặc env BOOM_PROCESS_LEDGER.

Chỉ dùng stdlib.
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

# Pipe không ai đọc sẽ block writer ở ~64 KiB -> output được drain off-thread.
MAX_CAPTURED_LINES = 500

# Env var để đổi file ledger mà không cần sửa code.
LEDGER_ENV = "BOOM_PROCESS_LEDGER"


class ProcessManager:
    """Quản lý tiến trình nền + ledger JSON (mirroring processes.json)."""

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
        """Cập nhật 1 entry trong ledger. False nếu session không có trong ledger."""
        ledger = self._load_ledger()
        if session_id not in ledger:
            return False
        ledger[session_id]["status"] = status
        ledger[session_id].update(extra)
        self._save_ledger(ledger)
        return True

    # -- drain output ----------------------------------------------------
    @staticmethod
    def _drain(buffers: Dict[str, Deque[str]], stream, key: str) -> None:
        try:
            for line in iter(stream.readline, ""):
                buffers[key].append(line)
        except (ValueError, OSError):
            pass  # pipe bị đóng giữa chừng

    @staticmethod
    def _take(buf: Optional[Deque[str]]) -> str:
        """Lấy (và xóa) toàn bộ output đã buffer từ lần poll trước."""
        if not buf:
            return ""
        chunks = []
        while buf:
            chunks.append(buf.popleft())
        return "".join(chunks)

    # -- public API ------------------------------------------------------
    def start_process(self, session_id: str, cmd: List[str],
                      cwd: Optional[str] = None) -> Dict[str, Any]:
        """Khởi động tiến trình nền, ghi vào ledger.

        Returns: {"session_id", "pid", "status": "running"}
                 hoặc {"session_id", "error"} khi cmd rỗng/không chạy được.
        """
        if not session_id or not isinstance(session_id, str):
            return {"session_id": session_id, "error": "session_id phải non-empty"}
        if not cmd:
            return {"session_id": session_id,
                    "error": "cmd phải là list argv non-empty"}
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
        """Đọc output mới + trạng thái tiến trình (running/exited/unknown/not_found)."""
        proc = self._active_procs.get(session_id)
        captured = self._captured.get(session_id)
        out = self._take(captured["stdout"]) if captured else ""
        err = self._take(captured["stderr"]) if captured else ""
        if proc is not None:
            ret = proc.poll()
            if ret is None:
                return {"session_id": session_id, "status": "running",
                        "pid": proc.pid, "stdout": out, "stderr": err}
            # Giữ buffer: caller vẫn cần đọc exit tail.
            del self._active_procs[session_id]
            self._set_ledger_status(session_id, "exited", exit_code=ret)
            return {"session_id": session_id, "status": "exited",
                    "exit_code": ret, "stdout": out, "stderr": err}
        info = self._load_ledger().get(session_id)
        if not info:
            return {"session_id": session_id, "status": "not_found",
                    "stdout": out, "stderr": err}
        if info.get("status") == "running":
            # Tiến trình do agent process cũ khởi động, pipe không còn giữ được
            # -> không đoán bừa, báo unknown.
            info["status"] = "unknown"
        return {**info, "session_id": session_id, "stdout": out, "stderr": err}

    def kill_process(self, session_id: str) -> Dict[str, Any]:
        """Dừng tiến trình (SIGTERM, 2s chưa chết thì SIGKILL), cập nhật ledger."""
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
        """Liệt kê mọi tiến trình trong ledger."""
        ledger = self._load_ledger()
        return [{"session_id": k, **v} for k, v in ledger.items()]


# ---------------------------------------------------------------------------
# singleton + tool functions (API gọn cho agent)
# ---------------------------------------------------------------------------

_MANAGER: Optional[ProcessManager] = None
_LEDGER_OVERRIDE: Optional[Path] = None


def configure(ledger_file: Optional[str] = None) -> Dict[str, Any]:
    """Đổi file ledger cho singleton (gọi trước lần dùng đầu tiên).

    Args:
        ledger_file: đường dẫn file ledger JSON, None để về mặc định.
    Returns:
        {"ok": True, "ledger_file": str}.
    """
    global _MANAGER, _LEDGER_OVERRIDE
    _LEDGER_OVERRIDE = Path(ledger_file) if ledger_file else None
    _MANAGER = None  # ép tạo lại ở lần get_manager() tới
    return {"ok": True,
            "ledger_file": str(_LEDGER_OVERRIDE) if _LEDGER_OVERRIDE else "(default)"}


def get_manager() -> ProcessManager:
    """Lấy singleton ProcessManager (tạo lazy để tránh side-effect khi import)."""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ProcessManager(ledger_file=_LEDGER_OVERRIDE)
    return _MANAGER


def start_process(session_id: str, cmd: List[str],
                  cwd: Optional[str] = None) -> Dict[str, Any]:
    """Tool: khởi động tiến trình nền có ledger.

    Returns: {"session_id", "pid", "status"} hoặc {"session_id", "error"}.
    """
    return get_manager().start_process(session_id, cmd, cwd)


def poll_process(session_id: str) -> Dict[str, Any]:
    """Tool: đọc output mới + trạng thái tiến trình."""
    return get_manager().poll_process(session_id)


def list_processes() -> Dict[str, Any]:
    """Tool: liệt kê mọi tiến trình trong ledger.

    Returns: {"ok": True, "processes": [...]}.
    """
    try:
        return {"ok": True, "processes": get_manager().list_processes()}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def kill_process(session_id: str) -> Dict[str, Any]:
    """Tool: dừng tiến trình theo session_id.

    Returns: {"session_id", "status": "killed"|"not_found", ...}.
    """
    if not session_id or not isinstance(session_id, str):
        return {"session_id": session_id, "status": "error",
                "error": "session_id phải non-empty"}
    try:
        return get_manager().kill_process(session_id)
    except OSError as exc:
        return {"session_id": session_id, "status": "error", "error": str(exc)}


__all__ = ["ProcessManager", "configure", "get_manager", "start_process",
           "poll_process", "list_processes", "kill_process",
           "MAX_CAPTURED_LINES"]
