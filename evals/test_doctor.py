"""Tests for agent/src/doctor.py.

These tests assert the *structure* of the doctor report and the exit-code
contract (0 when no FAIL, 1 when any FAIL). They deliberately do NOT assert
environment-specific outcomes (e.g. that node is installed), because the
environment running the tests is not guaranteed to match any particular setup.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Import the doctor module under test: <staging>/agent/src/doctor.py
STAGING_ROOT = Path(__file__).resolve().parents[1]
DOCTOR_PATH = STAGING_ROOT / "agent" / "src" / "doctor.py"
sys.path.insert(0, str(DOCTOR_PATH.parent))

import doctor  # noqa: E402  (import after sys.path fixup)


EXPECTED_CHECK_NAMES = {
    "repo_root",
    "python_version",
    "pip_dep_httpx",
    "pip_dep_pydantic",
    "node",
    "rust_toolchain",
    "port_5173",
    "port_18080",
    "port_8788",
    "config_file",
    "env_file",
    "rust_binaries",
    "desktop_node_modules",
}


def test_run_checks_structure():
    checks = doctor.run_checks()
    assert isinstance(checks, list) and checks, "run_checks() must return a non-empty list"
    for c in checks:
        assert isinstance(c.name, str) and c.name
        assert c.status in ("OK", "WARN", "FAIL"), f"bad status on {c.name}: {c.status}"
        assert isinstance(c.detail, str) and c.detail
        assert isinstance(c.fix, str)


def test_expected_checks_present():
    names = {c.name for c in doctor.run_checks()}
    assert EXPECTED_CHECK_NAMES <= names, f"missing checks: {EXPECTED_CHECK_NAMES - names}"


def test_exit_code_contract_real_run():
    checks = doctor.run_checks()
    code = doctor.exit_code(checks)
    assert code in (0, 1)
    assert (code == 1) == any(c.status == "FAIL" for c in checks)


def test_exit_code_contract_synthetic():
    ok = [doctor.Check("a", "OK", "fine"), doctor.Check("b", "WARN", "meh", fix="do x")]
    assert doctor.exit_code(ok) == 0
    bad = ok + [doctor.Check("c", "FAIL", "broken", fix="do y")]
    assert doctor.exit_code(bad) == 1


def test_render_report_structure():
    checks = doctor.run_checks()
    report = doctor.render_report(checks)
    assert "Summary" in report
    assert "Exit code:" in report
    for c in checks:
        assert c.name in report
        assert f"[{c.status}]" in report
    # Summary line carries the counts; recompute independently.
    ok = sum(1 for c in checks if c.status == "OK")
    warn = sum(1 for c in checks if c.status == "WARN")
    fail = sum(1 for c in checks if c.status == "FAIL")
    assert f"Summary: {ok} OK, {warn} WARN, {fail} FAIL" in report


def test_main_returns_int_and_prints(capsys):
    code = doctor.main([])
    assert isinstance(code, int) and code in (0, 1)
    out = capsys.readouterr().out
    assert "Summary" in out


def test_script_runs_directly_from_real_repo():
    """Run `python3 agent/src/doctor.py` with the real Boom repo as cwd.

    The doctor module itself is executed from its staging copy; only the
    working directory points at the real repo (read-only use).
    """
    repo = Path("/tmp/boom_git")
    if not (repo / "agent" / "src").is_dir():
        pytest.skip("reference repo /tmp/boom_git not available")
    proc = subprocess.run(
        [sys.executable, str(DOCTOR_PATH)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode in (0, 1), f"unexpected exit {proc.returncode}: {proc.stderr}"
    assert "Traceback" not in proc.stderr, proc.stderr
    assert "Summary" in proc.stdout
    # repo_root must resolve to the real repo when cwd is the repo root.
    assert str(repo) in proc.stdout


def test_module_mode_in_fake_repo(tmp_path):
    """Run `python3 -m agent.src.doctor` inside a minimal fake repo tree."""
    root = tmp_path / "fakerepo"
    src = root / "agent" / "src"
    src.mkdir(parents=True)
    (root / "agent" / "__init__.py").write_text("")
    (src / "__init__.py").write_text("")
    shutil.copy(DOCTOR_PATH, src / "doctor.py")
    (root / "gateway").mkdir()
    (root / "apps" / "web").mkdir(parents=True)

    proc = subprocess.run(
        [sys.executable, "-m", "agent.src.doctor"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode in (0, 1), f"unexpected exit {proc.returncode}: {proc.stderr}"
    assert "Traceback" not in proc.stderr, proc.stderr
    assert "Summary" in proc.stdout
    assert f"Repo root detected: {root}" in proc.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
