"""Gateway health patch: detailed health report for the Boom gateway.

``detailed_health()`` returns a JSON-serializable dict::

    {"status": "ok" | "degraded",
     "uptime_seconds": <float>,
     "version": <str>,
     "checks": {"database": {...}, "telegram": {...},
                "disk": {...}, "memory": {...}}}

Every individual check never raises: a failing check (or a check that
itself crashes) is reported as ``{"status": "fail", "detail": ...}`` and
the overall status becomes "degraded". The ``_CHECKS`` registry is a plain
dict so tests can monkeypatch individual checks.
"""

import os
import shutil
import time

_START = time.monotonic()

_FALLBACK_VERSION = "0.1.0"


def _get_version() -> str:
    """Best-effort gateway version string; "0.1.0" when undeterminable."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("boom-gateway")
        except PackageNotFoundError:
            pass
        for candidate in ("VERSION", "version.txt"):
            try:
                with open(candidate, encoding="utf-8") as f:
                    v = f.read().strip()
                if v:
                    return v
            except OSError:
                continue
    except Exception:
        pass
    return _FALLBACK_VERSION


def _check_database():
    """SQLite driver works and the state-db file exists."""
    try:
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.execute("SELECT 1")
        con.close()
    except Exception as e:  # noqa: BLE001 - reported, never raised
        return {"status": "fail",
                "detail": "sqlite driver broken: %s: %s" % (type(e).__name__, e)}
    db = os.environ.get("BOOM_DB", "state.db")
    if os.path.exists(db):
        return {"status": "ok", "detail": "db file present (%s)" % db}
    return {"status": "fail", "detail": "db file missing (%s)" % db}


def _check_telegram():
    """Telegram bot token is configured (value never echoed back)."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if tok:
        return {"status": "ok",
                "detail": "TELEGRAM_BOT_TOKEN is set (%d chars)" % len(tok)}
    return {"status": "fail", "detail": "TELEGRAM_BOT_TOKEN is not set"}


def _check_disk():
    """Disk has free space; reports free_mb."""
    try:
        _total, _used, free = shutil.disk_usage(
            os.environ.get("BOOM_DISK_PATH", os.getcwd()))
        free_mb = free / (1024 * 1024)
        return {"status": "ok",
                "detail": "%.1f MB free" % free_mb,
                "free_mb": free_mb}
    except Exception as e:  # noqa: BLE001 - reported, never raised
        return {"status": "fail",
                "detail": "%s: %s" % (type(e).__name__, e)}


def _check_memory():
    """System memory is readable; reports available_mb (Linux /proc)."""
    try:
        available_kb = None
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    available_kb = int(line.split()[1])
                    break
        if available_kb is None:
            raise RuntimeError("MemAvailable not found in /proc/meminfo")
        available_mb = available_kb / 1024
        return {"status": "ok",
                "detail": "%.1f MB available" % available_mb,
                "available_mb": available_mb}
    except Exception as e:  # noqa: BLE001 - reported, never raised
        return {"status": "fail",
                "detail": "%s: %s" % (type(e).__name__, e)}


_CHECKS = {
    "database": _check_database,
    "telegram": _check_telegram,
    "disk": _check_disk,
    "memory": _check_memory,
}


def detailed_health():
    """Run every check; never raises. Overall status is "degraded" when any
    check fails."""
    checks = {}
    for name, fn in _CHECKS.items():
        try:
            r = fn()
            if not isinstance(r, dict) or r.get("status") not in ("ok", "fail"):
                r = {"status": "fail", "detail": "check returned an invalid result"}
        except Exception as e:  # noqa: BLE001 - captured into the report
            r = {"status": "fail",
                 "detail": "%s: %s" % (type(e).__name__, e)}
        checks[name] = r
    try:
        version = _get_version()
    except Exception:  # noqa: BLE001 - fall back, never raise
        version = _FALLBACK_VERSION
    status = "ok" if all(c["status"] == "ok" for c in checks.values()) else "degraded"
    return {
        "status": status,
        "uptime_seconds": time.monotonic() - _START,
        "version": version,
        "checks": checks,
    }
