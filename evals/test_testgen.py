"""Test cho TestGenerator — stdlib only (unittest), chay: python3 test_testgen.py."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from testgen import TestGenerator, run_generated_tests

SAMPLE = Path(__file__).parent / "sample_module.py"


class TestAnalyze(unittest.TestCase):
    def setUp(self):
        self.gen = TestGenerator()

    def test_trich_du_function(self):
        funcs = self.gen.analyze_module(str(SAMPLE))
        names = {f["name"] for f in funcs}
        self.assertEqual(names, {"add", "greet", "fetch_count", "multiply"})

    def test_bo_qua_private_mac_dinh(self):
        funcs = self.gen.analyze_module(str(SAMPLE))
        self.assertNotIn("_private_helper", {f["name"] for f in funcs})

    def test_include_private(self):
        funcs = self.gen.analyze_module(str(SAMPLE), include_private=True)
        self.assertIn("_private_helper", {f["name"] for f in funcs})

    def test_args_va_default(self):
        funcs = {f["name"]: f for f in self.gen.analyze_module(str(SAMPLE))}
        self.assertEqual(
            funcs["greet"]["args"],
            [{"name": "name", "has_default": False},
             {"name": "greeting", "has_default": True}])
        self.assertEqual(
            [a["name"] for a in funcs["multiply"]["args"]], ["x", "y"])
        # method: self da bi loai
        self.assertNotIn("self", [a["name"] for a in funcs["multiply"]["args"]])

    def test_async_va_class_name(self):
        funcs = {f["name"]: f for f in self.gen.analyze_module(str(SAMPLE))}
        self.assertTrue(funcs["fetch_count"]["is_async"])
        self.assertFalse(funcs["add"]["is_async"])
        self.assertEqual(funcs["multiply"]["class_name"], "Calculator")
        self.assertIsNone(funcs["add"]["class_name"])

    def test_docstring(self):
        funcs = {f["name"]: f for f in self.gen.analyze_module(str(SAMPLE))}
        self.assertIn("Cong 2 so", funcs["add"]["docstring"])

    def test_file_loi_syntax_tra_ve_rong(self):
        with tempfile.NamedTemporaryFile("w", suffix=".py",
                                         delete=False) as fh:
            fh.write("def broken(:\n")
            bad = fh.name
        self.assertEqual(self.gen.analyze_module(bad), [])

    def test_file_khong_ton_tai_tra_ve_rong(self):
        self.assertEqual(self.gen.analyze_module("/tmp/khong_ton_tai_xyz.py"), [])


class TestEdgeCases(unittest.TestCase):
    def setUp(self):
        self.gen = TestGenerator()

    def test_luon_co_5_case_chuan(self):
        info = {"name": "f", "args": [{"name": "x", "has_default": False}]}
        cases = {c["case_name"]: c["args"]
                 for c in self.gen.generate_edge_cases(info)}
        self.assertEqual(cases["empty_string"], {"x": repr("")})
        self.assertEqual(cases["empty_list"], {"x": repr([])})
        self.assertEqual(cases["zero"], {"x": repr(0)})
        self.assertEqual(cases["negative_one"], {"x": repr(-1)})
        self.assertEqual(cases["huge_number"], {"x": repr(10 ** 18)})

    def test_arg_bat_buoc_co_case_none(self):
        info = {"name": "f", "args": [{"name": "x", "has_default": False},
                                      {"name": "y", "has_default": True}]}
        cases = {c["case_name"]: c["args"]
                 for c in self.gen.generate_edge_cases(info)}
        self.assertEqual(cases["required_args_none"], {"x": repr(None)})

    def test_khong_arg_thi_rong(self):
        self.assertEqual(
            self.gen.generate_edge_cases({"name": "f", "args": []}), [])

    def test_value_repr_an_toan(self):
        # repr phai eval duoc ve dung gia tri goc
        info = {"name": "f", "args": [{"name": "x", "has_default": False}]}
        for c in self.gen.generate_edge_cases(info):
            for v_repr in c["args"].values():
                eval(v_repr)  # khong raise moi dat


class TestGenerateAndRun(unittest.TestCase):
    def setUp(self):
        self.gen = TestGenerator()
        self.tmp = Path(tempfile.mkdtemp())
        self.out = str(self.tmp / "test_sample_gen.py")

    def test_write_tests_tra_ve_so_luong(self):
        n = self.gen.write_tests(str(SAMPLE), self.out)
        # 4 functions x (1 happy + 6 edge) = 28
        self.assertEqual(n, 28)
        self.assertTrue(Path(self.out).exists())

    def test_file_sinh_ra_hop_le_va_chay_pass(self):
        n = self.gen.write_tests(str(SAMPLE), self.out)
        result = run_generated_tests(self.out)
        self.assertEqual(result["total"], n, result["failed"])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["passed"], n)

    def test_header_va_importlib(self):
        src = self.gen.generate_tests(str(SAMPLE))
        self.assertIn('"""Auto-generated tests for sample_module."""', src)
        self.assertIn("importlib.util.spec_from_file_location", src)
        self.assertIn("def test_add_happy_path():", src)
        self.assertIn("def test_greet_edge_empty_string():", src)
        self.assertIn("def test_multiply_happy_path():", src)
        self.assertIn("asyncio.run", src)  # cho fetch_count

    def test_sample_value_heuristic(self):
        self.assertEqual(self.gen.sample_value("path"), "/tmp/test")
        self.assertEqual(self.gen.sample_value("file_name"), "/tmp/test")
        self.assertEqual(self.gen.sample_value("text"), "test")
        self.assertEqual(self.gen.sample_value("count"), 3)
        self.assertEqual(self.gen.sample_value("n"), 3)
        self.assertEqual(self.gen.sample_value("items"), [1, 2, 3])
        self.assertEqual(self.gen.sample_value("enabled"), True)
        self.assertEqual(self.gen.sample_value("whatever"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
