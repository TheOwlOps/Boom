"""Check that delegate_task runs real subagents, ordered by the DAG and shaped by roles.

Uses a scripted provider: the parent plans five subtasks — two independent, one that fails
in the provider, one that depends on the first two, one that depends on a task that does
not exist — so roles, dependency ordering, dependency output passing and both failure
paths are all exercised.
"""

import ast
import asyncio
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from environment import AgentEnvironment
from loop.react import ReActLoop
from providers.llm import LLMResponse, LLMUsage, ToolCallSpec
from tools.delegation import DelegationManager

EVAL_FILE = Path(__file__).resolve()

PLAN = {"tasks": [
    {"task_id": "a", "goal": "Find answer A", "role": "planner"},
    {"task_id": "b", "goal": "Find answer B", "role": "coder"},
    {"task_id": "c", "goal": "BOOM explode", "role": "reviewer"},
    {"task_id": "d", "goal": "Summarise A and B", "role": "reviewer", "depends_on": ["a", "b"]},
    {"task_id": "ghost", "goal": "Find answer ghost", "depends_on": ["nope"]},
]}


class ScriptedLLM:
    """Parent plans once; children answer, except BOOM (raises) and BURN (never stops)."""

    def __init__(self):
        self.model = "scripted"
        self.child_calls = []

    async def chat_complete(self, messages, tools_schema=None):
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        last_user = [m["content"] for m in messages if m.get("role") == "user"][-1]

        if last_user.startswith(("Find answer", "Summarise", "BOOM", "BURN")):
            self.child_calls.append({
                "goal": last_user.splitlines()[0],
                "prompt": last_user,
                "system": system,
                "tools": [s["name"] for s in (tools_schema or [])],
            })
            if last_user.startswith("BOOM"):
                raise RuntimeError("subagent provider exploded")
            if last_user.startswith("BURN"):
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCallSpec(id="burn", name="read_file", arguments={"path": str(EVAL_FILE)})],
                    usage=LLMUsage(input_tokens=10, output_tokens=10),
                    finish_reason="tool_calls",
                )
            return LLMResponse(
                content=f"answer for: {last_user.splitlines()[0]}",
                usage=LLMUsage(input_tokens=3, output_tokens=2),
                finish_reason="stop",
            )

        if not any(m.get("role") == "tool" for m in messages):
            return LLMResponse(
                content="",
                tool_calls=[ToolCallSpec(id="call_1", name="delegate_task", arguments=PLAN)],
                usage=LLMUsage(input_tokens=5, output_tokens=5),
                finish_reason="tool_calls",
            )
        return LLMResponse(
            content="Delegated.", usage=LLMUsage(input_tokens=6, output_tokens=1), finish_reason="stop"
        )


def child_call(llm, goal):
    for call in llm.child_calls:
        if call["goal"] == goal:
            return call
    raise AssertionError(f"no subagent ran for goal {goal!r}: {[c['goal'] for c in llm.child_calls]}")


