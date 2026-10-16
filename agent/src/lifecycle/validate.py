"""Startup configuration validation for Boom.

Fail-fast checks run once at process startup (called from the top of
``boom.py`` / ``agent.src.cli.main``) so misconfiguration surfaces as a
clear message instead of a cryptic crash mid-run.

Only the standard library is used (os, socket, sqlite3, shutil, subprocess,
sys, tempfile, pathlib, dataclasses).

Usage:
    from agent.src.lifecycle.validate import validate_startup

    report = validate_startup()          # no crash; inspect report
    report.raise_if_errors()             # raise StartupError on errors
    # or in one line:
    validate_startup().raise_if_errors()
"""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "StartupError",
    "CheckResult",
    "ValidationReport",
    "validate_startup",
    "DEFAULT_API_PORT",
    "DEFAULT_INGRESS_PORT",
    "MIN_PYTHON",
    "MIN_DISK_FREE_BYTES",
]

DEFAULT_API_PORT = 18080
DEFAULT_INGRESS_PORT = 8788
MIN_PYTHON = (3, 10)
MIN_DISK_FREE_BYTES = 100 * 1024 * 1024  # 100 MB


class StartupError(Exception):
    """Raised when startup validation finds one or more hard errors."""


@dataclass
class CheckResult:
    name: str
    level: str  # "ok" | "warning" | "error"
    message: str


@dataclass
class ValidationReport:
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(c.level == "error" for c in self.checks)

    @property
    def has_warnings(self) -> bool:
        return any(c.level == "warning" for c in self.checks)

    def errors(self) -> List[CheckResult]:
        return [c for c in self.checks if c.level == "error"]

    def warnings(self) -> List[CheckResult]:
        return [c for c in self.checks if c.level == "warning"]

    def summary(self) -> str:
        """One line per check."""
        lines = []
        icon = {"ok": "✓", "warning": "⚠", "error": "✗"}
        for c in self.checks:
            lines.append(f"{icon.get(c.level, '?')} [{c.level}] {c.name}: {c.message}")
        return "\n".join(lines)

    def raise_if_errors(self) -> "ValidationReport":
        """Raise StartupError with a summary if any check errored.

        NOTE: the header/footer below stay in Vietnamese — pinned by
        evals/test_harden_validate.py ("Khởi động", "lỗi" must be present).
        """
        if not self.has_errors:
            return self
        errs = self.errors()
        lines = ["Khởi động thất bại vì %d lỗi cấu hình nghiêm trọng:" % len(errs)]
        for i, e in enumerate(errs, 1):
            lines.append(f"  {i}. [{e.name}] {e.message}")
        lines.append(
            "Hãy sửa các lỗi trên rồi chạy lại. "
            "Nếu cần bỏ qua tạm thời, kiểm tra lại file cấu hình và biến môi trường."
        )
        raise StartupError("\n".join(lines))


def _cfg_get(config: Any, path: str, default: Any = None) -> Any:
    """Get a dotted attribute/dict path from an optional config object."""
    if config is None:
        return default
    cur: Any = config
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part, default)
        else:
            cur = getattr(cur, part, default)
        if cur is None:
            return default
    return cur


