import sys
import time
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))
sys.path.insert(0, str(root_dir))  # gateway/ lives at the repo root, next to agent/

from tools.fuzzy_patch import fuzzy_find_and_replace
from memory.curated_memory import CuratedMemory
from gateway.delivery_ledger import DeliveryLedger
from extensibility.skills_hub import SkillsHub
from memory.state_db import StateDB
from tools.session_search import SessionSearchTool

def test_deep_parity():
    # 1. Fuzzy patch testing
    sample = "line 1\r\nline 2    \r\nline 3\r\n"
    # Target has different line endings and extra spaces
    updated, matches, strat, err = fuzzy_find_and_replace(sample, "line 2", "line 2_replaced")
    assert err is None
    assert matches == 1
    assert "line 2_replaced" in updated
    print(f"[PASS] Fuzzy patch verified via strategy: {strat}")

    # 2. Curated memory testing
    mem_dir = root_dir / "evals" / "test_memories"
    cm = CuratedMemory(mem_dir)
    # Add
    res1 = cm.execute_operations("user", [{"action": "add", "content": "Ryan: Lead Architect"}])
    assert res1["success"] is True
    # Replace
    res2 = cm.execute_operations("user", [{"action": "replace", "old_text": "Ryan:", "content": "Ryan: Sếp & Creator"}])
    assert res2["success"] is True
    entries = cm.read_entries("user")
    assert "Sếp & Creator" in entries[0]
    # Remove
    res3 = cm.execute_operations("user", [{"action": "remove", "old_text": "Sếp & Creator"}])
    assert res3["success"] is True
    assert len(cm.read_entries("user")) == 0
    print("[PASS] CuratedMemory atomic operations and character budget verified.")

    # 3. Delivery ledger testing
    ledger_db = root_dir / "evals" / "test_delivery.db"
    if ledger_db.exists():
        ledger_db.unlink()
    dl = DeliveryLedger(ledger_db)
    ob_id = "ob_12345"
    assert dl.record_obligation(ob_id, "telegram", "chat_99", "hello sếp") is True
    # Duplicate insertion refused
    assert dl.record_obligation(ob_id, "telegram", "chat_99", "hello sếp") is False
    dl.mark_attempting(ob_id)
    assert dl.is_delivered(ob_id) is False
    dl.mark_delivered(ob_id)
    assert dl.is_delivered(ob_id) is True
    dl.close()
    if ledger_db.exists():
        ledger_db.unlink()
    print("[PASS] DeliveryLedger idempotency and duplicate suppression verified.")

    # 4. Skills Hub testing
    hub_dir = root_dir / "evals" / "test_skills_hub"
    sh = SkillsHub(hub_dir)
    # With no catalog and no source there is nothing to install; it must say so rather
    # than scaffold a placeholder SKILL.md that list_skills would then advertise.
    assert sh.install_skill("code-reviewer")["installed"] is False
    src_skill = root_dir / "evals" / "test_skill_src" / "code-reviewer"
    src_skill.mkdir(parents=True, exist_ok=True)
    (src_skill / "SKILL.md").write_text("---\nname: code-reviewer\n---\n# Review\n", encoding="utf-8")
    inst = sh.install_skill("code-reviewer", source_dir=src_skill)
    assert inst["installed"] is True, inst
    assert inst["mode"] == "copied", inst
    assert "code-reviewer" in sh.list_installed()
    print("[PASS] SkillsHub installation verified (copies a real source, refuses without one).")

    # 5. Session search testing
    state_db_file = root_dir / "evals" / "test_search_state.db"
    if state_db_file.exists():
        state_db_file.unlink()
    s_db = StateDB(state_db_file)
    s_db.create_session("sess_alpha", model="deepseek-coder")
    mid1 = s_db.append_message("sess_alpha", "user", "Configure NGINX reverse proxy for gateway")
    mid2 = s_db.append_message("sess_alpha", "assistant", "Here is the nginx.conf upstream configuration")
    
    search_tool = SessionSearchTool(s_db)
    # Mode 1: Discovery
    disc = search_tool.search(query="NGINX")
    assert disc["mode"] == "discovery"
    assert disc["matches"] >= 1
    # Mode 2: Read
    read_res = search_tool.search(session_id="sess_alpha")
    assert read_res["mode"] == "read"
    assert read_res["count"] == 2
    # Mode 3: Scroll
    scroll_res = search_tool.search(session_id="sess_alpha", around_message_id=mid1)
    assert scroll_res["mode"] == "scroll"
    assert len(scroll_res["messages"]) == 2

    s_db.close()
    if state_db_file.exists():
        state_db_file.unlink()
    print("[PASS] SessionSearchTool (Discovery, Read, Scroll) verified.")

    # Cleanup temp dirs
    import shutil
    if mem_dir.exists():
        shutil.rmtree(mem_dir)
    if hub_dir.exists():
        shutil.rmtree(hub_dir)

    print("ALL DEEP PARITY TESTS PASSED.")

if __name__ == "__main__":
    test_deep_parity()
