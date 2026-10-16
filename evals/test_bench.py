#!/usr/bin/env python3
"""Test cho bench_tools.py — chay that, chi dung Python stdlib.

Chay:  python3 test_bench.py   (hoac: python3 -m unittest test_bench -v)
"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# khi chạy từ repo root: benchmarks/ nằm ở ../benchmarks so với evals/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

import bench_tools


class TestBenchAPI(unittest.TestCase):
    """Verify khung run_bench / print_table."""

    def test_run_bench_tra_ve_dict_du_keys(self):  # 1
        r = bench_tools.run_bench("demo", lambda: None, 10, warmup=0)
        self.assertIsInstance(r, dict)

    def test_run_bench_ten_duoc_giu_nguyen(self):  # 2
        r = bench_tools.run_bench("ten_x", lambda: None, 5, warmup=0)
        self.assertEqual(r["name"], "ten_x")

    def test_run_bench_runs_khop_n(self):  # 3
        r = bench_tools.run_bench("demo", lambda: None, 7, warmup=0)
        self.assertEqual(r["runs"], 7)

    def test_run_bench_total_s_la_so_duong(self):  # 4
        r = bench_tools.run_bench("demo", lambda: None, 10, warmup=0)
        self.assertGreaterEqual(r["total_s"], 0.0)

    def test_run_bench_avg_s_la_so_duong(self):  # 5
        r = bench_tools.run_bench("demo", lambda: sum(range(100)), 50, warmup=0)
        self.assertGreater(r["avg_s"], 0.0)

    def test_run_bench_avg_ms_nhat_quan_voi_avg_s(self):  # 6
        r = bench_tools.run_bench("demo", lambda: sum(range(100)), 50, warmup=0)
        self.assertAlmostEqual(r["avg_ms"], r["avg_s"] * 1000.0)

    def test_run_bench_ops_per_s_nhat_quan(self):  # 7
        r = bench_tools.run_bench("demo", lambda: sum(range(100)), 50, warmup=0)
        self.assertGreater(r["ops_per_s"], 0.0)
        self.assertAlmostEqual(r["ops_per_s"], r["runs"] / r["total_s"])

    def test_print_table_chua_header(self):  # 8
        buf = io.StringIO()
        with redirect_stdout(buf):
            bench_tools.print_table(
                [{"name": "x", "runs": 1, "total_s": 0.1,
                  "avg_s": 0.1, "avg_ms": 100.0, "ops_per_s": 10.0}]
            )
        out = buf.getvalue()
        self.assertIn("benchmark", out)

    def test_print_table_chua_ten_va_so(self):  # 9+10
        buf = io.StringIO()
        with redirect_stdout(buf):
            bench_tools.print_table(
                [{"name": "ten_bench_abc", "runs": 42, "total_s": 0.5,
                  "avg_s": 0.0119, "avg_ms": 11.9, "ops_per_s": 84.0}]
            )
        out = buf.getvalue()
        self.assertIn("ten_bench_abc", out)  # 9
        self.assertIn("42", out)  # 10


class TestImportBench(unittest.TestCase):
    """Verify benchmark import package chay duoc va do duoc so duong."""

    def test_import_filesystem_chay_duoc(self):  # 11
        r = bench_tools.bench_import("tools.filesystem", n=3)
        self.assertGreater(r["total_s"], 0.0)

    def test_import_git_chay_duoc(self):  # 12
        r = bench_tools.bench_import("tools.git", n=3)
        self.assertGreater(r["ops_per_s"], 0.0)

    def test_import_web_chay_duoc(self):  # 13
        r = bench_tools.bench_import("tools.web", n=3)
        self.assertGreater(r["avg_s"], 0.0)

    def test_import_security_chay_duoc(self):  # 14
        r = bench_tools.bench_import("tools.security", n=3)
        self.assertGreater(r["avg_ms"], 0.0)

    def test_import_db_chay_duoc(self):  # 15
        r = bench_tools.bench_import("tools.db", n=3)
        self.assertGreater(r["total_s"], 0.0)

    def test_import_terminal_chay_duoc(self):  # 16
        r = bench_tools.bench_import("tools.terminal", n=3)
        self.assertGreater(r["ops_per_s"], 0.0)

    def test_import_code_chay_duoc(self):  # 17
        r = bench_tools.bench_import("tools.code", n=3)
        self.assertGreater(r["avg_s"], 0.0)

    def test_import_environment_chay_duoc(self):  # 18
        r = bench_tools.bench_import("environment", n=3)
        self.assertGreater(r["total_s"], 0.0)


class TestEnvBench(unittest.TestCase):
    """Verify benchmark khoi tao environment."""

    def test_env_init_chay_duoc(self):  # 19
        r = bench_tools.bench_env_init(n=2)
        self.assertGreater(r["total_s"], 0.0)

    def test_env_init_ten_xuat_hien_trong_bang(self):  # 20
        r = bench_tools.bench_env_init(n=2)
        buf = io.StringIO()
        with redirect_stdout(buf):
            bench_tools.print_table([r])
        self.assertIn("AgentEnvironment", buf.getvalue())

    def test_init_tools_only_chay_duoc(self):  # 21
        r = bench_tools.bench_init_tools_only(n=5)
        self.assertGreater(r["ops_per_s"], 0.0)

    def test_init_tools_only_ten_xuat_hien_trong_bang(self):  # 22
        r = bench_tools.bench_init_tools_only(n=5)
        buf = io.StringIO()
        with redirect_stdout(buf):
            bench_tools.print_table([r])
        self.assertIn("_init_tools", buf.getvalue())


class TestToolCallBench(unittest.TestCase):
    """Verify benchmark tool call don gian."""

    def test_sanitize(self):  # 23
        r = bench_tools.bench_sanitize(n=100)
        self.assertGreater(r["ops_per_s"], 0.0)

    def test_validate_path(self):  # 24
        r = bench_tools.bench_validate_path(n=100)
        self.assertGreater(r["avg_s"], 0.0)

    def test_validate_url(self):  # 25
        r = bench_tools.bench_validate_url(n=100)
        self.assertGreater(r["total_s"], 0.0)

    def test_scan_file(self):  # 26
        r = bench_tools.bench_scan_file(n=5)
        self.assertGreater(r["ops_per_s"], 0.0)

    def test_read_file(self):  # 27
        r = bench_tools.bench_read_file(n=10)
        self.assertGreater(r["avg_s"], 0.0)

    def test_schema_json(self):  # 28
        r = bench_tools.bench_schema_json(n=10)
        self.assertGreater(r["total_s"], 0.0)

    def test_ast_parse(self):  # 29
        r = bench_tools.bench_ast_parse(n=5)
        self.assertGreater(r["ops_per_s"], 0.0)

    def test_ten_tool_bench_xuat_hien_trong_bang(self):  # 30
        results = [
            bench_tools.bench_sanitize(n=20),
            bench_tools.bench_schema_json(n=10),
            bench_tools.bench_ast_parse(n=5),
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            bench_tools.print_table(results)
        out = buf.getvalue()
        for r in results:
            self.assertIn(r["name"], out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
