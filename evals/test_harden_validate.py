"""Tests for agent/src/lifecycle/validate.py (config-validator harden module).

Run:  python3 test_harden_validate.py
"""

import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Import root is agent/src (repo convention); the modules under test live
# in subpackages there (they previously lived next to this file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from lifecycle.validate import (  # noqa: E402
    CheckResult,
    StartupError,
    ValidationReport,
    validate_startup,
)


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestPortOccupied(unittest.TestCase):
    def test_port_occupied_reports_error(self):
        port = _free_port()
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", port))
        holder.listen(1)
        try:
            with mock.patch.dict(os.environ, {"BOOM_API_PORT": str(port),
                                              "BOOM_INGRESS_PORT": str(_free_port())}):
                report = validate_startup()
        finally:
            holder.close()
        api_checks = [c for c in report.checks if c.name == "port.boom_api_port"]
        self.assertEqual(len(api_checks), 1)
        self.assertEqual(api_checks[0].level, "error")
        self.assertIn(str(port), api_checks[0].message)
        self.assertTrue(report.has_errors)

    def test_invalid_port_value_reports_error(self):
        with mock.patch.dict(os.environ, {"BOOM_API_PORT": "notaport"}):
            report = validate_startup()
        api_checks = [c for c in report.checks if c.name == "port.boom_api_port"]
        self.assertEqual(api_checks[0].level, "error")


class TestDbWritable(unittest.TestCase):
    def test_unwritable_db_path_reports_error(self):
        # /proc is not writable -> any write test there must fail
        with mock.patch.dict(os.environ, {"BOOM_DB_PATH": "/proc/boom_test_xyz/boom.db"}):
            report = validate_startup()
        db_checks = [c for c in report.checks if c.name == "db.writable"]
        self.assertEqual(len(db_checks), 1)
        self.assertEqual(db_checks[0].level, "error")
        self.assertTrue(report.has_errors)

    def test_writable_db_path_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"BOOM_DB_PATH": os.path.join(tmp, "boom.db")}):
                report = validate_startup()
        db_checks = [c for c in report.checks if c.name == "db.writable"]
        self.assertEqual(db_checks[0].level, "ok")


class TestEnvVars(unittest.TestCase):
    def test_missing_keys_are_warnings_not_errors(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
                            "BOOM_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN")}
        with mock.patch.dict(os.environ, env, clear=True):
            report = validate_startup()
        llm = [c for c in report.checks if c.name == "env.llm_api_key"][0]
        tg = [c for c in report.checks if c.name == "env.telegram_token"][0]
        self.assertEqual(llm.level, "warning")
        self.assertEqual(tg.level, "ok")  # channel off + no token -> ok, not warning
        self.assertFalse(any(c.level == "error" for c in (llm, tg)))

    def test_telegram_enabled_without_token_warns(self):
        config = {"gateway": {"telegram": {"enabled": True}}}
        env = {k: v for k, v in os.environ.items()
               if k not in ("BOOM_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN")}
        with mock.patch.dict(os.environ, env, clear=True):
            report = validate_startup(config=config)
        tg = [c for c in report.checks if c.name == "env.telegram_token"][0]
        self.assertEqual(tg.level, "warning")
        self.assertIn("Telegram", tg.message)


class TestRaiseIfErrors(unittest.TestCase):
    def test_raise_if_errors_raises_startup_error_vietnamese(self):
        report = ValidationReport(checks=[
            CheckResult(name="port.boom_api_port", level="error",
                        message="Port 18080 đang bị chiếm bởi tiến trình khác."),
        ])
        with self.assertRaises(StartupError) as ctx:
            report.raise_if_errors()
        msg = str(ctx.exception)
        # Vietnamese keywords must be present
        for word in ("Khởi động", "lỗi"):
            self.assertIn(word, msg)

    def test_raise_if_errors_no_errors_returns_self(self):
        report = ValidationReport(checks=[
            CheckResult(name="x", level="ok", message="ok"),
            CheckResult(name="y", level="warning", message="warn"),
        ])
        self.assertIs(report.raise_if_errors(), report)
        self.assertFalse(report.has_errors)
        self.assertTrue(report.has_warnings)

    def test_validate_startup_with_occupied_port_raises(self):
        port = _free_port()
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", port))
        holder.listen(1)
        try:
            with mock.patch.dict(os.environ, {"BOOM_API_PORT": str(port)}):
                report = validate_startup()
                with self.assertRaises(StartupError):
                    report.raise_if_errors()
        finally:
            holder.close()


class TestHappyPath(unittest.TestCase):
    def test_current_environment_does_not_crash(self):
        # Uses the real environment; must at least not crash and return a report.
        report = validate_startup()
        self.assertIsInstance(report, ValidationReport)
        self.assertGreaterEqual(len(report.checks), 7)
        names = {c.name for c in report.checks}
        for expected in ("env.telegram_token", "env.llm_api_key",
                         "port.boom_api_port", "port.boom_ingress_port",
                         "db.writable", "tesseract",
                         "runtime.python_version", "runtime.disk_free"):
            self.assertIn(expected, names)
        for c in report.checks:
            self.assertIn(c.level, ("ok", "warning", "error"))
            self.assertTrue(c.message)
        # Summary should be printable without crashing
        self.assertTrue(report.summary())

    def test_tesseract_check_reports_something_sane(self):
        report = validate_startup()
        tess = [c for c in report.checks if c.name == "tesseract"][0]
        self.assertIn(tess.level, ("ok", "warning"))  # never error

    def test_python_version_ok_on_this_machine(self):
        report = validate_startup()
        py = [c for c in report.checks if c.name == "runtime.python_version"][0]
        self.assertEqual(py.level, "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
