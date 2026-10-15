"""Test cho DynamicOrchestrator (chạy standalone, không cần repo Boom).

Chạy:  python3 test_dynamic.py   (hoặc pytest test_dynamic.py)
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dynamic import DynamicOrchestrator, SubAgent, ROLES


class FakeDelegationManager:
    """Fake delegation_manager: execute_task đồng bộ, trả dict kiểu DelegationManager."""

    def __init__(self, fail_roles=()):
        self.calls = []
        self.fail_roles = set(fail_roles)

    def execute_task(self, subtask):
        self.calls.append(subtask)
        role = getattr(subtask, "role", "")
        if role in self.fail_roles:
            return {"task_id": "t1", "role": role, "status": "failed",
                    "summary": "", "error": "boom"}
        return {"task_id": "t1", "role": role, "status": "completed",
                "summary": f"real result for {role}"}


class AsyncFakeDelegationManager(FakeDelegationManager):
    async def execute_task(self, subtask):  # noqa: D102
        return super().execute_task(subtask)


class TestAnalyzeTask(unittest.TestCase):
    def setUp(self):
        self.orch = DynamicOrchestrator()  # mock mode

    def test_coder_and_data_scientist(self):
        roles = self.orch.analyze_task("viết code phân tích số liệu bán hàng")
        self.assertIn("coder", roles)
        self.assertIn("data_scientist", roles)

    def test_validator_appended_when_multiple(self):
        roles = self.orch.analyze_task("viết code phân tích số liệu bán hàng")
        self.assertEqual(roles[-1], "validator")
        self.assertEqual(len(roles), len(set(roles)), "roles không được trùng")

    def test_single_role_no_validator(self):
        roles = self.orch.analyze_task("sửa bug đăng nhập")
        self.assertEqual(roles, ["coder"])

    def test_researcher_fallback(self):
        roles = self.orch.analyze_task("xin chào")
        self.assertEqual(roles, ["researcher"])

    def test_visual_and_financial(self):
        roles = self.orch.analyze_task("đọc ảnh hoá đơn rồi tính giá vốn lợi nhuận")
        self.assertIn("visual_analyst", roles)
        self.assertIn("financial_modeler", roles)

    def test_english_keywords(self):
        roles = self.orch.analyze_task("search price data and fix bug")
        for r in ("researcher", "financial_modeler", "data_scientist", "coder", "validator"):
            self.assertIn(r, roles)


class TestLifecycle(unittest.TestCase):
    def test_spawn_mock_marks_done(self):
        orch = DynamicOrchestrator()
        agent = orch.spawn("coder", "viết hàm cộng")
        self.assertTrue(agent.id.startswith("agent_"))
        self.assertEqual(agent.status, "done")
        self.assertTrue(agent.result["mock"])
        self.assertIsNotNone(orch.get_agent(agent.id))

    def test_spawn_retire_lifecycle(self):
        orch = DynamicOrchestrator()
        agent = orch.spawn("researcher", "tìm tài liệu")
        self.assertEqual(len(orch.live_agents()), 1)
        self.assertTrue(orch.retire(agent.id))
        self.assertEqual(agent.status, "done")
        self.assertEqual(len(orch.live_agents()), 0)
        self.assertIsNone(orch.get_agent(agent.id))

    def test_retire_unknown_returns_false(self):
        orch = DynamicOrchestrator()
        self.assertFalse(orch.retire("agent_nope"))

    def test_subagent_ids_unique(self):
        orch = DynamicOrchestrator()
        ids = {orch.spawn("coder", "x").id for _ in range(20)}
        self.assertEqual(len(ids), 20)

    def test_spawn_with_real_delegation_manager(self):
        fake = FakeDelegationManager()
        orch = DynamicOrchestrator(delegation_manager=fake)
        agent = orch.spawn("coder", "viết code")
        self.assertEqual(agent.status, "done")
        self.assertEqual(agent.result["status"], "completed")
        self.assertIn("real result for coder", agent.result["summary"])
        # subtask bọc đúng role + goal chứa task
        self.assertEqual(fake.calls[0].role, "coder")
        self.assertIn("viết code", fake.calls[0].goal)

    def test_spawn_failure_marks_failed(self):
        fake = FakeDelegationManager(fail_roles={"coder"})
        orch = DynamicOrchestrator(delegation_manager=fake)
        agent = orch.spawn("coder", "viết code")
        self.assertEqual(agent.status, "failed")

    def test_spawn_exception_marks_failed(self):
        class Boom:
            def execute_task(self, subtask):
                raise RuntimeError("down")

        orch = DynamicOrchestrator(delegation_manager=Boom())
        agent = orch.spawn("coder", "x")
        self.assertEqual(agent.status, "failed")
        self.assertIn("RuntimeError", agent.result["error"])

    def test_spawn_async_inside_running_loop(self):
        # Đang trong event loop: spawn trả coroutine trong result, agent giữ running.
        async def main():
            orch = DynamicOrchestrator(delegation_manager=AsyncFakeDelegationManager())
            agent = orch.spawn("coder", "viết code")
            self.assertEqual(agent.status, "running")
            outcome = await agent.result  # caller tự await
            self.assertEqual(outcome["status"], "completed")
            return agent

        agent = asyncio.run(main())
        self.assertEqual(agent.status, "running")  # chưa ai retire/collect


class TestOrchestrate(unittest.TestCase):
    def test_orchestrate_mock_all_done(self):
        orch = DynamicOrchestrator()
        out = orch.orchestrate("viết code phân tích số liệu bán hàng")
        self.assertEqual(out["task"], "viết code phân tích số liệu bán hàng")
        self.assertIn("coder", out["roles"])
        self.assertIn("data_scientist", out["roles"])
        # tất cả agents done
        self.assertTrue(all(a["status"] == "done" for a in out["agents"]))
        # results đầy đủ theo role
        self.assertEqual(set(out["results"].keys()), set(out["roles"]))
        for role, res in out["results"].items():
            self.assertTrue(res["mock"], f"role {role} phải là mock")
        # đã retire hết
        self.assertEqual(len(orch.live_agents()), 0)
        # summary nhắc đủ roles
        for role in out["roles"]:
            self.assertIn(role, out["summary"])

    def test_orchestrate_with_fake_manager(self):
        fake = FakeDelegationManager()
        orch = DynamicOrchestrator(delegation_manager=fake)
        out = orch.orchestrate("tìm tài liệu về giá")
        self.assertIn("researcher", out["roles"])
        self.assertIn("financial_modeler", out["roles"])
        self.assertTrue(all(a["status"] == "done" for a in out["agents"]))
        self.assertEqual(
            out["results"]["researcher"]["summary"], "real result for researcher"
        )
        self.assertEqual(len(fake.calls), len(out["roles"]))

    def test_orchestrate_single_role(self):
        orch = DynamicOrchestrator()
        out = orch.orchestrate("sửa bug đăng nhập")
        self.assertEqual(out["roles"], ["coder"])
        self.assertEqual(len(out["agents"]), 1)

    def test_orchestrate_partial_failure_keeps_going(self):
        fake = FakeDelegationManager(fail_roles={"data_scientist"})
        orch = DynamicOrchestrator(delegation_manager=fake)
        out = orch.orchestrate("viết code phân tích số liệu")
        statuses = {a["role"]: a["status"] for a in out["agents"]}
        self.assertEqual(statuses["data_scientist"], "failed")
        self.assertEqual(statuses["coder"], "done")
        self.assertEqual(statuses["validator"], "done")


class TestCatalogue(unittest.TestCase):
    def test_roles_known(self):
        self.assertEqual(
            set(ROLES),
            {"coder", "researcher", "data_scientist",
             "visual_analyst", "financial_modeler", "validator"},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
