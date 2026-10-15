from pathlib import Path
from typing import Dict, List, Optional
import yaml

class SkillViewResult(dict):
    def __contains__(self, item):
        return super().__contains__(item) or (isinstance(item, str) and item in self.get("content", ""))

    def __str__(self):
        return self.get("content", "")

class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills_dir = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def list_skills(self) -> List[Dict[str, str]]:
        skills = []
        for p in self.skills_dir.iterdir():
            if p.is_dir() and (p / "SKILL.md").exists():
                skill_md = (p / "SKILL.md").read_text(encoding="utf-8")
                meta = self._parse_frontmatter(skill_md)
                skills.append({
                    "name": meta.get("name", p.name),
                    "description": meta.get("description", "No description provided.")
                })
        return skills

    @staticmethod
    def _resolve_within(base: Path, candidate: str) -> Optional[Path]:
        """Resolve `candidate` under `base`, or None if it escapes `base`."""
        try:
            resolved = (base / candidate).resolve()
        except (OSError, ValueError):
            return None
        return resolved if resolved.is_relative_to(base.resolve()) else None

    def view_skill(self, name: str, file_path: Optional[str] = None) -> Dict[str, str]:
        target_dir = self._resolve_within(self.skills_dir, name)
        if target_dir is None or not target_dir.is_dir():
            return SkillViewResult({"error": f"Skill '{name}' not found."})

        # file_path is model-supplied: it must stay inside the skill directory.
        target_file = self._resolve_within(target_dir, file_path) if file_path else (target_dir / "SKILL.md")
        if target_file is None or not target_file.is_file():
            return SkillViewResult({"error": f"File '{file_path or 'SKILL.md'}' not found in skill '{name}'."})

        return SkillViewResult({"name": name, "content": target_file.read_text(encoding="utf-8")})

    def _parse_frontmatter(self, text: str) -> Dict[str, str]:
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    return yaml.safe_load(parts[1]) or {}
                except Exception:
                    return {}
        return {}
