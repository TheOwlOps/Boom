"""terminal.shell — safe shell command execution for Boom.

Inherited from agent/src/tools/shell.py, with a tidier API:
run / bg / status / kill (/ forget).

Safety model:
- NEVER uses shell=True. Command strings go through shlex.split, so
  `;`, `|`, `&&`, `$(...)` are just literal arguments, never interpreted.
- Denylist blocks dangerous commands: rm -rf /, mkfs, dd writing /dev/*,
  fork bombs, power commands (shutdown/reboot/...).
- cwd is jailed inside allowed_root (default: the current cwd).
- Each stream's output is capped at 100 KiB; run() kills the process after
  `timeout` seconds (default 30s for EVERY command).

Stdlib + pydantic only.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Union

from pydantic import BaseModel, Field, ValidationError

MAX_OUTPUT_BYTES = 100 * 1024  # 100 KiB per stream
KILL_GRACE_SECONDS = 5
DEFAULT_TIMEOUT = 30  # default timeout for every run() command

# Classic fork bomb: :(){ :|:& };:
_FORK_BOMB_RE = re.compile(r":\(\)\s*\{[^}]*\}\s*;\s*:")


class RunOptions(BaseModel):
    """Validated parameters for run()/bg(): timeout, cwd, allowed_root."""

    timeout: int = Field(default=DEFAULT_TIMEOUT, gt=0, le=600,
                         description="timeout in seconds, 1..600")
    cwd: Optional[str] = Field(default=None, description="directory to run the command in")
    allowed_root: Optional[str] = Field(
        default=None, description="cwd must be inside this root")


def _split_cmd(cmd: Union[str, List[str]]) -> List[str]:
    """Split a command into argv. Strings go through shlex (never a shell)."""
    if isinstance(cmd, list):
        return list(cmd)
    return shlex.split(cmd)


def _is_blocked(argv: List[str]) -> bool:
    """True if argv matches a dangerous-command pattern (denylist)."""
    if not argv:
        return True  # empty commands are blocked too
    exe = os.path.basename(argv[0])

    # Power / system-destroying commands
    if exe in {"shutdown", "reboot", "poweroff", "halt", "init"}:
        return True

    # mkfs.* in all forms
    if exe == "mkfs" or exe.startswith("mkfs."):
        return True

    # dd touching raw devices
    if exe == "dd":
        for arg in argv[1:]:
            if arg.startswith("if=/dev/") or arg.startswith("of=/dev/"):
                return True

    # rm -rf against the system root
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
    """Returns an error string if the command is blocked, otherwise None."""
    if not isinstance(cmd, (str, list)):
        return "blocked: cmd must be a string or an argv list"
    if isinstance(cmd, str) and _FORK_BOMB_RE.search(cmd):
        return "blocked: dangerous command (fork bomb pattern)"
    try:
        argv = _split_cmd(cmd)
    except ValueError as exc:  # broken quoting...
        return f"blocked: could not parse command ({exc})"
    if _is_blocked(argv):
        return "blocked: dangerous command"
    return None


def _resolve_cwd(cwd: Optional[str], allowed_root: Optional[str]) -> str:
    """Resolves cwd and ensures it is inside allowed_root, else raises PermissionError."""
    root = Path(allowed_root).resolve() if allowed_root else Path.cwd().resolve()
    target = Path(cwd).resolve() if cwd else root
    try:
        target.relative_to(root)
    except ValueError:
        raise PermissionError(f"cwd {target} is outside allowed_root {root}")
    return str(target)


def _validate_options(timeout: int, cwd: Optional[str],
                      allowed_root: Optional[str]) -> tuple[Optional[RunOptions], Optional[str]]:
    """Validates parameters with pydantic. Returns (options, None) or (None, error)."""
    try:
        return RunOptions(timeout=timeout, cwd=cwd, allowed_root=allowed_root), None
    except ValidationError as exc:
        return None, f"invalid parameters: {exc.errors()[0]['msg']}"


def _truncate(data: bytes) -> str:
    """Decodes bytes, capped at MAX_OUTPUT_BYTES."""
    if len(data) > MAX_OUTPUT_BYTES:
        data = data[:MAX_OUTPUT_BYTES] + b"\n...[truncated at 100KB]..."
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# synchronous run
# ---------------------------------------------------------------------------

def run(cmd: Union[str, List[str]], timeout: int = DEFAULT_TIMEOUT,
        cwd: Optional[str] = None,
        allowed_root: Optional[str] = None) -> Dict:
    """Runs a command synchronously, capturing stdout/stderr.

    Description:
        Runs the command and waits for it (up to *timeout* seconds, default
        30s). Denylisted dangerous commands are blocked before running.
    Args:
        cmd: command string or argv list.
        timeout: timeout in seconds (1..600), default 30.
        cwd: directory to run the command in.
        allowed_root: cwd must be inside this root.
    Returns:
        {"ok", "returncode", "stdout", "stderr", "timed_out"}.
        timed_out=True when the process was killed for exceeding the timeout.
    Raises:
        Never raises — all errors are returned as dicts.
    """
    blocked = _check_dangerous(cmd)
    if blocked:
        return {"ok": False, "returncode": -1, "stdout": "",
                "stderr": blocked, "timed_out": False}
    opts, err = _validate_options(timeout, cwd, allowed_root)
    if err:
        return {"ok": False, "returncode": -1, "stdout": "",
                "stderr": err, "timed_out": False}
    assert opts is not None
    try:
        cwd_resolved = _resolve_cwd(opts.cwd, opts.allowed_root)
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
            timeout=opts.timeout,
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
# background jobs
# ---------------------------------------------------------------------------

_JOBS: Dict[str, Dict] = {}
_JOBS_LOCK = threading.Lock()


def _drain(stream, buf: List[bytes], proc: subprocess.Popen) -> None:
    """Reads a pipe to EOF, keeping at most MAX_OUTPUT_BYTES (keeps the tail)."""
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            buf.append(chunk)
            total = sum(len(c) for c in buf)
            if total > MAX_OUTPUT_BYTES:
                joined = b"".join(buf)
                buf.clear()
                buf.append(joined[-MAX_OUTPUT_BYTES:])
    except (ValueError, OSError):
        pass  # pipe closed mid-read
    finally:
        try:
            stream.close()
        except Exception:
            pass


def bg(cmd: Union[str, List[str]], cwd: Optional[str] = None,
       allowed_root: Optional[str] = None) -> Dict:
    """Runs a command in the background, returning a job_id immediately.

    Description:
        Starts a background process; output is buffered and readable via
        status(). Background jobs have no timeout (use kill() to stop them).
    Args:
        cmd: command string or argv list.
        cwd: directory to run the command in.
        allowed_root: cwd must be inside this root.
    Returns:
        {"ok": True, "job_id": str} or {"ok": False, "error": str}.
    Raises:
        Never raises — all errors are returned as dicts.
    """
    blocked = _check_dangerous(cmd)
    if blocked:
        return {"ok": False, "error": blocked}
    opts, err = _validate_options(DEFAULT_TIMEOUT, cwd, allowed_root)
    if err:
        return {"ok": False, "error": err}
    assert opts is not None
    try:
        cwd_resolved = _resolve_cwd(opts.cwd, opts.allowed_root)
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
    entry = {"proc": proc, "out": [], "err": [], "cmd": argv}
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


def status(job_id: str) -> Dict:
    """Checks a background job's status: running or done, with buffered output.

    Args:
        job_id: the id returned by bg().
    Returns:
        {"ok": True, "job_id", "running": bool, "returncode": int|None,
         "stdout": str, "stderr": str}
        or {"ok": False, "error": "not found"}.
    Raises:
        Never raises — all errors are returned as dicts.
    """
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


def kill(job_id: str) -> Dict:
    """Stops a background job: SIGTERM, then SIGKILL if still alive after 5s.

    Args:
        job_id: the id returned by bg().
    Returns:
        {"ok": True, "job_id": str} or {"ok": False, "error": "not found"}.
    Raises:
        Never raises — all errors are returned as dicts.
    """
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


def forget(job_id: str) -> Dict:
    """Drops a finished job from the registry to free memory.

    Args:
        job_id: the id returned by bg().
    Returns:
        {"ok": True, "job_id": str} or {"ok": False, "error": str}
        (not found / job still running).
    Raises:
        Never raises — all errors are returned as dicts.
    """
    with _JOBS_LOCK:
        entry = _JOBS.get(job_id)
        if entry is None:
            return {"ok": False, "error": "not found"}
        if entry["proc"].poll() is None:
            return {"ok": False, "error": "job still running"}
        del _JOBS[job_id]
    return {"ok": True, "job_id": job_id}


__all__ = ["run", "bg", "status", "kill", "forget", "RunOptions",
           "DEFAULT_TIMEOUT"]
