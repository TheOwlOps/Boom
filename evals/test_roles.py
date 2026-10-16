"""Test cho agent/src/orchestration/roles.py (ban nang cap cho DynamicOrchestrator).

Chay:  cd ~/workspace/boom-mods/advanced/role-defs && python3 -m pytest test_roles.py -v
   hoac: python3 test_roles.py

REAL_TOOLS la danh sach tool that lay tu AgentEnvironment.get_tools()
trong /tmp/boom_git/agent/src/environment.py (chay thuc te, 131 tools).
Moi role trong ROLE_DEFS chi duoc dung tool nam trong list nay.
"""

import os
import re
import sys
from pathlib import Path
import unittest

# Import root is agent/src (repo convention); the modules under test live
# in subpackages there (they previously lived next to this file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))
from orchestration.roles import ROLE_DEFS, get_role, list_roles  # noqa: E402

# Danh sach 131 tools that tu AgentEnvironment.get_tools() (verify 2026-10-16).
REAL_TOOLS = {
    "append_file", "artifact_csv", "artifact_html", "artifact_json",
    "artifact_markdown", "ast_inspect", "audit_get", "audit_query",
    "authz_add_user", "authz_assign_role", "authz_check", "authz_create_role",
    "authz_list_roles", "authz_list_users", "authz_revoke_role",
    "broadcast_history", "broadcast_send", "browser_click", "browser_close",
    "browser_close_tab", "browser_eval", "browser_fill", "browser_navigate",
    "browser_network", "browser_open_tab", "browser_screenshot", "browser_tabs",
    "browser_use_tab", "browser_wait_for", "calendar_create", "calendar_delete",
    "calendar_list", "captcha_balance", "captcha_solve_image", "connector_status",
    "crawl_site", "cron_add", "cron_list", "cron_remove", "cron_run",
    "delegate_task", "digest_preview", "drive_download", "drive_list",
    "drive_search", "drive_upload", "edit_file", "execute_code", "file_info",
    "forget_learning", "github_get_issue", "github_list_issues",
    "github_list_prs", "github_read_file", "github_search_code",
    "gmail_discard", "gmail_draft", "gmail_read", "gmail_search", "gmail_send",
    "goal_add", "goal_complete", "goal_get", "goal_list", "goal_log",
    "goal_pause", "install_skill", "lazada_search", "learn_from_conversation",
    "list_skills", "list_toolsets", "memory", "news_headlines", "news_search",
    "ocr_image", "ocr_image_queued", "ocr_image_url", "optimize_workflow",
    "patch_file", "person_add", "person_get", "person_list", "person_note",
    "person_search", "process_kill", "process_list", "process_poll",
    "process_start", "read_file", "read_sitemap", "recall_learned",
    "research_brief", "rewind", "sandbox_exec", "scrape_list", "scrape_product",
    "scrape_table", "search_tools", "session_search", "shell_bg", "shell_kill",
    "shell_run", "shell_status", "shopee_product", "shopee_search",
    "spotify_now_playing", "spotify_pause", "spotify_play", "spotify_search",
    "task_cancel", "task_create", "task_list", "task_resume", "task_status",
    "tiki_product", "tiki_search", "trigger_create", "trigger_delete",
    "trigger_list", "trigger_toggle", "verify_code", "verify_text",
    "view_skill", "watch_url", "weather_daily", "weather_now", "web_extract",
    "web_extract_many", "web_fetch", "web_search", "write_file",
}

EXPECTED_ROLES = {"coder", "researcher", "data_scientist",
                  "visual_analyst", "financial_modeler", "validator"}
REQUIRED_FIELDS = {"name", "description", "system_prompt", "tools", "when_to_use"}


def count_sentences(text: str) -> int:
    """Dem so cau don gian: tach theo dau cau . ! ? (ca tieng Viet)."""
    parts = [p.strip() for p in re.split(r"[.!?…]+", text) if p.strip()]
    return len(parts)


class TestRoleDefs(unittest.TestCase):
    def test_all_six_roles_present(self):
        self.assertEqual(set(ROLE_DEFS.keys()), EXPECTED_ROLES)

    def test_each_role_has_all_five_fields(self):
        for role, d in ROLE_DEFS.items():
            with self.subTest(role=role):
                self.assertEqual(set(d.keys()), REQUIRED_FIELDS,
                                 f"role {role} thieu/thua field")

    def test_name_matches_key(self):
        for role, d in ROLE_DEFS.items():
            with self.subTest(role=role):
                self.assertEqual(d["name"], role)

    def test_system_prompt_not_empty_and_long_enough(self):
        for role, d in ROLE_DEFS.items():
            with self.subTest(role=role):
                prompt = d["system_prompt"]
                self.assertIsInstance(prompt, str)
                self.assertTrue(prompt.strip(), f"role {role}: system_prompt rong")
                # Yeu cau: it nhat 3 cau
                self.assertGreaterEqual(
                    count_sentences(prompt), 3,
                    f"role {role}: system_prompt chi co {count_sentences(prompt)} cau (< 3)")

    def test_description_and_when_to_use_not_empty(self):
        for role, d in ROLE_DEFS.items():
            with self.subTest(role=role):
                self.assertTrue(d["description"].strip())
                self.assertTrue(d["when_to_use"].strip())

    def test_all_tools_exist_in_environment(self):
        """QUAN TRONG: moi tool phai ton tai that trong AgentEnvironment."""
        for role, d in ROLE_DEFS.items():
            with self.subTest(role=role):
                self.assertTrue(d["tools"], f"role {role}: tools rong")
                for tool in d["tools"]:
                    self.assertIn(tool, REAL_TOOLS,
                                  f"role {role}: tool '{tool}' KHONG ton tai trong environment.py")

    def test_no_duplicate_tools_per_role(self):
        for role, d in ROLE_DEFS.items():
            with self.subTest(role=role):
                self.assertEqual(len(d["tools"]), len(set(d["tools"])),
                                 f"role {role}: tools bi trung")

    def test_get_role_case_insensitive(self):
        self.assertIsNotNone(get_role("Coder"))
        self.assertIsNotNone(get_role("  VALIDATOR "))
        self.assertIsNone(get_role("khong_ton_tai"))
        self.assertIsNone(get_role(""))
        self.assertIsNone(get_role(None))

    def test_list_roles(self):
        self.assertEqual(set(list_roles()), EXPECTED_ROLES)

    def test_real_tools_count_sanity(self):
        # Sanity check: list hardcode phai du 131 tools nhu environment that
        self.assertEqual(len(REAL_TOOLS), 131)


if __name__ == "__main__":
    unittest.main(verbosity=2)
