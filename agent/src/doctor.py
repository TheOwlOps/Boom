"""Boom doctor: environment diagnostics for the Boom repo.

Runnable in two ways from the repo root:
    python3 -m agent.src.doctor
    python3 agent/src/doctor.py

Exits 0 when no check FAILs, 1 when any check FAILs.

Stdlib only.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

# Check statuses.
OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
STATUSES = (OK, WARN, FAIL)

# Ports used by Boom services: Tauri dev server (vite, under `tauri dev`),
# Python API server, TS gateway.
PORTS = {
    5173: "desktop dev server (vite, under tauri dev)",
    18080: "Python API server",
    8788: "TS gateway",
}

RECOMMENDED_PYTHON = (3, 12)
MINIMUM_PYTHON = (3, 11)

REQUIRED_PIP_DEPS = ("httpx", "pydantic")


@dataclass
class Check:
    """Result of a single diagnostic check."""
    name: str
    status: str  # one of OK / WARN / FAIL
    detail: str
    fix: str = ""  # actionable fix, shown for WARN/FAIL


def find_repo_root() -> Optional[Path]:
    """Locate the Boom repo root: dir containing agent/src, gateway and apps/desktop.

    Searches upward from this file's location first, then from the cwd.
    """
    markers = (Path("agent") / "src", Path("gateway"), Path("apps") / "desktop")

    def probe(start: Path) -> Optional[Path]:
        cur = start.resolve()
        for _ in range(12):  # walk up at most 12 levels
            if all((cur / m).is_dir() for m in markers):
                return cur
            parent = cur.parent
            if parent == cur:
                break
            cur = parent
        return None

    found = probe(Path(__file__).parent)
    if found is not None:
        return found
    return probe(Path.cwd())


def boom_config_path() -> Path:
    """Return the expected config file path: $BOOM_HOME/config.json or ~/.boom/config.json."""
    home = Path(os.environ.get("BOOM_HOME", str(Path.home() / ".boom")))
    return home / "config.json"


def _run_version(cmd: List[str]) -> Optional[str]:
    """Run `<cmd> --version`-style command, return first output line or None."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or proc.stderr).strip().splitlines()
    return out[0].strip() if out else None


