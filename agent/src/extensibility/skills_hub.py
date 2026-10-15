import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

class SkillsHub:
    """Manages skill discovery, local catalog synchronization, and installation."""

    def __init__(self, skills_dir: Path, hub_catalog_dir: Optional[Path] = None):
        self.skills_dir = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.hub_catalog_dir = Path(hub_catalog_dir) if hub_catalog_dir else None

    def list_installed(self) -> List[str]:
        return [p.name for p in self.skills_dir.iterdir() if p.is_dir() and (p / "SKILL.md").exists()]

    def search_catalog(self, query: str) -> List[Dict[str, str]]:
        if not self.hub_catalog_dir or not self.hub_catalog_dir.exists():
            return []

        results = []
        q = query.lower()
        for p in self.hub_catalog_dir.iterdir():
            if p.is_dir() and (p / "SKILL.md").exists():
                name = p.name
                content = (p / "SKILL.md").read_text(encoding="utf-8")
                if q in name.lower() or q in content.lower():
                    results.append({"name": name, "preview": content[:120]})
        return results

    def install_skill(self, skill_name: str, source_dir: Optional[Path] = None) -> Dict[str, Any]:
        target = self.skills_dir / skill_name
        if target.exists():
            return {"installed": False, "error": f"Skill '{skill_name}' already exists"}

        src = source_dir or (self.hub_catalog_dir / skill_name if self.hub_catalog_dir else None)
        if not src or not src.exists():
            # No source to copy from. Report that instead of scaffolding a placeholder
            # skill, which would make list_skills advertise instructions nobody wrote.
            return {
                "installed": False,
                "error": (
                    f"No source for skill '{skill_name}'"
                    + (f" in catalog {self.hub_catalog_dir}" if self.hub_catalog_dir else " (no hub catalog configured)")
                ),
            }

        shutil.copytree(src, target)
        return {"installed": True, "name": skill_name, "mode": "copied"}
