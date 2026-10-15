"""Unified shell tools for Boom.

A small, stdlib-only module providing a safe, synchronous ``run``,
a fire-and-forget ``run_bg``, plus ``job_status`` / ``job_kill``
for managing background jobs.

Safety model
------------
* Commands are NEVER executed with ``shell=True``.  A string command is
  tokenised with :func:`shlex.split`, so ``;``, ``|``, ``&&``, ``$(...)``
  and friends are passed as literal arguments, not interpreted.
* A blocklist rejects obviously destructive commands (``rm -rf /``,
  ``mkfs``, ``dd`` against ``/dev/*``, fork bombs, power commands).
* ``cwd`` is confined to ``allowed_root`` (defaults to the current
  working directory) so a tool call cannot escape the project tree.
* Output is capped at 100 KiB per stream; ``run`` kills the child after
  ``timeout`` seconds.

This is complementary to the existing tooling in this tree:
``tools/process_manager.py`` (ledger-backed daemon tracking),
``tools/code_execution.py`` (in-process Python kernel) and the
``sandbox_exec`` tool (Rust sandbox over IPC).  Use this module when you
want a plain, dependency-free shell runner with explicit safety checks.
"""

import os
import re
import shlex
import signal
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Union

MAX_OUTPUT_BYTES = 100 * 1024  # 100 KiB per stream
KILL_GRACE_SECONDS = 5

_FORK_BOMB_RE = re.compile(r":\(\)\s*\{[^}]*\}\s*;\s*:")


def _split_cmd(cmd: Union[str, List[str]]) -> List[str]:
    """Tokenise a command.  Strings go through shlex (never the shell)."""
    if isinstance(cmd, list):
        return list(cmd)
    return shlex.split(cmd)


def _is_blocked(argv: List[str]) -> bool:
    """True if the argv list matches a dangerous-command pattern."""
    if not argv:
        return False
    exe = os.path.basename(argv[0])

    # Power / destructive system commands
    if exe in {"shutdown", "reboot", "poweroff", "halt", "init"}:
        return True

    # mkfs.* in any form
    if exe == "mkfs" or exe.startswith("mkfs."):
        return True

    # dd touching raw devices
    if exe == "dd":
        for arg in argv[1:]:
            if arg.startswith("if=/dev/") or arg.startswith("of=/dev/"):
                return True

    # rm -rf <root>
    if exe == "rm":
        if "--no-preserve-root" in argv:
            return True
        flags = [a for a in argv[1:] if a.startswith("-") and a != "-"]
        flag_chars = set("".join(f[1:] for f in flags if not f.startswith("--")))
        targets = [a for a in argv[1:] if not a.startswith("-")]
        if "r" in flag_chars and "f" in flag_chars:
            for t in targets:
                norm = t.rstrip("/") or "/"
                if norm in ("/", "/*"):
                    return True
    return False


def _check_dangerous(cmd: Union[str, List[str]]) -> Optional[str]:
    """Return an error string if the command is blocked, else None."""
    if isinstance(cmd, str) and _FORK_BOMB_RE.search(cmd):
        return "blocked: dangerous command (fork bomb pattern)"
    try:
        argv = _split_cmd(cmd)
    except ValueError as exc:  # bad quoting etc.
        return f"blocked: cannot parse command ({exc})"
    if _is_blocked(argv):
        return "blocked: dangerous command"
    return None


def _resolve_cwd(cwd: Optional[str], allowed_root: Optional[str]) -> str:
    root = Path(allowed_root).resolve() if allowed_root else Path.cwd().resolve()
    target = Path(cwd).resolve() if cwd else root
    try:
        target.relative_to(root)
    except ValueError:
        raise PermissionError(f"cwd {target} is outside allowed_root {root}")
    return str(target)


def _truncate(data: bytes) -> str:
    if len(data) > MAX_OUTPUT_BYTES:
        data = data[:MAX_OUTPUT_BYTES] + b"\n...[truncated at 100KB]..."
    return data.decode("utf-8", errors="replace")


