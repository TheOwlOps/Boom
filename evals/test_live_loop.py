"""End-to-end check of the live ReAct turn against a real OpenAI-compatible endpoint.

Set BOOM_TEST_BASE_URL / BOOM_TEST_API_KEY / BOOM_TEST_MODEL (or OPENAI_BASE_URL /
OPENAI_API_KEY / BOOM_MODEL). Exits with a SKIP notice when none are configured.
"""

import asyncio
import os
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from environment import AgentEnvironment
from loop.react import ReActLoop
from providers.llm import LLMProvider

async def test_live_turn():
    base_url = os.getenv("BOOM_TEST_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("BOOM_TEST_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("BOOM_TEST_MODEL") or os.getenv("BOOM_MODEL")

    if not (base_url and api_key and model):
        print("[SKIP] Set BOOM_TEST_BASE_URL, BOOM_TEST_API_KEY and BOOM_TEST_MODEL to run the live loop test.")
        return

    test_db = root_dir / "evals" / "live_loop_test.db"
    probe = root_dir / "evals" / "live_probe.txt"
    if test_db.exists():
        test_db.unlink()

    # Unattended: the model writes a probe file, and the approval gate denies mutating
    # tools when there is no terminal to prompt from.
    env = AgentEnvironment(root_dir=root_dir, db_path=test_db, yolo=True)
    llm = LLMProvider(base_url=base_url, api_key=api_key, model=model, timeout=180.0)
    loop = ReActLoop(
        env,
        llm,
        max_turns=6,
        accountant=env.token_accountant,
        session_id="live-test",
    )

    try:
        probe.write_text("LIVE_PROBE_OK\n", encoding="utf-8")

        result = await loop.step(
            f"Read the file {probe.as_posix()} with the read_file tool, "
            "then reply with its exact contents and nothing else."
        )

        assert result["success"] is True, result
        tool_names = [s["action"]["name"] for s in result["trace"] if s.get("action")]
        assert tool_names, f"model never called a tool: {result}"
        assert "read_file" in tool_names, f"expected read_file, got {tool_names}"
        assert "LIVE_PROBE_OK" in result["action"], result["action"]

        usage = env.token_accountant.get_usage("live-test")
        assert usage.input_tokens > 0, usage
        assert usage.output_tokens > 0, usage
        cost = env.token_accountant.calculate_cost("live-test", model)

        print(
            f"[PASS] Live turn verified against {base_url} ({model}).\n"
            f"       tools called: {tool_names}\n"
            f"       tokens: in {usage.input_tokens} / out {usage.output_tokens}"
            f" / reasoning {usage.reasoning_tokens} / cache read {usage.cache_read_tokens}\n"
            f"       cost: ${cost:.6f}\n"
            f"       answer: {result['action'].strip()[:120]}"
        )
    finally:
        for closer in ("memory_store", "state_db", "delivery_ledger"):
            if hasattr(env, closer):
                getattr(env, closer).close()
        if probe.exists():
            probe.unlink()
        if test_db.exists():
            test_db.unlink()

if __name__ == "__main__":
    asyncio.run(test_live_turn())