async def test_delegation():
    test_db = root_dir / "evals" / "delegation_test.db"
    if test_db.exists():
        test_db.unlink()

    env = AgentEnvironment(root_dir=root_dir, db_path=test_db, yolo=True)
    llm = ScriptedLLM()
    loop = ReActLoop(env, llm, accountant=env.token_accountant, session_id="deleg")

    result = await loop.step("Delegate this work.")
    assert result["success"] is True, result
    assert result["action"] == "Delegated.", result["action"]

    report = ast.literal_eval(result["trace"][0]["observation"])
    assert report["total_subagents"] == 5, report
    assert report["completed"] == 3, report  # a, b, d
    by_id = {r["task_id"]: r for r in report["results"]}
    assert by_id["a"]["status"] == "completed", by_id
    assert by_id["a"]["summary"] == "answer for: Find answer A", by_id
    assert by_id["c"]["status"] == "failed" and "RuntimeError" in by_id["c"]["error"], by_id
    assert by_id["d"]["status"] == "completed", by_id
    assert "unknown dependency" in by_id["ghost"]["error"], by_id
    print("[PASS] DAG-ordered delegation: 3 completed, 1 provider failure, 1 unknown dependency.")

    # The role decides the subagent's instructions and its toolset.
    planner = child_call(llm, "Find answer A")
    assert planner["system"].startswith("Decompose the user's goal"), planner["system"][:80]
    assert "read_file" in planner["tools"] and "sandbox_exec" not in planner["tools"], planner["tools"]

    coder = child_call(llm, "Find answer B")
    assert coder["system"].startswith("Write and fix code"), coder["system"][:80]
    assert "write_file" in coder["tools"] and "list_skills" not in coder["tools"], coder["tools"]

    # A role-less subtask keeps the parent's toolset, minus the withheld tools. (The ghost
    # subtask above never reaches a subagent: its dependency is refused before it starts.)
    plain = await env.delegation.delegate_task([{"goal": "Find answer plain", "task_id": "plain"}])
    assert plain["results"][0]["status"] == "completed", plain
    unroled = child_call(llm, "Find answer plain")
    assert "write_file" in unroled["tools"] and "list_skills" in unroled["tools"], unroled

    for call in llm.child_calls:
        assert "delegate_task" not in call["tools"], call
        assert "execute_code" not in call["tools"], call
    print("[PASS] Role instructions and per-role toolsets reached the subagents.")

    # A dependency's report is handed to the subtask that waits on it.
    dependent = child_call(llm, "Summarise A and B")
    assert "Results from the subtasks this one depends on" in dependent["prompt"], dependent["prompt"]
    assert "answer for: Find answer A" in dependent["prompt"], dependent["prompt"]
    assert "answer for: Find answer B" in dependent["prompt"], dependent["prompt"]
    print("[PASS] Dependency outputs were passed into the dependent subagent.")

    # Subagent turns are real turns: they land in the transcript under a child id.
    child = env.state_db.get_session_messages("deleg/a")
    assert [m["role"] for m in child] == ["user", "assistant"], child
    assert child[1]["content"] == "answer for: Find answer A", child
    found = env.get_tools()["session_search"]["func"](query="answer for")
    assert found["matches"] >= 1, found
    print("[PASS] Subagent transcript persisted and searchable.")

    # Unbound delegation (no loop, hence no provider) must say so, not invent a result.
    unbound = await DelegationManager().delegate_task([{"goal": "anything"}])
    assert "error" in unbound, unbound
    # A duplicate id would otherwise silently overwrite another subtask.
    dup = await env.delegation.delegate_task([{"goal": "x", "task_id": "same"},
                                              {"goal": "y", "task_id": "same"}])
    assert "duplicate task_id" in dup["error"], dup
    # A role the swarm does not know must fail that subtask, not the whole call.
    bad_role = await env.delegation.delegate_task([{"goal": "Find answer A", "role": "wizard"}])
    assert bad_role["results"][0]["status"] == "failed", bad_role
    assert "unknown role" in bad_role["results"][0]["error"], bad_role
    print("[PASS] Unbound delegation, duplicate ids and unknown roles are refused.")

    # A subagent that never stops must stop at its token ceiling.
    env.delegation.token_budget = 30
    try:
        burned = await env.delegation.delegate_task([{"goal": "BURN", "task_id": "burn", "role": "coder"}])
        entry = burned["results"][0]
        assert entry["status"] == "failed", entry
        assert "Token budget of 30" in entry["error"], entry
        print("[PASS] Subagent token budget stops an unbounded subagent.")
    finally:
        env.delegation.token_budget = 60000

    for closer in ("memory_store", "state_db", "delivery_ledger"):
        if hasattr(env, closer):
            getattr(env, closer).close()
    if test_db.exists():
        test_db.unlink()

    print("ALL DELEGATION TESTS PASSED.")


if __name__ == "__main__":
    asyncio.run(test_delegation())
