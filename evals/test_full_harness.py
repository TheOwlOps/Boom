import asyncio
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from environment import AgentEnvironment
from loop.conversation import ConversationRunner
from loop.react import ToolCall
from providers.llm import LLMResponse, LLMUsage, ToolCallSpec

async def test_full_harness():
    test_db = root_dir / "evals" / "harness_test.db"
    if test_db.exists():
        test_db.unlink()

    env = AgentEnvironment(root_dir=root_dir, db_path=test_db)
    tools = env.get_tools()

    # 1. Assert unified tools are present
    assert "read_file" in tools
    assert "write_file" in tools
    assert "patch_file" in tools
    assert "list_skills" in tools
    assert "sample_echo" in tools  # from plugins/sample-tool
    print("[PASS] Unified tools registry verified (Core + File + Skills + Plugins).")

    # 2. Test file tools
    sample_txt = root_dir / "evals" / "sample.txt"
    w_res = tools["write_file"]["func"](str(sample_txt), "line 1\nline 2\n")
    assert w_res["success"] is True

    r_res = tools["read_file"]["func"](str(sample_txt))
    assert r_res["total_lines"] == 2

    p_res = tools["patch_file"]["func"](str(sample_txt), "line 2", "line two")
    assert p_res["success"] is True

    r_res2 = tools["read_file"]["func"](str(sample_txt))
    assert "line two" in r_res2["content"]
    print("[PASS] File tools (read/write/patch) verified.")

    # 3. Test conversation runner drives a real model/tool loop
    class ScriptedLLM:
        """Returns one tool call, then a final answer once the tool result comes back."""

        def __init__(self):
            self.calls = []
            self.seen_tools_schema = None

        async def chat_complete(self, messages, tools_schema=None):
            self.calls.append(messages)
            self.seen_tools_schema = tools_schema
            if not any(m.get("role") == "tool" for m in messages):
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCallSpec(id="call_1", name="list_skills", arguments={})],
                    usage=LLMUsage(input_tokens=10, output_tokens=5),
                    finish_reason="tool_calls",
                )
            return LLMResponse(
                content="Skills listed.",
                usage=LLMUsage(input_tokens=20, output_tokens=7),
                finish_reason="stop",
            )

    llm = ScriptedLLM()
    runner = ConversationRunner(env, llm=llm)
    turn = await runner.run_turn("session-001", "Please test reading skills")
    assert turn.success is True
    assert len(turn.steps) == 1
    assert turn.steps[0].action.name == "list_skills"
    assert turn.response_text == "Skills listed."
    # The loop must have offered real function definitions to the provider.
    assert any(s["name"] == "list_skills" for s in (llm.seen_tools_schema or []))
    # Both round trips must be billed to the accountant.
    usage = env.token_accountant.get_usage("session-001")
    assert usage.input_tokens == 30, usage
    assert usage.output_tokens == 12, usage
    print("[PASS] Integrated ConversationRunner turn verified (real model/tool loop).")

    # 4. The turn must be durable: session_search reads what the loop actually wrote.
    persisted = env.state_db.get_session_messages("session-001")
    roles = [m["role"] for m in persisted]
    assert roles == ["user", "assistant", "tool", "assistant"], roles
    assert persisted[0]["content"] == "Please test reading skills"
    assert persisted[3]["content"] == "Skills listed."

    s_res = tools["session_search"]["func"](query="skills")
    assert s_res["mode"] == "discovery", s_res
    assert s_res["matches"] >= 1, s_res
    assert any(r["session_id"] == "session-001" for r in s_res["results"]), s_res

    # An FTS5 operator pasted into the query must not raise out of the tool.
    assert tools["session_search"]["func"](query='NEAR(" broken')["matches"] == 0
    print("[PASS] Transcript persistence + session_search verified.")

    # 5. The guardrail must screen a shell tool before it reaches the sandbox.
    refusal = await runner.react_loop.execute_step(
        ToolCall(name="sandbox_exec", arguments={"cmd": "rm", "args": ["-rf", "/"]})
    )
    assert refusal.startswith("Error: Command blocked by security policy"), refusal
    untouched = await runner.react_loop.execute_step(
        ToolCall(name="read_file", arguments={"path": str(sample_txt)})
    )
    assert "Command blocked" not in untouched, untouched
    print("[PASS] Guardrail screens shell tools before execution.")

    # 6. Compaction shrinks the live context without orphaning a tool_calls exchange.
    comp = env.compressor
    msgs = [
        {"role": "user", "content": "one"},
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 5000},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "ok"},
    ]
    out = comp.compact_messages(msgs, max_tokens=50)
    assert out[0]["role"] == "user", out
    assert out[-1]["role"] == "assistant", out
    assert not any(m["role"] == "tool" for m in out), out
    spilled = comp.micro_compact("y" * 5000)
    assert "characters omitted" in spilled and len(spilled) < 5000, len(spilled)
    print("[PASS] Context compaction verified (pairing preserved, large output spilled).")

    # Cleanup
    if hasattr(env, "memory_store"):
        env.memory_store.close()
    if hasattr(env, "state_db"):
        env.state_db.close()
    if hasattr(env, "delivery_ledger"):
        env.delivery_ledger.close()

    if sample_txt.exists():
        sample_txt.unlink()
    if test_db.exists():
        test_db.unlink()

    print("ALL FULL HARNESS TESTS PASSED.")

if __name__ == "__main__":
    asyncio.run(test_full_harness())
