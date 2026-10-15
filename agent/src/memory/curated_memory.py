"""Curated Memory Engine managing MEMORY.md and USER.md with character budgets and atomic batch operations."""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MEMORY_CHAR_LIMIT = 2200
USER_CHAR_LIMIT = 1375
ENTRY_DELIMITER = "\n§\n"

class CuratedMemory:
    def __init__(self, memory_dir: Path):
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.user_file = self.memory_dir / "USER.md"

        if not self.memory_file.exists():
            self.memory_file.write_text("", encoding="utf-8")
        if not self.user_file.exists():
            self.user_file.write_text("", encoding="utf-8")

    def _get_target_file(self, target: str) -> Tuple[Path, int]:
        if target.lower() == "user":
            return self.user_file, USER_CHAR_LIMIT
        return self.memory_file, MEMORY_CHAR_LIMIT

    def read_entries(self, target: str = "memory") -> List[str]:
        f, _ = self._get_target_file(target)
        raw = f.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]

    def get_snapshot(self) -> Dict[str, str]:
        """Frozen snapshot to inject into system prompt at session start."""
        return {
            "memory": self.memory_file.read_text(encoding="utf-8").strip(),
            "user": self.user_file.read_text(encoding="utf-8").strip(),
        }

    def execute_operations(self, target: str, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Applies batch operations atomically: add, replace, remove."""
        target_file, limit = self._get_target_file(target)
        entries = self.read_entries(target)

        for op in operations:
            action = op.get("action")
            content = op.get("content", "").strip()
            old_text = op.get("old_text", "").strip()

            if action == "add":
                if not content:
                    return {"success": False, "error": "Content required for 'add'"}
                entries.append(content)

            elif action == "remove":
                if not old_text:
                    return {"success": False, "error": "old_text required for 'remove'"}
                matched = [i for i, e in enumerate(entries) if old_text in e]
                if not matched:
                    return {"success": False, "error": f"Entry matching '{old_text}' not found"}
                del entries[matched[0]]

            elif action == "replace":
                if not old_text or not content:
                    return {"success": False, "error": "old_text and content required for 'replace'"}
                matched = [i for i, e in enumerate(entries) if old_text in e]
                if not matched:
                    return {"success": False, "error": f"Entry matching '{old_text}' not found"}
                entries[matched[0]] = content

            else:
                return {"success": False, "error": f"Unknown action '{action}'"}

        # Check final character budget
        new_text = ENTRY_DELIMITER.join(entries)
        if len(new_text) > limit:
            return {
                "success": False,
                "error": f"Character limit exceeded ({len(new_text)}/{limit} chars). Consolidate entries.",
                "current_length": len(new_text),
                "limit": limit
            }

        target_file.write_text(new_text, encoding="utf-8")
        return {
            "success": True,
            "target": target,
            "entries_count": len(entries),
            "current_length": len(new_text),
            "limit": limit
        }
