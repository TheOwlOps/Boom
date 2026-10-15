import re
from pathlib import Path
from typing import Optional
from memory.store import MemoryStore

class ReflexionEngine:
    def __init__(self, memory_store: MemoryStore, skills_dir: Path):
        self.store = memory_store
        self.skills_dir = Path(skills_dir)

    def extract_and_learn(
        self,
        topic: str,
        problem: str,
        solution: str,
        export_as_skill: bool = False
    ) -> int:
        """Persist learned lesson and optionally synthesize a SKILL.md file."""
        lesson_id = self.store.save_lesson(topic, problem, solution)

        if export_as_skill:
            slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
            target_dir = self.skills_dir / slug
            target_dir.mkdir(parents=True, exist_ok=True)
            skill_file = target_dir / "SKILL.md"

            content = f"""---
name: {slug}
description: "Autonomously learned skill for: {topic}"
---

# {topic}

## Problem Observed
{problem}

## Verified Solution
{solution}
"""
            skill_file.write_text(content, encoding="utf-8")

        return lesson_id
