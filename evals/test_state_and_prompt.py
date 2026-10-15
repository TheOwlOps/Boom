import sys
import time
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from memory.state_db import StateDB
from loop.prompt_builder import SystemPromptBuilder
from loop.token_accounting import TokenAccountant
from tools.process_manager import ProcessManager
from tools.browser_tool import BrowserTool

def test_hermes_parity_components():
    # 1. Test StateDB with FTS5
    test_db = root_dir / "evals" / "test_state.db"
    if test_db.exists():
        test_db.unlink()

    db = StateDB(test_db)
    db.create_session("sess-1", model="claude-3-7-sonnet")
    db.append_message("sess-1", "user", "How do I fix memory leaks in Rust?")
    db.append_message("sess-1", "assistant", "Use Arc and weak references to avoid cycles.")
    db.record_timeline_event("sess-1", "turn_completed", {"status": "ok"})

    # Search via FTS5
    fts_results = db.search_fts("memory leaks")
    assert len(fts_results) >= 1
    assert "Rust" in fts_results[0]["content"]
    print("[PASS] StateDB SQLite WAL & FTS5 full-text search verified.")

    # 1b. The event log has a reader, and retention drops exactly what is older than the cutoff.
    db.record_timeline_event("sess-1", "delegated", {"role": "critic"})
    events = db.read_timeline("sess-1")
    assert [e["event_type"] for e in events] == ["delegated", "turn_completed"], events
    assert events[0]["data"] == {"role": "critic"}, events
    assert db.read_timeline("sess-1", event_type="turn_completed")[0]["data"] == {"status": "ok"}
    assert db.read_timeline("no-such-session") == []

    stale = time.time() - 40 * 86400
    with db.conn:
        db.conn.execute("UPDATE messages SET timestamp = ? WHERE role = 'user'", (stale,))
        db.conn.execute("UPDATE timeline SET timestamp = ? WHERE event_type = 'delegated'", (stale,))
    removed = db.prune(30)
    assert removed == {"messages": 1, "timeline": 1, "sessions": 0}, removed
    assert db.search_fts("memory leaks") == [], "the FTS index must not outlive the message"
    assert [e["event_type"] for e in db.read_timeline("sess-1")] == ["turn_completed"]
    assert db.get_session_messages("sess-1")[0]["role"] == "assistant"
    assert db.prune(0) == {"messages": 0, "timeline": 0, "sessions": 0}, "0 keeps everything"
    print("[PASS] Timeline read back and retention pruned 1 message + 1 event, index included.")

    # 2. Test SystemPromptBuilder
    builder = SystemPromptBuilder(root_dir)
    skills = [{"name": "ast-refactor", "description": "AST structural search"}]
    prompt_tg = builder.build_system_prompt("telegram", available_skills=skills)
    assert "ultra-terse" in prompt_tg
    assert "ast-refactor" in prompt_tg

    prompt_zalo = builder.build_system_prompt("zalo", available_skills=skills)
    assert "LỄ PHÉP" in prompt_zalo
    print("[PASS] SystemPromptBuilder platform tone & skills block verified.")

    # 3. Test ProcessManager
    proc_json = root_dir / "evals" / "test_processes.json"
    if proc_json.exists():
        proc_json.unlink()

    pm = ProcessManager(proc_json)
    spawn = pm.start_process("sleep-test", ["python", "-c", "import time; time.sleep(1)"])
    assert spawn["status"] == "running"
    
    status = pm.poll_process("sleep-test")
    assert status["status"] in ["running", "exited"]
    
    kill_res = pm.kill_process("sleep-test")
    assert kill_res["status"] in ["killed", "not_found"]
    print("[PASS] ProcessManager daemon ledger verified.")

    # 4. Test TokenAccountant
    acc = TokenAccountant()
    acc.record_usage("sess-1", input_tokens=1000, output_tokens=500, cache_read_tokens=2000)
    cost = acc.calculate_cost("sess-1", model="claude-3-7-sonnet")
    assert cost > 0
    print(f"[PASS] TokenAccountant cost tracking verified: ${cost}")

    # 5. Test BrowserTool. It drives a real Chrome over CDP now, so a box with no browser or
    #    no network must report an error rather than a success: either outcome is accepted, and
    #    what is asserted is that a navigation that did happen really loaded a page. The frame
    #    codec and the CDP call path are covered offline in evals/test_browser_cdp.py.
    browser = BrowserTool()
    try:
        nav = browser.goto_url("https://hermes-agent.nousresearch.com")
        if "error" in nav:
            print(f"[PASS] BrowserTool reported no browser/network: {nav['error'][:60]}")
        else:
            assert nav["status"] == "navigated", nav
            info = browser.page_info()
            assert info["state"] in ("complete", "interactive"), info
            print("[PASS] BrowserTool navigation verified.")
    finally:
        browser.close()

    # Cleanup
    db.close()
    if test_db.exists():
        test_db.unlink()
        for wal in [test_db.with_suffix(".db-wal"), test_db.with_suffix(".db-shm")]:
            if wal.exists():
                wal.unlink()
    if proc_json.exists():
        proc_json.unlink()

    print("ALL HERMES PARITY COMPONENT TESTS PASSED.")

if __name__ == "__main__":
    test_hermes_parity_components()
