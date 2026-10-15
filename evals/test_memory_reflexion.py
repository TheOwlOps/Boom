import shutil
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from memory.store import MemoryStore
from memory.reflexion import ReflexionEngine
from extensibility.skill_loader import SkillLoader

def test_closed_learning_loop():
    test_db = root_dir / "evals" / "test_memory.db"
    test_skills_dir = root_dir / "evals" / "test_skills"
    
    if test_db.exists():
        test_db.unlink()
    if test_skills_dir.exists():
        shutil.rmtree(test_skills_dir)
    test_skills_dir.mkdir(parents=True, exist_ok=True)

    store = MemoryStore(test_db)
    engine = ReflexionEngine(store, test_skills_dir)

    # 1. Learn a lesson and auto-synthesize SKILL.md
    topic = "Fix Rust E0308 Type Mismatch"
    problem = "Mismatched types between &str and String in tokio Command"
    solution = "Convert Vec<String> to Vec<&str> via iter().map(|s| s.as_str()).collect()"

    lesson_id = engine.extract_and_learn(topic, problem, solution, export_as_skill=True)
    assert lesson_id > 0

    # 2. Search from memory store
    results = store.search_lessons("Type Mismatch")
    assert len(results) == 1
    assert results[0]["topic"] == topic
    print("[PASS] SQLite Persistent Memory search verified.")

    # 3. Verify SkillLoader dynamically detects new skill
    loader = SkillLoader(test_skills_dir)
    skills = loader.list_skills()
    assert len(skills) == 1
    assert skills[0]["name"] == "fix-rust-e0308-type-mismatch"
    
    skill_content = loader.view_skill("fix-rust-e0308-type-mismatch")
    assert "Verified Solution" in skill_content
    print("[PASS] Reflexion Engine auto-generated SKILL.md and SkillLoader discovered it.")

    store.close()
    if test_db.exists():
        test_db.unlink()
    if test_skills_dir.exists():
        shutil.rmtree(test_skills_dir)
    print("ALL MEMORY & LEARNING LOOP TESTS PASSED.")

if __name__ == "__main__":
    test_closed_learning_loop()
