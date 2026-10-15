"""terminal.shell — chạy lệnh shell an toàn cho Boom.

Kế thừa từ agent/src/tools/shell.py, đổi tên API cho gọn:
run / bg / status / kill (/ forget).

Mô hình an toàn:
- KHÔNG BAO GIỜ dùng shell=True. Chuỗi lệnh được tách bằng shlex.split
  nên `;`, `|`, `&&`, `$(...)` chỉ là tham số chữ, không được diễn giải.
- Denylist chặn lệnh nguy hiểm: rm -rf /, mkfs, dd ghi /dev/*,
  fork bomb, lệnh nguồn (shutdown/reboot/...).
- cwd bị nhốt trong allowed_root (mặc định: cwd hiện tại).
- Output mỗi stream cắt ở 100 KiB; run() kill tiến trình sau `timeout` giây
  (mặc định 30s cho MỌI lệnh).

Chỉ dùng stdlib + pydantic.
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

MAX_OUTPUT_BYTES = 100 * 1024  # 100 KiB mỗi stream
KILL_GRACE_SECONDS = 5
DEFAULT_TIMEOUT = 30  # timeout mặc định cho mọi lệnh run()

# Fork bomb kinh điển: :(){ :|:& };:
_FORK_BOMB_RE = re.compile(r":\(\)\s*\{[^}]*\}\s*;\s*:")


class RunOptions(BaseModel):
    """Tham số validate cho run()/bg(): timeout, cwd, allowed_root."""

    timeout: int = Field(default=DEFAULT_TIMEOUT, gt=0, le=600,
                         description="timeout giây, 1..600")
    cwd: Optional[str] = Field(default=None, description="thư mục chạy lệnh")
    allowed_root: Optional[str] = Field(
        default=None, description="cwd phải nằm trong root này")


def _split_cmd(cmd: Union[str, List[str]]) -> List[str]:
    """Tách lệnh thành argv. Chuỗi đi qua shlex (không qua shell)."""
    if isinstance(cmd, list):
        return list(cmd)
    return shlex.split(cmd)


def _is_blocked(argv: List[str]) -> bool:
    """True nếu argv khớp pattern lệnh nguy hiểm (denylist)."""
    if not argv:
        return True  # lệnh rỗng cũng bị chặn
    exe = os.path.basename(argv[0])

    # Lệnh nguồn / phá hệ thống
    if exe in {"shutdown", "reboot", "poweroff", "halt", "init"}:
        return True

    # mkfs.* mọi dạng
    if exe == "mkfs" or exe.startswith("mkfs."):
        return True

    # dd động vào thiết bị thô
    if exe == "dd":
        for arg in argv[1:]:
            if arg.startswith("if=/dev/") or arg.startswith("of=/dev/"):
                return True

    # rm -rf vào gốc hệ thống
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
    """Trả về chuỗi lỗi nếu lệnh bị chặn, ngược lại None."""
    if not isinstance(cmd, (str, list)):
        return "blocked: cmd phải là chuỗi hoặc list argv"
    if isinstance(cmd, str) and _FORK_BOMB_RE.search(cmd):
        return "blocked: dangerous command (fork bomb pattern)"
    try:
        argv = _split_cmd(cmd)
    except ValueError as exc:  # quote hỏng...
        return f"blocked: không parse được lệnh ({exc})"
    if _is_blocked(argv):
        return "blocked: dangerous command"
    return None


def _resolve_cwd(cwd: Optional[str], allowed_root: Optional[str]) -> str:
    """Resolve cwd và đảm bảo nằm trong allowed_root, sai thì raise PermissionError."""
    root = Path(allowed_root).resolve() if allowed_root else Path.cwd().resolve()
    target = Path(cwd).resolve() if cwd else root
    try:
        target.relative_to(root)
    except ValueError:
        raise PermissionError(f"cwd {target} nằm ngoài allowed_root {root}")
    return str(target)


def _validate_options(timeout: int, cwd: Optional[str],
                      allowed_root: Optional[str]) -> tuple[Optional[RunOptions], Optional[str]]:
    """Validate tham số bằng pydantic. Trả về (options, None) hoặc (None, lỗi)."""
    try:
        return RunOptions(timeout=timeout, cwd=cwd, allowed_root=allowed_root), None
    except ValidationError as exc:
        return None, f"tham số không hợp lệ: {exc.errors()[0]['msg']}"


def _truncate(data: bytes) -> str:
    """Decode bytes, cắt ở MAX_OUTPUT_BYTES."""
    if len(data) > MAX_OUTPUT_BYTES:
        data = data[:MAX_OUTPUT_BYTES] + b"\n...[truncated at 100KB]..."
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# chạy đồng bộ
# ---------------------------------------------------------------------------

def run(cmd: Union[str, List[str]], timeout: int = DEFAULT_TIMEOUT,
        cwd: Optional[str] = None,
        allowed_root: Optional[str] = None) -> Dict:
    """Chạy lệnh đồng bộ, thu stdout/stderr.

    Mô tả:
        Chạy lệnh và đợi kết thúc (tối đa *timeout* giây, mặc định 30s).
        Lệnh nguy hiểm trong denylist bị chặn trước khi chạy.
    Args:
        cmd: chuỗi lệnh hoặc list argv.
        timeout: timeout giây (1..600), mặc định 30.
        cwd: thư mục chạy lệnh.
        allowed_root: cwd phải nằm trong root này.
    Returns:
        {"ok", "returncode", "stdout", "stderr", "timed_out"}.
        timed_out=True khi tiến trình bị kill do quá timeout.
    Raises:
        Không raise — mọi lỗi trả về dict.
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
# job nền
# ---------------------------------------------------------------------------

