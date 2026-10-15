"""Test cho gateway_health_patch.detailed_health(). Stdlib only (unittest).

Chay:  python3 test_harden_health.py        (tu thu muc chua file nay)
       python3 -m unittest test_harden_health -v
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gateway_health_patch as ghp


class TestDetailedHealth(unittest.TestCase):
    def test_structure_keys(self):
        """Response phai co du keys, moi check co status."""
        r = ghp.detailed_health()
        for key in ("status", "uptime_seconds", "version", "checks"):
            self.assertIn(key, r, "missing key: %s" % key)
        self.assertIn(r["status"], ("ok", "degraded"))
        self.assertIsInstance(r["uptime_seconds"], (int, float))
        self.assertIsInstance(r["version"], str)
        for name in ("database", "telegram", "disk", "memory"):
            self.assertIn(name, r["checks"], "missing check: %s" % name)
            self.assertIn("status", r["checks"][name])
            self.assertIn("detail", r["checks"][name])
            self.assertIn(r["checks"][name]["status"], ("ok", "fail"))

    def test_forced_fail_becomes_degraded_no_raise(self):
        """Ep 1 check fail (monkeypatch) -> tong 'degraded', khong raise."""
        orig = ghp._CHECKS["database"]
        ghp._CHECKS["database"] = lambda: {"status": "fail", "detail": "forced"}
        try:
            r = ghp.detailed_health()  # phai khong raise
        finally:
            ghp._CHECKS["database"] = orig
        self.assertEqual(r["status"], "degraded")
        self.assertEqual(r["checks"]["database"]["status"], "fail")

    def test_all_forced_ok_gives_ok(self):
        """Tat ca check ok -> tong 'ok'."""
        orig = dict(ghp._CHECKS)
        for name in ghp._CHECKS:
            ghp._CHECKS[name] = lambda: {"status": "ok", "detail": "forced ok"}
        try:
            r = ghp.detailed_health()
        finally:
            ghp._CHECKS.update(orig)
        self.assertEqual(r["status"], "ok")

    def test_check_that_raises_is_captured(self):
        """Ham check tu raise cung khong lam sup detailed_health()."""
        orig = ghp._CHECKS["memory"]
        def boom():
            raise RuntimeError("simulated crash")
        ghp._CHECKS["memory"] = boom
        try:
            r = ghp.detailed_health()
        finally:
            ghp._CHECKS["memory"] = orig
        self.assertEqual(r["status"], "degraded")
        self.assertEqual(r["checks"]["memory"]["status"], "fail")
        self.assertIn("RuntimeError", r["checks"]["memory"]["detail"])

    def test_uptime_nonneg_and_increases(self):
        r1 = ghp.detailed_health()
        time.sleep(0.05)
        r2 = ghp.detailed_health()
        self.assertGreaterEqual(r1["uptime_seconds"], 0)
        self.assertGreater(r2["uptime_seconds"], r1["uptime_seconds"])

    def test_version_fallback(self):
        """_get_version() co raise cung khong lam sup, version ve '0.1.0'."""
        orig = ghp._get_version

        def boom():
            raise RuntimeError("nope")

        ghp._get_version = boom
        try:
            r = ghp.detailed_health()
        finally:
            ghp._get_version = orig
        self.assertEqual(r["version"], "0.1.0")
        # Ban goc: import that bai -> fallback (test chay standalone,
        # khong co package gateway tren path).
        self.assertEqual(ghp._get_version(), "0.1.0")

    def test_individual_checks_never_raise(self):
        """Goi truc tiep tung check, khong duoc raise, phai tra dict co status."""
        for name, fn in (
            ("database", ghp._check_database),
            ("telegram", ghp._check_telegram),
            ("disk", ghp._check_disk),
            ("memory", ghp._check_memory),
        ):
            r = fn()  # raise o day la FAIL test
            self.assertIsInstance(r, dict, name)
            self.assertIn(r.get("status"), ("ok", "fail"), name)

    def test_disk_memory_numeric_fields(self):
        r = ghp.detailed_health()
        disk, mem = r["checks"]["disk"], r["checks"]["memory"]
        for check, field in ((disk, "free_mb"), (mem, "available_mb")):
            if check["status"] == "ok":
                self.assertIsInstance(check[field], (int, float))
                self.assertGreaterEqual(check[field], 0)

    def test_telegram_does_not_leak_token(self):
        """detail cua telegram khong bao gio chua gia tri token."""
        os.environ["TELEGRAM_BOT_TOKEN"] = "secret-token-xyz"
        try:
            r = ghp._check_telegram()
        finally:
            del os.environ["TELEGRAM_BOT_TOKEN"]
        self.assertEqual(r["status"], "ok")
        self.assertNotIn("secret-token-xyz", r["detail"])
        r2 = ghp._check_telegram()
        self.assertEqual(r2["status"], "fail")


if __name__ == "__main__":
    unittest.main(verbosity=2)