def _check_env_vars(config: Any) -> List[CheckResult]:
    results: List[CheckResult] = []

    telegram_token = os.environ.get("BOOM_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_enabled = bool(_cfg_get(config, "gateway.telegram.enabled", False)) or bool(telegram_token)
    if telegram_enabled and not telegram_token:
        results.append(CheckResult(
            name="env.telegram_token",
            level="warning",
            message=(
                "The Telegram channel is ON but no token is set "
                "(BOOM_TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN). The Telegram bot "
                "will not receive messages. Fix: set the BOOM_TELEGRAM_BOT_TOKEN=<token> "
                "environment variable or disable the Telegram channel in the config."
            ),
        ))
    else:
        results.append(CheckResult(
            name="env.telegram_token", level="ok",
            message="Telegram token OK (or the Telegram channel is off)."))

    # LLM keys: OpenAI / Anthropic / OpenRouter
    has_any_llm_key = any(os.environ.get(v) for v in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"))
    primary_name = str(_cfg_get(config, "primary_provider.name", "") or "").lower()
    primary_key = str(_cfg_get(config, "primary_provider.api_key", "") or "")
    needs_llm = bool(primary_name) or bool(_cfg_get(config, "primary_provider", None) is not None)
    if not has_any_llm_key and not primary_key:
        results.append(CheckResult(
            name="env.llm_api_key",
            level="warning",
            message=(
                "No LLM API key found (OPENAI_API_KEY / ANTHROPIC_API_KEY / "
                "OPENROUTER_API_KEY). Boom still runs locally but every task needing "
                "a language model will fail. Fix: set one of the env vars above, or "
                "configure api_key in ~/.boom/config.json."
            ),
        ))
    else:
        results.append(CheckResult(
            name="env.llm_api_key", level="ok",
            message="LLM provider API key is set."))
    return results


def _check_port(env_name: str, default: int) -> CheckResult:
    raw = os.environ.get(env_name, "")
    try:
        port = int(raw) if raw else default
    except ValueError:
        return CheckResult(
            name=f"port.{env_name.lower()}",
            level="error",
            message=(
                f"{env_name}='{raw}' is not a valid port number. "
                f"Set {env_name} to an integer from 1 to 65535."
            ),
        )
    if not 1 <= port <= 65535:
        return CheckResult(
            name=f"port.{env_name.lower()}",
            level="error",
            message=(
                f"Port {port} ({env_name}) is outside the valid 1-65535 range. "
                f"Set {env_name} within 1-65535."
            ),
        )
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
    except OSError:
        return CheckResult(
            name=f"port.{env_name.lower()}",
            level="error",
            message=(
                f"Port {port} is already taken by another process. "
                f"Stop that process or set a different {env_name}."
            ),
        )
    return CheckResult(
        name=f"port.{env_name.lower()}",
        level="ok",
        message=f"Port {port} is free, ready to bind.",
    )


def _db_path(config: Any) -> Path:
    raw = os.environ.get("BOOM_DB_PATH") or str(_cfg_get(config, "db_path", "") or "")
    if raw:
        return Path(raw).expanduser()
    home = os.environ.get("BOOM_HOME", str(Path.home() / ".boom"))
    return Path(home) / "boom.db"


def _check_db_writable(config: Any) -> CheckResult:
    path = _db_path(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Safe: probes a temp file in the same dir, never touches the real DB.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".boom_dbtest_")
        os.close(fd)
        try:
            conn = sqlite3.connect(tmp)
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS _boom_startup_probe (id INTEGER PRIMARY KEY, v TEXT)")
                conn.execute("INSERT INTO _boom_startup_probe (v) VALUES ('ok')")
                conn.commit()
                row = conn.execute("SELECT v FROM _boom_startup_probe").fetchone()
                if not row or row[0] != "ok":
                    raise OSError("read-back of the probe data failed")
            finally:
                conn.close()
        finally:
            os.unlink(tmp)
    except Exception as exc:
        return CheckResult(
            name="db.writable",
            level="error",
            message=(
                f"Cannot write the database at '{path}': {exc}. "
                f"Fix: check write permission on '{path.parent}' (e.g. chmod u+w), "
                f"or point BOOM_DB_PATH to a writable location."
            ),
        )
    return CheckResult(
        name="db.writable",
        level="ok",
        message=f"Database path '{path}' is writable.",
    )


def _check_tesseract() -> CheckResult:
    bin_path = shutil.which("tesseract")
    if not bin_path:
        return CheckResult(
            name="tesseract",
            level="warning",
            message=(
                "tesseract not found on PATH — image OCR will not work. "
                "Fix: install tesseract-ocr (e.g. sudo apt install tesseract-ocr "
                "tesseract-ocr-vie) and rerun."
            ),
        )
    try:
        proc = subprocess.run(
            [bin_path, "--version"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return CheckResult(
            name="tesseract",
            level="warning",
            message=(
                f"Could not run 'tesseract --version': {exc}. "
                "Image OCR may not work; try reinstalling tesseract-ocr."
            ),
        )
    version_line = (proc.stdout or proc.stderr or "").strip().splitlines()
    version = version_line[0] if version_line else "unknown version"
    # Check for Vietnamese language data
    tessdata_prefix = os.environ.get("TESSDATA_PREFIX", "")
    vie_found = False
    search_dirs = []
    if tessdata_prefix:
        search_dirs.append(Path(tessdata_prefix))
    # Common locations on Debian/Ubuntu
    search_dirs += [Path("/usr/share/tesseract-ocr/4.00/tessdata"),
                    Path("/usr/share/tesseract-ocr/5/tessdata"),
                    Path("/usr/share/tessdata")]
    for d in search_dirs:
        if (d / "vie.traineddata").exists():
            vie_found = True
            break
    if not vie_found:
        return CheckResult(
            name="tesseract",
            level="warning",
            message=(
                f"Tesseract is installed ({version}) but missing Vietnamese language "
                "data (vie.traineddata) — Vietnamese OCR will be poor or broken. "
                "Fix: sudo apt install tesseract-ocr-vie."
            ),
        )
    return CheckResult(
        name="tesseract",
        level="ok",
        message=f"Tesseract OK ({version}), Vietnamese data present.",
    )


def _check_python_version() -> CheckResult:
    if sys.version_info < MIN_PYTHON:
        req = ".".join(map(str, MIN_PYTHON))
        cur = ".".join(map(str, sys.version_info[:3]))
        return CheckResult(
            name="runtime.python_version",
            level="error",
            message=(
                f"Current Python is {cur}, Boom requires Python >= {req}. "
                f"Upgrade Python (Python 3.12 recommended) and rerun."
            ),
        )
    cur = ".".join(map(str, sys.version_info[:3]))
    return CheckResult(
        name="runtime.python_version",
        level="ok",
        message=f"Python {cur} meets the requirement (>= 3.10).",
    )


def _check_disk_free() -> CheckResult:
    try:
        usage = shutil.disk_usage(str(Path.home()))
    except OSError as exc:
        return CheckResult(
            name="runtime.disk_free",
            level="warning",
            message=(
                f"Cannot read disk usage: {exc}. "
                "Manually verify at least 100MB is free."
            ),
        )
    if usage.free < MIN_DISK_FREE_BYTES:
        free_mb = usage.free / (1024 * 1024)
        return CheckResult(
            name="runtime.disk_free",
            level="error",
            message=(
                f"Only {free_mb:.1f}MB free on disk, below the 100MB minimum. "
                "Boom needs space for its database and logs — free up disk space and rerun."
            ),
        )
    free_mb = usage.free / (1024 * 1024)
    return CheckResult(
        name="runtime.disk_free",
        level="ok",
        message=f"{free_mb:.0f}MB free on disk (>= 100MB).",
    )


def validate_startup(config: Any = None) -> ValidationReport:
    """Run all startup checks and return a ValidationReport (never raises).

    Pass a BoomConfig (or dict) to let env-var checks consider channel/LLM
    settings; when ``config`` is None the checks degrade to environment-only
    heuristics. Hard failures are reported as level="error"; call
    ``report.raise_if_errors()`` to fail fast with a StartupError.
    """
    report = ValidationReport()
    report.checks.extend(_check_env_vars(config))
    report.checks.append(_check_port("BOOM_API_PORT", DEFAULT_API_PORT))
    report.checks.append(_check_port("BOOM_INGRESS_PORT", DEFAULT_INGRESS_PORT))
    report.checks.append(_check_db_writable(config))
    report.checks.append(_check_tesseract())
    report.checks.append(_check_python_version())
    report.checks.append(_check_disk_free())
    return report
