import asyncio
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))
sys.path.insert(0, str(root_dir / "agent" / "src"))

from acp_adapter import AcpServer
from environment import AgentEnvironment
from loop.react import ReActLoop
from providers.llm import LLMResponse, LLMUsage

class ScriptedLLM:
    """One final answer, no tool calls: this path only needs a real reply back."""

    model = "scripted"

    async def chat_complete(self, messages, tools_schema=None):
        return LLMResponse(
            content="Auth middleware refactored.",
            usage=LLMUsage(input_tokens=5, output_tokens=3),
            finish_reason="stop",
        )

async def test_acp_adapter():
    test_db = root_dir / "evals" / "acp_test.db"
    if test_db.exists():
        test_db.unlink()

    env = AgentEnvironment(root_dir=root_dir, db_path=test_db)
    server = AcpServer(react_loop=ReActLoop(env, ScriptedLLM(), session_id="acp_sess_1"))

    # 1. Initialize
    init_req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    res = await server.handle_request(init_req)
    assert res["result"]["serverInfo"]["name"] == "harness-acp-adapter"
    print("[PASS] ACP initialize verified.")

    # 2. Session new
    new_req = {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"model": "claude-3-7"}}
    res = await server.handle_request(new_req)
    session_id = res["result"]["sessionId"]
    assert session_id is not None
    print("[PASS] ACP session/new verified.")

    # 3. Session prompt must return the agent's own answer.
    prompt_req = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "session/prompt",
        "params": {"sessionId": session_id, "prompt": "Refactor auth middleware"}
    }
    res = await server.handle_request(prompt_req)
    assert res["result"]["reply"] == "Auth middleware refactored.", res
    assert res["result"]["success"] is True, res
    print("[PASS] ACP session/prompt runs the real agent loop.")

    # 4. Session list
    list_req = {"jsonrpc": "2.0", "id": 4, "method": "session/list", "params": {}}
    res = await server.handle_request(list_req)
    assert len(res["result"]["sessions"]) == 1
    assert res["result"]["sessions"][0]["messages"] == 2
    print("[PASS] ACP session/list verified.")

    # 5. Without a loop the server must refuse, not fabricate a reply.
    bare = AcpServer()
    await bare.handle_request(init_req)
    await bare.handle_request(new_req)
    missing = await bare.handle_request(prompt_req)
    assert "reply" not in missing, missing
    assert "No agent loop" in missing["error"]["message"], missing
    print("[PASS] ACP refuses a prompt with no agent loop attached.")

    for attr in ("memory_store", "state_db", "delivery_ledger"):
        getattr(env, attr).close()
    if test_db.exists():
        test_db.unlink()

    print("ALL ACP ADAPTER TESTS PASSED.")

if __name__ == "__main__":
    asyncio.run(test_acp_adapter())
