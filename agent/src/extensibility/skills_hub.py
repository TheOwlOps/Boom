"""SkillsHub — skill discovery, local catalog sync, and installation.

This is the upgraded version of
``agent/src/extensibility/skills_hub.py`` from the Boom repo
(TheOwlOps/Boom): ``search_catalog()`` now ranks results with a
token-based scoring system instead of a plain substring match.

Everything else (``list_installed``, ``install_skill``) is unchanged.
"""

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Scoring weights: a query-token match in the skill name is worth more
# than one in the description, which is worth more than one in the body.
SCORE_NAME_MATCH = 3
SCORE_DESCRIPTION_MATCH = 2
SCORE_CONTENT_MATCH = 1
PREVIEW_LENGTH = 120


class SkillsHub:
    """Manages skill discovery, local catalog synchronization, and installation."""

    def __init__(self, skills_dir: Path, hub_catalog_dir: Optional[Path] = None):
        self.skills_dir = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.hub_catalog_dir = Path(hub_catalog_dir) if hub_catalog_dir else None

    def list_installed(self) -> List[str]:
        return [p.name for p in self.skills_dir.iterdir() if p.is_dir() and (p / "SKILL.md").exists()]

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    @staticmethod
    def _split_frontmatter(text: str) -> Tuple[str, str]:
        """Split SKILL.md into (frontmatter, body).

        Frontmatter is the YAML-ish block between the leading ``---`` lines.
        If there is none, returns ("", text).
        """
        lines = text.splitlines()
        if len(lines) >= 2 and lines[0].strip() == "---":
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
        return "", text

    @staticmethod
    def _frontmatter_field(frontmatter: str, field: str) -> str:
        """Return the value of a ``field: value`` line in the frontmatter."""
        for line in frontmatter.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            if key.strip().lower() == field.lower():
                return value.strip()
        return ""

    def _score_skill(self, tokens: List[str], name: str, description: str, content: str) -> int:
        """Token-based relevance score.

        Each query token is scored independently and summed:
          token in skill name        -> +3
          token in frontmatter desc  -> +2
          token in SKILL.md body     -> +1
        All matching is case-insensitive. Presence per (token, field) is
        counted once — a token appearing five times in the body still
        contributes +1 for that token.
        """
        name_l = name.lower()
        desc_l = description.lower()
        body_l = content.lower()
        score = 0
        for tok in tokens:
            if tok in name_l:
                score += SCORE_NAME_MATCH
            if tok in desc_l:
                score += SCORE_DESCRIPTION_MATCH
            if tok in body_l:
                score += SCORE_CONTENT_MATCH
        return score

    def search_catalog(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search the hub catalog, ranked by relevance score (highest first).

        Args:
            query: Search query. Multi-word queries are split into tokens;
                each token scores independently and the scores are summed.
            limit: Maximum number of results to return (default 10).

        Returns:
            List of ``{"name": ..., "score": ..., "preview": ...}`` dicts,
            sorted by score descending, name ascending as a stable
            tie-break. Skills with a score of 0 are excluded.
            An empty query returns [] (same as the old implementation).
        """
        if not self.hub_catalog_dir or not self.hub_catalog_dir.exists():
            return []

        tokens = [t for t in query.lower().split() if t]
        if not tokens:
            return []

        results = []
        for p in self.hub_catalog_dir.iterdir():
            if not (p.is_dir() and (p / "SKILL.md").exists()):
                continue
            name = p.name
            content = (p / "SKILL.md").read_text(encoding="utf-8")
            frontmatter, body = self._split_frontmatter(content)
            description = self._frontmatter_field(frontmatter, "description")
            score = self._score_skill(tokens, name, description, body)
            if score > 0:
                results.append(
                    {
                        "name": name,
                        "score": score,
                        "preview": content[:PREVIEW_LENGTH],
                    }
                )

        results.sort(key=lambda r: (-r["score"], r["name"]))
        return results[:limit]

    # ------------------------------------------------------------------
    # Install
    # ------------------------------------------------------------------
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