_JOBS: Dict[str, Dict] = {}
_JOBS_LOCK = threading.Lock()


def _drain(stream, buf: List[bytes], proc: subprocess.Popen) -> None:
    """Đọc pipe tới EOF, giữ tối đa MAX_OUTPUT_BYTES (giữ phần đuôi)."""
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
        pass  # pipe bị đóng giữa chừng
    finally:
        try:
            stream.close()
        except Exception:
            pass


def bg(cmd: Union[str, List[str]], cwd: Optional[str] = None,
       allowed_root: Optional[str] = None) -> Dict:
    """Chạy lệnh ở chế độ nền, trả về ngay job_id.

    Mô tả:
        Khởi động tiến trình nền, output được gom vào buffer để đọc dần
        qua status(). Job nền không áp timeout (dùng kill() để dừng).
    Args:
        cmd: chuỗi lệnh hoặc list argv.
        cwd: thư mục chạy lệnh.
        allowed_root: cwd phải nằm trong root này.
    Returns:
        {"ok": True, "job_id": str} hoặc {"ok": False, "error": str}.
    Raises:
        Không raise — mọi lỗi trả về dict.
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
    """Xem trạng thái job nền: đang chạy hay đã xong, kèm output đã gom.

    Args:
        job_id: id do bg() trả về.
    Returns:
        {"ok": True, "job_id", "running": bool, "returncode": int|None,
         "stdout": str, "stderr": str}
        hoặc {"ok": False, "error": "not found"}.
    Raises:
        Không raise — mọi lỗi trả về dict.
    """
    with _JOBS_LOCK:
        entry = _JOBS.get(job_id)
    if entry is None:
        return {"ok": False, "error": "not found"}
    proc = entry["proc"]
    rc = proc.poll()
    if rc is not None:
        _join_drainers(entry)  # đảm bảo đã gom hết output
    return {
        "ok": True,
        "job_id": job_id,
        "running": rc is None,
        "returncode": rc,
        "stdout": b"".join(entry["out"]).decode("utf-8", errors="replace"),
        "stderr": b"".join(entry["err"]).decode("utf-8", errors="replace"),
    }


def kill(job_id: str) -> Dict:
    """Dừng job nền: SIGTERM, sau 5s chưa chết thì SIGKILL.

    Args:
        job_id: id do bg() trả về.
    Returns:
        {"ok": True, "job_id": str} hoặc {"ok": False, "error": "not found"}.
    Raises:
        Không raise — mọi lỗi trả về dict.
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
    """Xóa job đã xong khỏi registry để giải phóng bộ nhớ.

    Args:
        job_id: id do bg() trả về.
    Returns:
        {"ok": True, "job_id": str} hoặc {"ok": False, "error": str}
        (not found / job still running).
    Raises:
        Không raise — mọi lỗi trả về dict.
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