def run(
    cmd: Union[str, List[str]],
    timeout: int = 30,
    cwd: Optional[str] = None,
    allowed_root: Optional[str] = None,
) -> Dict:
    """Run a command synchronously, capturing stdout/stderr.

    Returns ``{"ok", "returncode", "stdout", "stderr", "timed_out"}``.
    ``timed_out`` is True when the child was killed after ``timeout`` s.
    """
    blocked = _check_dangerous(cmd)
    if blocked:
        return {"ok": False, "returncode": -1, "stdout": "",
                "stderr": blocked, "timed_out": False}
    try:
        cwd_resolved = _resolve_cwd(cwd, allowed_root)
    except PermissionError as exc:
        return {"ok": False, "returncode": -1, "stdout": "",
                "stderr": str(exc), "timed_out": False}
    argv = _split_cmd(cmd)
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd_resolved,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": _truncate(exc.stdout or b""),
            "stderr": _truncate(exc.stderr or b""),
            "timed_out": True,
        }
    except FileNotFoundError as exc:
        return {"ok": False, "returncode": -1, "stdout": "",
                "stderr": str(exc), "timed_out": False}
    except OSError as exc:
        return {"ok": False, "returncode": -1, "stdout": "",
                "stderr": str(exc), "timed_out": False}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": _truncate(proc.stdout),
        "stderr": _truncate(proc.stderr),
        "timed_out": False,
    }


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------

_JOBS: Dict[str, Dict] = {}
_JOBS_LOCK = threading.Lock()


def _drain(stream, buf: List[bytes], proc: subprocess.Popen) -> None:
    """Read a pipe until EOF, keeping at most MAX_OUTPUT_BYTES."""
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            buf.append(chunk)
            total = sum(len(c) for c in buf)
            if total > MAX_OUTPUT_BYTES:
                # keep the tail; head is dropped
                joined = b"".join(buf)
                buf.clear()
                buf.append(joined[-MAX_OUTPUT_BYTES:])
    except (ValueError, OSError):
        pass  # pipe closed under us
    finally:
        try:
            stream.close()
        except Exception:
            pass


def run_bg(
    cmd: Union[str, List[str]],
    cwd: Optional[str] = None,
    allowed_root: Optional[str] = None,
) -> Dict:
    """Start a command in the background; return immediately with a job_id."""
    blocked = _check_dangerous(cmd)
    if blocked:
        return {"ok": False, "error": blocked}
    try:
        cwd_resolved = _resolve_cwd(cwd, allowed_root)
    except PermissionError as exc:
        return {"ok": False, "error": str(exc)}
    argv = _split_cmd(cmd)
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd_resolved,
        )
    except (FileNotFoundError, OSError) as exc:
        return {"ok": False, "error": str(exc)}

    job_id = "job_" + uuid.uuid4().hex[:12]
    entry = {
        "proc": proc,
        "out": [],
        "err": [],
        "cmd": argv,
    }
    entry["out_thread"] = threading.Thread(
        target=_drain, args=(proc.stdout, entry["out"], proc), daemon=True)
    entry["err_thread"] = threading.Thread(
        target=_drain, args=(proc.stderr, entry["err"], proc), daemon=True)
    entry["out_thread"].start()
    entry["err_thread"].start()
    with _JOBS_LOCK:
        _JOBS[job_id] = entry
    return {"ok": True, "job_id": job_id}


def _join_drainers(entry: Dict) -> None:
    for key in ("out_thread", "err_thread"):
        t = entry.get(key)
        if t is not None:
            t.join(timeout=2)


def job_status(job_id: str) -> Dict:
    """Return running state, returncode and buffered output for a job."""
    with _JOBS_LOCK:
        entry = _JOBS.get(job_id)
    if entry is None:
        return {"ok": False, "error": "not found"}
    proc = entry["proc"]
    rc = proc.poll()
    if rc is not None:
        _join_drainers(entry)  # make sure all output was collected
    return {
        "ok": True,
        "job_id": job_id,
        "running": rc is None,
        "returncode": rc,
        "stdout": b"".join(entry["out"]).decode("utf-8", errors="replace"),
        "stderr": b"".join(entry["err"]).decode("utf-8", errors="replace"),
    }


def job_kill(job_id: str) -> Dict:
    """SIGTERM a job, SIGKILL after 5 s if it is still alive."""
    with _JOBS_LOCK:
        entry = _JOBS.get(job_id)
    if entry is None:
        return {"ok": False, "error": "not found"}
    proc = entry["proc"]
    if proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait(timeout=KILL_GRACE_SECONDS)
    _join_drainers(entry)
    return {"ok": True, "job_id": job_id}


def job_forget(job_id: str) -> Dict:
    """Drop a finished job from the registry to free memory."""
    with _JOBS_LOCK:
        entry = _JOBS.get(job_id)
        if entry is None:
            return {"ok": False, "error": "not found"}
        if entry["proc"].poll() is None:
            return {"ok": False, "error": "job still running"}
        del _JOBS[job_id]
    return {"ok": True, "job_id": job_id}
