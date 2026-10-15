#!/usr/bin/env python3
"""Test cho 4 skills batch 2: db-helper, cron-manager, git-workflow, env-setup.

Chạy:  python3 test_skills_c.py
Mỗi skill có >= 5 assertions, chạy bằng python3 thật, unittest chuẩn.
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Nạp 4 module skill (mỗi skill 1 thư mục, tên file không dấu gạch ngang)
ROOT = Path(__file__).resolve().parent.parent
for _skill_dir in ("db-helper", "cron-manager", "git-workflow", "env-setup"):
    sys.path.insert(0, str(ROOT / "skills" / _skill_dir))

import helper  # noqa: E402  (skills/db-helper/helper.py)
from helper import DBHelper, InsertResult, MigrationReport, UnsafeSQLError  # noqa: E402

import manager  # noqa: E402  (skills/cron-manager/manager.py)
from manager import CronError, CronExpression, CronJob  # noqa: E402
from manager import add, describe, list_jobs, parse_cron, remove  # noqa: E402

import workflow  # noqa: E402  (skills/git-workflow/workflow.py)
from workflow import BranchStatus, CheckItem, CommitPlan  # noqa: E402
from workflow import branch_status, pr_checklist, smart_commit  # noqa: E402

import setup as env_setup  # noqa: E402  (skills/env-setup/setup.py)
from setup import CheckResult, EnvConfig  # noqa: E402
from setup import all_ok, check_deps, check_env_vars, check_ports, check_python, run_all  # noqa: E402


# ===========================================================================
# 1. db-helper
# ===========================================================================

class TestDBHelper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, role TEXT)")
        conn.execute("INSERT INTO users (name, role) VALUES ('An', 'admin')")
        conn.commit()
        conn.close()
        self.db = DBHelper(self.db_path, allowed_tables=["users"])
        self.sql_file = Path(self.tmp.name) / "m.sql"

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_query_all(self):
        rows = self.db.query("users")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "An")
        self.assertEqual(rows[0]["role"], "admin")

    def test_query_filters(self):
        self.assertEqual(len(self.db.query("users", {"role": "admin"})), 1)
        self.assertEqual(len(self.db.query("users", {"role": "khong-co"})), 0)
        # Lọc theo nhiều điều kiện (AND)
        self.assertEqual(len(self.db.query("users", {"role": "admin", "name": "An"})), 1)

    def test_query_table_ngoai_whitelist_bi_chan(self):
        with self.assertRaises(UnsafeSQLError):
            self.db.query("sqlite_master")

    def test_query_ten_bang_injection_bi_chan(self):
        with self.assertRaises(UnsafeSQLError):
            self.db.query("users; DROP TABLE users--")
        with self.assertRaises(UnsafeSQLError):
            self.db.query("users", {"role; --": "x"})

    def test_insert_va_doc_lai(self):
        res = self.db.insert("users", {"name": "Binh", "role": "user"})
        self.assertIsInstance(res, InsertResult)
        self.assertGreater(res.rowid, 0)
        self.assertEqual(res.table, "users")
        rows = self.db.query("users", {"name": "Binh"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "user")

    def test_insert_gia_tri_co_quote_khong_pha_cau_lenh(self):
        # Parameterized query: quote trong giá trị không phá được SQL
        self.db.insert("users", {"name": "O'Brien", "role": "user"})
        rows = self.db.query("users", {"name": "O'Brien"})
        self.assertEqual(len(rows), 1)
        # Bảng vẫn còn nguyên 2 dòng (không bị injection xóa)
        self.assertEqual(len(self.db.query("users")), 2)

    def test_insert_bang_ngoai_whitelist_bi_chan(self):
        with self.assertRaises(UnsafeSQLError):
            self.db.insert("bang_la", {"a": 1})

    def test_delete_bat_buoc_co_where(self):
        with self.assertRaises(UnsafeSQLError):
            self.db.delete("users", {})
        # Bảng vẫn còn nguyên sau lần chặn
        self.assertEqual(len(self.db.query("users")), 1)

    def test_delete_co_filters(self):
        n = self.db.delete("users", {"name": "An"})
        self.assertEqual(n, 1)
        self.assertEqual(self.db.query("users"), [])

    def test_migrate_ap_dung_thanh_cong(self):
        self.sql_file.write_text(
            "CREATE TABLE jobs (id INTEGER);\nINSERT INTO jobs (id) VALUES (1);",
            encoding="utf-8",
        )
        db2 = DBHelper(self.db_path, allowed_tables=["users", "jobs"])
        try:
            rep = db2.migrate(self.sql_file)
            self.assertIsInstance(rep, MigrationReport)
            self.assertTrue(rep.applied)
            self.assertEqual(rep.statements, 2)
            self.assertEqual(db2.query("jobs"), [{"id": 1}])
        finally:
            db2.close()

    def test_migrate_chan_drop(self):
        self.sql_file.write_text("DROP TABLE users;", encoding="utf-8")
        with self.assertRaises(UnsafeSQLError):
            self.db.migrate(self.sql_file)
        # Bảng vẫn còn nguyên vì quét an toàn chạy TRƯỚC khi thực thi
        self.assertEqual(len(self.db.query("users")), 1)

    def test_migrate_chan_delete_khong_where(self):
        self.sql_file.write_text("DELETE FROM users;", encoding="utf-8")
        with self.assertRaises(UnsafeSQLError):
            self.db.migrate(self.sql_file)
        self.assertEqual(len(self.db.query("users")), 1)

    def test_migrate_cho_phep_delete_co_where(self):
        self.sql_file.write_text("DELETE FROM users WHERE name = 'An';", encoding="utf-8")
        rep = self.db.migrate(self.sql_file)
        self.assertTrue(rep.applied)
        self.assertEqual(self.db.query("users"), [])


# ===========================================================================
# 2. cron-manager
# ===========================================================================

class TestCronManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "jobs.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_va_list(self):
        job = add("backup", "@daily", "python3 backup.py", store=self.store)
        self.assertIsInstance(job, CronJob)
        self.assertEqual(job.name, "backup")
        self.assertEqual(job.schedule, "@daily")
        self.assertTrue(job.enabled)
        jobs = list_jobs(store=self.store)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].command, "python3 backup.py")

    def test_add_ten_trung_bi_chan(self):
        add("dup", "@daily", "echo 1", store=self.store)
        with self.assertRaises(CronError):
            add("dup", "@hourly", "echo 2", store=self.store)
        # Job cũ vẫn còn nguyên 1 cái
        self.assertEqual(len(list_jobs(store=self.store)), 1)

    def test_add_schedule_sai_bi_chan(self):
        with self.assertRaises(CronError):
            add("bad", "khong-phai-cron", "echo", store=self.store)
        with self.assertRaises(CronError):
            add("bad2", "61 * * * *", "echo", store=self.store)
        self.assertEqual(list_jobs(store=self.store), [])

    def test_add_ten_khong_hop_le_bi_chan(self):
        with self.assertRaises(CronError):
            add("ten co dau cach", "@daily", "echo", store=self.store)
        with self.assertRaises(CronError):
            add("", "@daily", "echo", store=self.store)

    def test_remove(self):
        add("rm1", "@hourly", "echo x", store=self.store)
        self.assertTrue(remove("rm1", store=self.store))
        self.assertEqual(list_jobs(store=self.store), [])
        # Xóa job không tồn tại -> False, không raise
        self.assertFalse(remove("khong-co", store=self.store))

    def test_parse_cron_alias(self):
        for alias in ("@daily", "@hourly", "@weekly", "@monthly", "@yearly", "@reboot"):
            c = parse_cron(alias)
            self.assertIsInstance(c, CronExpression)
            self.assertEqual(c.alias, alias)
        c = parse_cron("@daily")
        self.assertEqual(c.hour, [0])
        self.assertEqual(c.minute, [0])

    def test_parse_cron_5_truong(self):
        c = parse_cron("*/5 * * * *")
        self.assertEqual(c.minute, [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55])
        self.assertIsNone(c.alias)
        c = parse_cron("0 9 * * mon-fri")
        self.assertEqual(c.hour, [9])
        self.assertEqual(c.weekday, [1, 2, 3, 4, 5])

    def test_parse_cron_ten_thang_va_khoang(self):
        c = parse_cron("30 8 1 jan,dec *")
        self.assertEqual(c.month, [1, 12])
        self.assertEqual(c.day, [1])
        c = parse_cron("0 9-17 * * *")
        self.assertEqual(c.hour, list(range(9, 18)))

    def test_parse_cron_sai_nem_loi(self):
        for bad in ("61 * * * *", "*/0 * * * *", "* * *", "* * * * * *", "", "abc",
                    "0 0 32 * *", "0 25 * * *", "5-1 * * * *"):
            with self.assertRaises(CronError, msg=f"phải lỗi với {bad!r}"):
                parse_cron(bad)

    def test_describe_tieng_viet(self):
        self.assertIn("5 phút", describe("*/5 * * * *"))
        self.assertIn("00:00", describe("@daily"))
        self.assertIn("khởi động", describe("@reboot"))
        self.assertIn("09:30", describe("30 9 * * *"))

    def test_luu_json_dung_dinh_dang(self):
        add("p1", "0 8 * * *", "echo hi", store=self.store)
        data = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "p1")
        self.assertEqual(data[0]["command"], "echo hi")
        self.assertIn("created_at", data[0])


# ===========================================================================
# 3. git-workflow (dựng repo git thật trong thư mục tạm)
# ===========================================================================

class TestGitWorkflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self._git("init", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        (self.repo / "app.py").write_text("print('hi')\n")
        self._git("add", "-A")
        self._git("commit", "-m", "init")

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True,
                       capture_output=True, text=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_branch_status_sach(self):
        st = branch_status(str(self.repo))
        self.assertIsInstance(st, BranchStatus)
        self.assertEqual(st.branch, "main")
        self.assertTrue(st.clean)
        self.assertEqual(st.staged, [])
        self.assertEqual(st.untracked, [])

    def test_branch_status_phat_hien_file_dirty(self):
        (self.repo / "app.py").write_text("print('changed')\n")
        (self.repo / "moi.txt").write_text("new\n")
        st = branch_status(str(self.repo))
        self.assertFalse(st.clean)
        self.assertIn("app.py", st.modified)
        self.assertIn("moi.txt", st.untracked)

    def test_smart_commit_sinh_message_dang_staged(self):
        (self.repo / "auth.py").write_text("TOKEN = 1\n")
        self._git("add", "auth.py")
        plan = smart_commit(str(self.repo), dry_run=True)
        self.assertIsInstance(plan, CommitPlan)
        self.assertTrue(plan.staged)
        self.assertEqual(plan.type, "feat")  # file mới -> feat
        self.assertTrue(plan.message.startswith("feat("))
        self.assertIn("auth.py", plan.message)
        self.assertIn("auth.py", plan.files)

    def test_smart_commit_khong_co_gi_tra_ve_none(self):
        self.assertIsNone(smart_commit(str(self.repo), dry_run=True))

    def test_smart_commit_that_su_commit(self):
        (self.repo / "x.py").write_text("X = 1\n")
        self._git("add", "x.py")
        plan = smart_commit(str(self.repo), dry_run=False)
        log = subprocess.run(["git", "log", "--format=%s", "-1"], cwd=self.repo,
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(log, plan.message)
        # Sau commit cây phải sạch
        self.assertTrue(branch_status(str(self.repo)).clean)

    def test_smart_commit_nhan_dien_test_va_docs(self):
        (self.repo / "test_login.py").write_text("def test_x(): pass\n")
        self._git("add", "test_login.py")
        plan = smart_commit(str(self.repo), dry_run=True)
        self.assertEqual(plan.type, "test")
        self.assertTrue(plan.message.startswith("test("))

    def test_pr_checklist_phat_hien_loi_tren_main_dirty(self):
        (self.repo / "app.py").write_text("dirty\n")
        items = pr_checklist(str(self.repo))
        self.assertIsInstance(items, list)
        self.assertGreaterEqual(len(items), 4)
        by_name = {i.name: i for i in items}
        for i in items:
            self.assertIsInstance(i, CheckItem)
        # Đang dirty + đứng trên main -> 2 mục này phải fail
        self.assertFalse(by_name["Cây làm việc sạch"].passed)
        self.assertFalse(by_name["Không commit trực tiếp lên main/master"].passed)
        # Mỗi mục fail đều có gợi ý sửa
        self.assertTrue(by_name["Cây làm việc sạch"].fix)

    def test_pr_checklist_nhanh_tinh_nang_sach(self):
        self._git("checkout", "-b", "feat/x")
        items = pr_checklist(str(self.repo))
        by_name = {i.name: i for i in items}
        self.assertTrue(by_name["Cây làm việc sạch"].passed)
        self.assertTrue(by_name["Không commit trực tiếp lên main/master"].passed)
        self.assertTrue(by_name["Không còn dấu conflict"].passed)
        # Nhánh mới chưa push -> mục push phải fail kèm lệnh gợi ý
        self.assertFalse(by_name["Nhánh đã push hết lên remote"].passed)
        self.assertIn("git push", by_name["Nhánh đã push hết lên remote"].fix)


# ===========================================================================
# 4. env-setup
# ===========================================================================

class TestEnvSetup(unittest.TestCase):
    def test_python_version_dat(self):
        r = check_python("3.8")
        self.assertIsInstance(r, CheckResult)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.fix_command, "")
        self.assertIn("Python", r.check)

    def test_python_version_khong_dat(self):
        r = check_python("9.9")  # version không tưởng -> chắc chắn fail
        self.assertEqual(r.status, "fail")
        self.assertTrue(r.fix_command)  # phải có gợi ý sửa
        self.assertIn("9.9", r.detail + r.check)

    def test_dep_da_cai(self):
        (r,) = check_deps(["pydantic"])
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.check, "dep:pydantic")
        self.assertEqual(r.fix_command, "")

    def test_dep_chua_cai(self):
        (r,) = check_deps(["boom_dep_khong_ton_tai_xyz"])
        self.assertEqual(r.status, "fail")
        self.assertIn("pip install boom_dep_khong_ton_tai_xyz", r.fix_command)

    def test_env_var_chua_set(self):
        os.environ.pop("BOOM_TEST_VAR_XYZ", None)
        (r,) = check_env_vars(["BOOM_TEST_VAR_XYZ"])
        self.assertEqual(r.status, "fail")
        self.assertIn("export BOOM_TEST_VAR_XYZ", r.fix_command)

    def test_env_var_da_set(self):
        os.environ["BOOM_TEST_VAR_XYZ"] = "1"
        try:
            (r,) = check_env_vars(["BOOM_TEST_VAR_XYZ"])
            self.assertEqual(r.status, "ok")
            self.assertEqual(r.fix_command, "")
        finally:
            del os.environ["BOOM_TEST_VAR_XYZ"]

    def test_port_dang_bi_chiem(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))  # giữ port này để giả lập "đang dùng"
        port = s.getsockname()[1]
        try:
            (r,) = check_ports([port])
            self.assertEqual(r.status, "fail")
            self.assertIn(str(port), r.fix_command)
            self.assertIn("chiếm", r.detail)
        finally:
            s.close()

    def test_port_con_trong(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # thả ra -> port trống
        (r,) = check_ports([port])
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.fix_command, "")

    def test_run_all_tra_ve_dung_schema(self):
        cfg = EnvConfig(min_python="3.8", deps=["pydantic"],
                        env_vars=[], ports=[])
        results = run_all(cfg)
        self.assertGreaterEqual(len(results), 2)  # python + 1 dep
        self.assertTrue(all_ok(results))
        for r in results:
            d = r.model_dump()
            # Đúng schema yêu cầu: {check, status, fix_command}
            self.assertIn("check", d)
            self.assertIn("status", d)
            self.assertIn("fix_command", d)
            self.assertIn(d["status"], ("ok", "fail", "warn"))

    def test_run_all_co_muc_fail_thi_all_ok_false(self):
        cfg = EnvConfig(deps=["boom_dep_khong_ton_tai_xyz"])
        results = run_all(cfg)
        self.assertFalse(all_ok(results))
        fails = [r for r in results if r.status == "fail"]
        self.assertEqual(len(fails), 1)
        self.assertTrue(fails[0].fix_command.startswith("pip install"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