def _port_in_use(port: int) -> bool:
    """True if something accepts TCP connections on 127.0.0.1:port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


# ---------------------------------------------------------------- checks

def check_python_version() -> Check:
    v = sys.version_info
    cur = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= RECOMMENDED_PYTHON:
        return Check("python_version", OK, f"Python {cur} (>= 3.12 recommended)")
    if (v.major, v.minor) >= MINIMUM_PYTHON:
        return Check(
            "python_version", WARN,
            f"Python {cur} is below the recommended 3.12 (minimum supported: 3.11)",
            fix="Install Python 3.12+, e.g.: pyenv install 3.12.7 && pyenv local 3.12.7",
        )
    return Check(
        "python_version", FAIL,
        f"Python {cur} is too old (minimum supported: 3.11)",
        fix="Install Python 3.12+, e.g.: pyenv install 3.12.7 && pyenv local 3.12.7",
    )


def _check_pip_dep(name: str) -> Check:
    try:
        importlib.import_module(name)
    except ImportError:
        return Check(
            f"pip_dep_{name}", FAIL,
            f"Python package '{name}' is not importable",
            fix=f"pip install {name}   # or from repo root: pip install -e .",
        )
    try:
        version = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        version = "unknown (module found, no dist metadata)"
    return Check(f"pip_dep_{name}", OK, f"Python package '{name}' importable (version {version})")


def check_node() -> Check:
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is None:
        return Check(
            "node", WARN,
            "node not found on PATH (needed to build/run the desktop UI)",
            fix="Install Node.js 20+, e.g.: nvm install 22 && nvm use 22  (https://nodejs.org)",
        )
    node_v = _run_version([node, "--version"]) or "version unknown"
    if npm is None:
        return Check(
            "node", WARN,
            f"node {node_v} found but npm is missing (needed to install web deps)",
            fix="Reinstall Node.js from https://nodejs.org (bundles npm), or: npm install -g npm",
        )
    npm_v = _run_version([npm, "--version"]) or "version unknown"
    return Check("node", OK, f"node {node_v}, npm {npm_v}")


def check_rust_toolchain() -> Check:
    cargo = shutil.which("cargo")
    rustc = shutil.which("rustc")
    if cargo is None or rustc is None:
        return Check(
            "rust_toolchain", WARN,
            "cargo/rustc not found (only needed to rebuild Rust crates; prebuilt binaries are checked separately)",
            fix="Install Rust via rustup: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh",
        )
    cargo_v = _run_version([cargo, "--version"]) or "version unknown"
    rustc_v = _run_version([rustc, "--version"]) or "version unknown"
    return Check("rust_toolchain", OK, f"{cargo_v}; {rustc_v}")


def check_port(port: int, label: str) -> Check:
    if _port_in_use(port):
        return Check(
            f"port_{port}", WARN,
            f"Port {port} ({label}) is IN USE — make sure it is your Boom instance, not a conflict",
            fix=f"Inspect the listener: ss -ltnp | grep :{port}   (or: lsof -i :{port}); "
                f"stop the conflicting process before starting Boom.",
        )
    return Check(f"port_{port}", OK, f"Port {port} ({label}) is free")


def check_config_file() -> Check:
    path = boom_config_path()
    if not path.is_file():
        return Check(
            "config_file", WARN,
            f"Config file not found: {path}",
            fix="Run the setup wizard from the repo root: python3 boom.py setup   (or: boom setup)",
        )
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Check(
            "config_file", WARN,
            f"Config file {path} exists but is not valid JSON: {exc}",
            fix=f"Fix the JSON syntax in {path}, or re-run: python3 boom.py setup",
        )
    return Check("config_file", OK, f"Config file present: {path}")


def check_env_file(repo_root: Optional[Path]) -> Check:
    path = (repo_root or Path.cwd()) / ".env"
    if path.is_file():
        return Check("env_file", OK, f".env present: {path}")
    # .env is optional by design: never FAIL, only WARN.
    return Check(
        "env_file", WARN,
        f"No .env file at {path} (optional)",
        fix=f"Create one if you need local secrets: touch {path}  # then add KEY=value lines",
    )


def check_repo_root() -> Check:
    root = find_repo_root()
    if root is None:
        return Check(
            "repo_root", FAIL,
            "Could not detect the Boom repo root (expected dirs: agent/src, gateway, apps/desktop)",
            fix="cd into your Boom checkout, then re-run: python3 -m agent.src.doctor",
        )
    return Check("repo_root", OK, f"Repo root detected: {root}")


def check_rust_binaries(repo_root: Optional[Path]) -> Check:
    if repo_root is None:
        return Check(
            "rust_binaries", WARN,
            "Repo root unknown, cannot locate target/release/",
            fix="cd into your Boom checkout, then re-run: python3 -m agent.src.doctor",
        )
    built = sorted(
        p.name for p in (repo_root / "target" / "release").glob("boom-*")
        if p.is_file() and not p.name.endswith(".d") and os.access(p, os.X_OK)
    )
    if built:
        return Check("rust_binaries", OK, f"Rust perf binaries built ({len(built)}): {', '.join(built)}")
    return Check(
        "rust_binaries", WARN,
        "No built Rust binaries in target/release/boom-* (perf features will fall back to Python)",
        fix="From the repo root: cargo build --release",
    )


def check_desktop_node_modules(repo_root: Optional[Path]) -> Check:
    if repo_root is None:
        return Check(
            "desktop_node_modules", WARN,
            "Repo root unknown, cannot locate apps/desktop/node_modules/",
            fix="cd into your Boom checkout, then re-run: python3 -m agent.src.doctor",
        )
    nm = repo_root / "apps" / "desktop" / "node_modules"
    if nm.is_dir() and any(nm.iterdir()):
        return Check("desktop_node_modules", OK, f"Desktop UI dependencies installed: {nm}")
    return Check(
        "desktop_node_modules", WARN,
        f"Desktop UI dependencies missing: {nm} not found or empty",
        fix="From the repo root: cd apps/desktop && npm install   # (repo also ships pnpm-lock.yaml: pnpm install works too)",
    )


def _safe(fn: Callable[[], Check]) -> Check:
    """Run a check, converting unexpected exceptions into FAIL results."""
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 — diagnostics must never crash
        return Check(
            getattr(fn, "__name__", "unknown"), FAIL,
            f"Check crashed unexpectedly: {exc!r}",
            fix="Report this as a bug against agent/src/doctor.py",
        )
    if result.status not in STATUSES:
        return Check(result.name, FAIL, f"Check returned invalid status: {result.status!r}")
    return result


def run_checks() -> List[Check]:
    """Run every diagnostic check, in a stable order. Never raises."""
    root_check = _safe(check_repo_root)
    repo_root = find_repo_root() if root_check.status == OK else None
    checks: List[Check] = [
        root_check,
        _safe(check_python_version),
        *[_safe(lambda n=name: _check_pip_dep(n)) for name in REQUIRED_PIP_DEPS],
        _safe(check_node),
        _safe(check_rust_toolchain),
        *[_safe(lambda p=port, l=label: check_port(p, l)) for port, label in PORTS.items()],
        _safe(check_config_file),
        _safe(lambda: check_env_file(repo_root)),
        _safe(lambda: check_rust_binaries(repo_root)),
        _safe(lambda: check_desktop_node_modules(repo_root)),
    ]
    return checks


# ---------------------------------------------------------------- report

def exit_code(checks: List[Check]) -> int:
    """Exit code contract: 0 if no FAIL, 1 if any FAIL."""
    return 1 if any(c.status == FAIL for c in checks) else 0


def render_report(checks: List[Check]) -> str:
    lines: List[str] = []
    lines.append("Boom doctor — environment diagnostics")
    lines.append("")
    name_w = max((len(c.name) for c in checks), default=0)
    for c in checks:
        lines.append(f"[{c.status}] {c.name:<{name_w}}  {c.detail}")
    ok = sum(1 for c in checks if c.status == OK)
    warn = sum(1 for c in checks if c.status == WARN)
    fail = sum(1 for c in checks if c.status == FAIL)
    lines.append("")
    lines.append(f"Summary: {ok} OK, {warn} WARN, {fail} FAIL ({len(checks)} checks)")
    problems = [c for c in checks if c.status in (WARN, FAIL) and c.fix]
    if problems:
        lines.append("")
        lines.append("Suggested fixes:")
        for c in problems:
            lines.append(f"  [{c.status}] {c.name}: {c.detail}")
            lines.append(f"         fix: {c.fix}")
    lines.append("")
    lines.append(f"Exit code: {exit_code(checks)}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    checks = run_checks()
    print(render_report(checks))
    return exit_code(checks)


if __name__ == "__main__":
    sys.exit(main())
