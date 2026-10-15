import sys
from pathlib import Path

# Add agent/src to sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from extensibility.skill_loader import SkillLoader
from extensibility.plugin_registry import PluginRegistry

def test_skills_loader():
    skills_dir = root_dir / "skills"
    loader = SkillLoader(skills_dir)
    skills = loader.list_skills()
    assert len(skills) >= 1, "Should discover at least 1 skill"
    assert skills[0]["name"] == "ast-refactor"
    assert "AST-based" in skills[0]["description"]
    
    res = loader.view_skill("ast-refactor")
    assert res is not None
    text = res["content"] if isinstance(res, dict) else res
    assert "AST Refactor Skill" in text
    print("[PASS] SkillLoader verified.")

def test_plugin_registry():
    plugins_dir = root_dir / "plugins"
    registry = PluginRegistry(plugins_dir)
    ctx = registry.scan_and_load()
    assert "sample-tool" in registry.loaded_plugins
    assert "sample_echo" in ctx.tools
    assert ctx.tools["sample_echo"]["func"]("ping") == "echo: ping"
    print("[PASS] PluginRegistry verified.")

if __name__ == "__main__":
    test_skills_loader()
    test_plugin_registry()
    print("ALL TESTS PASSED.")
