"""Test cho 4 skills luồng 2B: deployer, doc-researcher, refactor-assistant, api-designer.

Chạy: python3 test_skills_b.py
Chỉ dùng stdlib. Mỗi skill >= 5 assertions.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "skills"
sys.path.insert(0, str(BASE / "deployer"))
sys.path.insert(0, str(BASE / "doc-researcher"))
sys.path.insert(0, str(BASE / "refactor-assistant"))
sys.path.insert(0, str(BASE / "api-designer"))

from checklist import (  # noqa: E402
    run_checklist, deploy_blocked, check_no_secrets, check_env_documented,
    check_version_bumped, check_changelog_updated, check_backup_plan,
    check_tests_pass)
from research import (  # noqa: E402
    build_research_plan, render_outline, build_source_checklist)
from assistant import RefactorAssistant  # noqa: E402
from design import (  # noqa: E402
    design_api, validate_resources, build_openapi, validate_design,
    render_stubs)


def _du_an_mau(tmp, secret=False, thieu_env=False):
    """Dựng project mẫu tối thiểu để test deployer."""
    root = Path(tmp)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.2.0"\n')
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n- x\n")
    (root / ".env.example").write_text("API_URL=\n")
    code = 'import os\nAPI_URL = os.getenv("API_URL")\n'
    if secret:
        code += 'KEY = "AKIAIOSFODNN7DEMO1234"  # khoa bi mat AWS that\n'
    if thieu_env:
        code += 'OTHER = os.getenv("MISSING_VAR")\n'
    (root / "app.py").write_text(code)
    (root / "test_app.py").write_text(
        "def test_ok():\n    assert True\n")
    return root


# ---------------------------------------------------------------------------
# 1. deployer
# ---------------------------------------------------------------------------

class TestDeployer(unittest.TestCase):
    def test_checklist_tra_ve_6_check_dung_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _du_an_mau(tmp)
            kq = run_checklist(str(root), test_command="python3 -m pytest -q")
            self.assertEqual(len(kq), 6)  # đủ 6 nhóm check
            for item in kq:
                self.assertIn("check", item)
                self.assertIn("status", item)
                self.assertIn("detail", item)
                self.assertIn(item["status"], ("pass", "fail", "warn"))

    def test_phat_hien_secret_thi_fail_va_block_deploy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _du_an_mau(tmp, secret=True)
            r = check_no_secrets(str(root))
            self.assertEqual(r["status"], "fail")
            self.assertIn("AWS", r["detail"])
            self.assertTrue(deploy_blocked([r]))  # fail -> block deploy

    def test_khong_secret_thi_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _du_an_mau(tmp)
            r = check_no_secrets(str(root))
            self.assertEqual(r["status"], "pass")

    def test_env_thieu_document_thi_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _du_an_mau(tmp, thieu_env=True)
            r = check_env_documented(str(root))
            self.assertEqual(r["status"], "fail")
            self.assertIn("MISSING_VAR", r["detail"])

    def test_env_du_document_thi_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _du_an_mau(tmp)
            r = check_env_documented(str(root))
            self.assertEqual(r["status"], "pass")

    def test_version_khong_git_thi_warn_khong_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _du_an_mau(tmp)
            r = check_version_bumped(str(root))
            self.assertEqual(r["status"], "warn")  # không chặn khi thiếu git

    def test_changelog_co_unreleased_thi_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _du_an_mau(tmp)
            r = check_changelog_updated(str(root))
            self.assertEqual(r["status"], "pass")

    def test_thieu_backup_thi_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _du_an_mau(tmp)
            r = check_backup_plan(str(root))
            self.assertEqual(r["status"], "warn")

    def test_co_script_backup_thi_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _du_an_mau(tmp)
            (root / "backup.sh").write_text("#!/bin/sh\n# backup\n")
            r = check_backup_plan(str(root))
            self.assertEqual(r["status"], "pass")

    def test_tests_pass_that(self):
        # chạy test command thật (stdlib, không cần pytest)
        with tempfile.TemporaryDirectory() as tmp:
            root = _du_an_mau(tmp)
            (root / "run_checks.py").write_text(
                "import sys\nsys.exit(0)\n")
            r = check_tests_pass(str(root),
                                 test_command="python3 run_checks.py")
            self.assertEqual(r["status"], "pass")

    def test_tests_fail_that(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _du_an_mau(tmp)
            (root / "run_checks.py").write_text(
                "import sys\nsys.exit(1)\n")
            r = check_tests_pass(str(root),
                                 test_command="python3 run_checks.py")
            self.assertEqual(r["status"], "fail")
            self.assertTrue(deploy_blocked([r]))


# ---------------------------------------------------------------------------
# 2. doc-researcher
# ---------------------------------------------------------------------------

class TestDocResearcher(unittest.TestCase):
    def test_plan_co_du_6_goc_nhin(self):
        plan = build_research_plan("So sánh Redis vs Valkey")
        ids = [q.id for q in plan.questions]
        for gid in ("what", "why", "how", "compare", "risk", "practice"):
            self.assertIn(gid, ids)

    def test_depth_deep_them_cau_hoi_dao_sau(self):
        quick = build_research_plan("X", depth="quick")
        deep = build_research_plan("X", depth="deep")
        self.assertEqual(len(quick.questions), 6)
        self.assertEqual(len(deep.questions), 10)  # 6 + 4 đào sâu

    def test_depth_sai_thi_raise(self):
        with self.assertRaises(ValueError):
            build_research_plan("X", depth="sieu-sau")

    def test_topic_rong_thi_raise(self):
        with self.assertRaises(ValueError):
            build_research_plan("   ")

    def test_cau_hoi_user_duoc_merge_len_dau(self):
        plan = build_research_plan("X", questions=["Chi phí bao nhiêu?"])
        self.assertEqual(plan.questions[0].id, "user-1")
        self.assertEqual(plan.questions[0].question, "Chi phí bao nhiêu?")

    def test_moi_cau_hoi_co_nguon_goi_y(self):
        plan = build_research_plan("X")
        for q in plan.questions:
            self.assertTrue(q.suggested_sources)
            self.assertTrue(q.why)

    def test_outline_co_template_todo(self):
        plan = build_research_plan("So sánh A vs B", goal="chọn 1")
        md = render_outline(plan)
        self.assertIn("# Research: So sánh A vs B", md)
        self.assertIn("chọn 1", md)
        self.assertGreater(md.count("> TODO:"), 6)  # mỗi câu hỏi + tổng hợp

    def test_source_checklist_map_dung(self):
        plan = build_research_plan("X")
        cl = build_source_checklist(plan)
        self.assertIn("official-docs", cl)
        self.assertIn("what", cl["official-docs"])  # câu what cần official-docs

    def test_to_dict_day_du(self):
        d = build_research_plan("X").to_dict()
        self.assertEqual(
            set(d.keys()),
            {"topic", "goal", "depth", "questions",
             "outline_markdown", "source_checklist"})
        self.assertTrue(json.dumps(d, ensure_ascii=False))  # JSON-serializable


# ---------------------------------------------------------------------------
# 3. refactor-assistant
# ---------------------------------------------------------------------------

def _project_refactor(tmp):
    root = Path(tmp)
    (root / "a.py").write_text(
        "def tinh_tong(a, b):\n"
        "    return a + b\n"
        "\n"
        "x = tinh_tong(1, 2)\n")
    (root / "b.py").write_text(
        "from a import tinh_tong\n"
        "\n"
        "y = tinh_tong(3, 4)\n")
    return root


class TestRefactorAssistant(unittest.TestCase):
    def test_rename_preview_khong_ghi_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _project_refactor(tmp)
            ra = RefactorAssistant(str(root))
            kq = ra.rename("tinh_tong", "cong", ["a.py", "b.py"], preview=True)
            self.assertTrue(kq["ok"])
            self.assertTrue(kq.get("dry_run"))
            # file gốc không đổi
            self.assertIn("tinh_tong", (root / "a.py").read_text())

    def test_rename_preview_co_diff_va_dem_occurrences(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _project_refactor(tmp)
            ra = RefactorAssistant(str(root))
            kq = ra.rename("tinh_tong", "cong", ["a.py", "b.py"], preview=True)
            self.assertEqual(len(kq["renamed_files"]), 2)
            self.assertGreaterEqual(kq["occurrences"], 3)
            self.assertIn("diffs", kq)
            diff = next(iter(kq["diffs"].values()))
            self.assertIn("tinh_tong", diff)
            self.assertIn("cong", diff)

    def test_rename_apply_that_doi_ten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _project_refactor(tmp)
            ra = RefactorAssistant(str(root))
            kq = ra.rename("tinh_tong", "cong", ["a.py"], preview=False)
            self.assertTrue(kq["ok"])
            text = (root / "a.py").read_text()
            self.assertIn("def cong(", text)
            self.assertNotIn("tinh_tong", text)

    def test_rename_ten_sai_thi_bao_loi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _project_refactor(tmp)
            ra = RefactorAssistant(str(root))
            kq = ra.rename("tinh_tong", "123-sai", ["a.py"], preview=True)
            self.assertFalse(kq["ok"])
            self.assertIn("error", kq)

    def test_apply_last_preview_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _project_refactor(tmp)
            ra = RefactorAssistant(str(root))
            # chưa preview -> apply báo lỗi
            kq = ra.apply_last_preview()
            self.assertFalse(kq["ok"])
            # preview rồi apply
            p = ra.rename("tinh_tong", "cong", ["a.py"], preview=True)
            self.assertTrue(p["ok"])
            a = ra.apply_last_preview()
            self.assertTrue(a["ok"])
            self.assertIn("def cong(", (root / "a.py").read_text())
            # apply 2 lần không được (tránh apply nhầm)
            a2 = ra.apply_last_preview()
            self.assertFalse(a2["ok"])

    def test_extract_preview_suy_params_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "c.py").write_text(
                "def main():\n"
                "    gia = 100\n"
                "    thue = gia * 0.1\n"
                "    tong = gia + thue\n"
                "    print(tong)\n")
            ra = RefactorAssistant(str(root))
            # tách dòng 3-4: 'gia' định nghĩa ở ngoài -> param;
            # 'tong' gán trong đoạn và dùng sau (print) -> return
            kq = ra.extract("c.py", 3, 4, "tinh_thue", preview=True)
            self.assertTrue(kq["ok"], kq.get("error"))
            self.assertIn("gia", kq["params"])
            self.assertIn("tong", kq["returns"])
            self.assertIn("def tinh_thue(", kq["diff"])

    def test_move_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src.py").write_text("def helper():\n    return 1\n")
            (root / "dst.py").write_text('"""dst"""\n')
            ra = RefactorAssistant(str(root))
            kq = ra.move("src.py", "dst.py", "helper", preview=True)
            self.assertTrue(kq["ok"])
            self.assertIn("def helper", kq["diff_dest"])

    def test_rollback_khoi_phuc_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _project_refactor(tmp)
            ra = RefactorAssistant(str(root))
            goc = (root / "a.py").read_text()
            ra.rename("tinh_tong", "cong", ["a.py"], preview=False)
            self.assertNotEqual((root / "a.py").read_text(), goc)
            ra.rollback()
            self.assertEqual((root / "a.py").read_text(), goc)


# ---------------------------------------------------------------------------
# 4. api-designer
# ---------------------------------------------------------------------------

_RESOURCES = [
    {"name": "products",
     "fields": {"name": "string", "price": "number", "in_stock": "boolean"}},
    {"name": "orders",
     "fields": {"product_id": "uuid", "quantity": "integer",
                "tags": "array[string]"}},
]


class TestApiDesigner(unittest.TestCase):
    def test_design_api_thanh_cong(self):
        r = design_api("Shop API", _RESOURCES, framework="fastapi")
        self.assertEqual(r["openapi"]["openapi"], "3.0.3")
        self.assertEqual(r["openapi"]["info"]["title"], "Shop API")
        self.assertIn("/products", r["openapi"]["paths"])
        self.assertIn("/products/{id}", r["openapi"]["paths"])
        self.assertIn("/orders", r["openapi"]["paths"])

    def test_crud_du_5_operation_va_status_code_chuan(self):
        r = design_api("Shop API", _RESOURCES)
        paths = r["openapi"]["paths"]
        ops = paths["/products"]
        self.assertEqual(ops["post"]["responses"].keys() >= {"201"},
                         True)
        self.assertIn("201", ops["post"]["responses"])
        self.assertIn("204", paths["/products/{id}"]["delete"]["responses"])
        self.assertIn("200", ops["get"]["responses"])

    def test_list_bat_buoc_pagination(self):
        r = design_api("Shop API", _RESOURCES)
        params = {p["name"]
                  for p in r["openapi"]["paths"]["/products"]["get"]["parameters"]}
        self.assertTrue({"page", "page_size"} <= params)
        # validation pass hết
        self.assertTrue(all(v["status"] == "pass"
                            for v in r["validation"]))

    def test_schema_co_error_va_pagination_envelope(self):
        r = design_api("Shop API", _RESOURCES)
        schemas = r["openapi"]["components"]["schemas"]
        self.assertIn("Error", schemas)
        self.assertIn("Product", schemas)
        self.assertIn("ProductCreate", schemas)
        self.assertNotIn("id", schemas["ProductCreate"]["properties"])  # create bỏ id
        self.assertEqual(schemas["Order"]["properties"]["tags"]["type"], "array")

    def test_ten_resource_sai_thi_raise(self):
        with self.assertRaises(ValueError):
            design_api("X", [{"name": "Product", "fields": {"a": "string"}}])
        with self.assertRaises(ValueError):
            design_api("X", [{"name": "product", "fields": {"a": "string"}}])

    def test_type_khong_ho_tro_thi_raise(self):
        with self.assertRaises(ValueError):
            design_api("X", [{"name": "items",
                              "fields": {"x": "kieu-la"}}])

    def test_stubs_fastapi(self):
        stub = render_stubs(
            [{"name": "products",
              "fields": {"name": "string", "price": "number"},
              "description": ""}],
            framework="fastapi")
        self.assertIn("APIRouter", stub)
        self.assertIn("status.HTTP_201_CREATED", stub)
        self.assertIn("status.HTTP_204_NO_CONTENT", stub)
        self.assertIn("page_size", stub)
        compile(stub, "<stub>", "exec")  # stub phải là Python hợp lệ

    def test_stubs_flask(self):
        stub = render_stubs(
            [{"name": "products",
              "fields": {"name": "string"},
              "description": ""}],
            framework="flask")
        self.assertIn("Blueprint", stub)
        self.assertIn(", 201", stub)
        self.assertIn(", 204", stub)
        compile(stub, "<stub>", "exec")

    def test_framework_sai_thi_raise(self):
        with self.assertRaises(ValueError):
            render_stubs([], framework="django")

    def test_validate_resources_trung_ten(self):
        errors, _ = validate_resources([
            {"name": "items", "fields": {"a": "string"}},
            {"name": "items", "fields": {"b": "string"}},
        ])
        self.assertTrue(any("trùng" in e for e in errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
