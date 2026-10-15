import asyncio
import json
import shutil
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from security.guardrail import SecurityGuardrail
from loop.context_compressor import ContextCompressor
from loop.trajectory import TrajectoryRecorder
from tools.delegation import DelegationManager
from tools.code_execution import CodeExecutionKernel
from tools.web_tools import WebTools, build_tools as build_web_tools

async def test_all_advanced_features():
    # 1. Test SecurityGuardrail
    guard = SecurityGuardrail(root_dir)
    ok1, reason1 = guard.check_command("rm -rf /")
    assert ok1 is False
    assert "dangerous" in reason1

    ok2, _ = guard.check_command("ls -la")
    assert ok2 is True
    print("[PASS] SecurityGuardrail verified.")

    # 2. Test ContextCompressor micro_compact
    scratch_dir = root_dir / "evals" / "test_scratch"
    compressor = ContextCompressor(scratch_dir=scratch_dir)
    huge_text = "A" * 3000
    compacted = compressor.micro_compact(huge_text, max_chars=1000)
    assert len(compacted) < 3000
    assert "omitted" in compacted
    assert "full output saved to" in compacted
    print("[PASS] ContextCompressor micro_compact verified.")

    # 3. Test TrajectoryRecorder
    traj_dir = root_dir / "evals" / "test_trajectories"
    recorder = TrajectoryRecorder(session_id="test-session-123", log_dir=traj_dir)
    recorder.record_step("Thinking about bug", "read_file", {"path": "main.py"}, "File content")
    json_path = recorder.export_json()
    assert json_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["total_steps"] == 1
    assert data["session_id"] == "test-session-123"
    print("[PASS] TrajectoryRecorder export verified.")

    # 4. Test DelegationManager — a real subagent needs a bound loop and provider, so an
    #    unbound manager must refuse. Its behaviour with a runtime lives in test_delegation.py.
    delegator = DelegationManager()
    res = await delegator.delegate_task([
        {"goal": "Investigate DB leak", "context": "Error in auth.py"},
        {"goal": "Refactor frontend component", "context": "Table.tsx needs pagination"}
    ])
    assert delegator.available is False
    assert "error" in res, res
    print("[PASS] DelegationManager refuses without a subagent runtime.")

    # 5. Test CodeExecutionKernel (Meta-tool calling)
    bound_tools = {
        "square": {
            "name": "square",
            "func": lambda n: n * n
        }
    }
    kernel = CodeExecutionKernel(bound_tools)
    code = """
result = square(9)
print(f"Calculated square: {result}")
"""
    exec_res = kernel.execute(code)
    assert exec_res["exit_code"] == 0
    assert "Calculated square: 81" in exec_res["stdout"]
    print("[PASS] CodeExecutionKernel meta execution verified.")

    # 6. Test WebTools. No network here: search hits DuckDuckGo and extract fetches a URL, and
    #    both have their parsing covered against a local server in evals/test_web_tools.py.
    web_tools = build_web_tools()
    assert {"web_search", "web_extract"} <= set(web_tools), sorted(web_tools)
    print("[PASS] WebTools registration verified (offline).")

    # Cleanup test artifacts
    if scratch_dir.exists():
        shutil.rmtree(scratch_dir)
    if traj_dir.exists():
        shutil.rmtree(traj_dir)

    print("ALL ADVANCED FEATURES TESTS PASSED.")

if __name__ == "__main__":
    asyncio.run(test_all_advanced_features())
