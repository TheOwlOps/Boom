#!/usr/bin/env python3
"""Benchmark hieu nang cac tool groups cua Boom (TheOwlOps/Boom).

- Do bang time.perf_counter() (monotonic, do phan giai cao).
- CHI dung Python stdlib (time, subprocess, json, ast, importlib, ...).
- Repo Boom read-only tai /tmp/boom_git, KHONG sua repo.
- Chay:  python3 bench_tools.py
"""
import ast
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ----------------------------------------------------------------------------
# Duong dan repo (read-only). environment.py tu them repo root vao sys.path.
# ----------------------------------------------------------------------------
REPO_SRC = Path("/tmp/boom_git/agent/src")
REPO_ROOT = Path("/tmp/boom_git")
for _p in (str(REPO_SRC), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Cac tool package chinh trong Boom (tu agent/src/tools/).
TOOL_PACKAGES = [
    "tools.filesystem",
    "tools.git",
    "tools.web",
    "tools.security",
    "tools.db",
    "tools.terminal",
    "tools.code",
]

RESULT_KEYS = ("name", "runs", "total_s", "avg_s", "avg_ms", "ops_per_s")


def run_bench(name, fn, n, warmup=2):
    """Chay fn n lan, tra ve dict ket qua.

    warmup: so lan chay "lam nong" truoc (khong tinh) de on dinh CPU/cache.
    """
    for _ in range(warmup):
        fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    total = time.perf_counter() - t0
    avg = total / n if n else 0.0
    return {
        "name": name,
        "runs": n,
        "total_s": total,
        "avg_s": avg,
        "avg_ms": avg * 1000.0,
        "ops_per_s": n / total if total > 0 else 0.0,
    }


def print_table(results):
    """In bang ket qua dang text don gian ra stdout."""
    header = f"{'benchmark':42s} {'runs':>8s} {'total(s)':>12s} {'avg/op':>12s} {'ops/sec':>14s}"
    print(header)
    print("-" * len(header))
    for r in results:
        avg = r["avg_s"]
        if avg < 0.001:
            avg_str = f"{avg * 1e6:>10.2f} us"
        elif avg < 1.0:
            avg_str = f"{avg * 1e3:>10.2f} ms"
        else:
            avg_str = f"{avg:>11.3f} s"
        print(
            f"{r['name']:42s} {r['runs']:>8d} {r['total_s']:>12.3f} "
            f"{avg_str} {r['ops_per_s']:>14.1f}"
        )


# ----------------------------------------------------------------------------
# (a) Cold import time tung tool package: chay trong subprocess rieng de do
#     thoi gian import "lanh" that (bao gom ca interpreter startup).
# ----------------------------------------------------------------------------
def bench_import(package, n=20):
    def _once():
        code = (
            "import sys; sys.path.insert(0, r'%s'); sys.path.insert(0, r'%s'); import %s"
            % (REPO_SRC, REPO_ROOT, package)
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "import %s that bai: %s" % (package, proc.stderr[-500:])
            )

    return run_bench("import/" + package, _once, n)


# ----------------------------------------------------------------------------
# (b) Khoi tao AgentEnvironment.
# ----------------------------------------------------------------------------
def _make_env():
    import environment  # import o day de tinh ca import vao lan dau

    tmp = tempfile.mkdtemp(prefix="boom_bench_")
    return environment.AgentEnvironment(root_dir=Path(tmp))


def bench_env_init(n=10):
    """Khoi tao AgentEnvironment hoan chinh (SQLite, skills, registry...)."""
    return run_bench("env/AgentEnvironment.__init__", _make_env, n)


def bench_init_tools_only(n=50):
    """Rieng buoc dang ky ~159 tools (env._init_tools)."""
    import environment

    tmp = tempfile.mkdtemp(prefix="boom_bench_")
    env = environment.AgentEnvironment(root_dir=Path(tmp))

    def _once():
        env._init_tools()

    return run_bench("env/_init_tools (159 tools)", _once, n)


# ----------------------------------------------------------------------------
# (c) Tool call don gian: cac ham pure / gan-pure.
# ----------------------------------------------------------------------------
def bench_sanitize(n=5000):
    from security.sanitize import sanitize_shell_arg

    def _once():
        sanitize_shell_arg("hello; rm -rf / --no-preserve-root")

    return run_bench("tool/sanitize_shell_arg", _once, n)


def bench_validate_path(n=5000):
    from security.sanitize import validate_path

    def _once():
        validate_path("/tmp/boom_bench/data/x.txt", ["/tmp/boom_bench"])

    return run_bench("tool/validate_path", _once, n)


def bench_validate_url(n=5000):
    from tools.web.ssrf import validate_url

    # Dung IP-literal de khong phu thuoc DNS (DNS cua sandbox co the
    # resolve hostname ve dai IP noi bo va bi SSRF guard chan).
    def _once():
        validate_url("https://93.184.216.34/api/v1/search?q=boom")

    return run_bench("tool/ssrf.validate_url", _once, n)


def bench_scan_file(n=100):
    from security.secretscan import scan_file

    # File mau ~200 dong, lan 1 dong chua "api key" gia de scanner co viec.
    sample = Path(tempfile.mkdtemp(prefix="boom_bench_")) / "sample.py"
    lines = ["x_%d = %d  # dong binh thuong\n" % (i, i) for i in range(199)]
    lines.append('GITHUB_TOKEN = "ghp_fake1234567890abcdef"\n')
    sample.write_text("".join(lines), encoding="utf-8")

    def _once():
        scan_file(sample)

    return run_bench("tool/secretscan.scan_file (200 dong)", _once, n)


def bench_read_file(n=200):
    from tools.file_ops import read_file

    sample = Path(tempfile.mkdtemp(prefix="boom_bench_")) / "big.txt"
    sample.write_text("".join("dong %d\n" % i for i in range(2000)), encoding="utf-8")

    def _once():
        read_file(str(sample))

    return run_bench("tool/file_ops.read_file (2000 dong)", _once, n)


# ----------------------------------------------------------------------------
# (d) JSON serialize/deserialize toan bo tool schemas (159 tools).
# ----------------------------------------------------------------------------
def bench_schema_json(n=300):
    import environment

    tmp = tempfile.mkdtemp(prefix="boom_bench_")
    env = environment.AgentEnvironment(root_dir=Path(tmp))
    schemas = {name: t["schema"] for name, t in env._tools.items()}

    def _once():
        s = json.dumps(schemas, ensure_ascii=False)
        json.loads(s)

    return run_bench(
        "json/schemas %d tools round-trip" % len(schemas), _once, n
    )


# ----------------------------------------------------------------------------
# (e) AST parse 1 file source vua phai (~45KB: environment.py).
# ----------------------------------------------------------------------------
def bench_ast_parse(n=100):
    source = (REPO_SRC / "environment.py").read_text(encoding="utf-8")

    def _once():
        ast.parse(source)

    return run_bench("ast/parse environment.py (45KB)", _once, n)


# ----------------------------------------------------------------------------
# Chay tat ca.
# ----------------------------------------------------------------------------
def run_all():
    results = []
    for pkg in TOOL_PACKAGES:
        results.append(bench_import(pkg))
    results.append(bench_import("environment", n=10))
    results.append(bench_env_init())
    results.append(bench_init_tools_only())
    results.append(bench_sanitize())
    results.append(bench_validate_path())
    results.append(bench_validate_url())
    results.append(bench_scan_file())
    results.append(bench_read_file())
    results.append(bench_schema_json())
    results.append(bench_ast_parse())
    return results


def main():
    results = run_all()
    print_table(results)
    return results


if __name__ == "__main__":
    main()
