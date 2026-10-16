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


def _parse_version(v) -> Tuple[int, ...]:
    """Parse a version string into a comparable tuple of ints.

    Tolerates a leading "v" and trailing pre-release suffixes
    (``_parse_version("v1.2-beta") == (1, 2)``); empty/None -> ``(0,)``.
    """
    if not v:
        return (0,)
    s = str(v).strip()
    if s[:1].lower() == "v":
        s = s[1:]
    parts = []
    for piece in s.split("."):
        num = ""
        for ch in piece:
            if ch.isdigit():
                num += ch
            else:
                break
        if num:
            parts.append(int(num))
        else:
            break
    return tuple(parts) or (0,)


def _compare_versions(a, b) -> int:
    """Compare two version strings: -1 if a < b, 0 if equal, 1 if a > b.

    Tuples are zero-padded to equal length, so "1.2" == "1.2.0".
    """
    pa, pb = _parse_version(a), _parse_version(b)
    n = max(len(pa), len(pb))
    pa = pa + (0,) * (n - len(pa))
    pb = pb + (0,) * (n - len(pb))
    return (pa > pb) - (pa < pb)


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
    @staticmethod
    def _skill_version_of(skill_dir: Path) -> str:
        """Version declared by a skill directory's SKILL.md ("1.0.0" default)."""
        try:
            text = (Path(skill_dir) / "SKILL.md").read_text(encoding="utf-8")
        except OSError:
            return "1.0.0"
        frontmatter, _ = SkillsHub._split_frontmatter(text)
        return SkillsHub._frontmatter_field(frontmatter, "version") or "1.0.0"

    @staticmethod
    def _set_installed_name(skill_dir: Path, name: str) -> None:
        """Point the installed copy's frontmatter ``name:`` at the install name.

        A source directory may declare a different name (e.g. a "skill-a-v2"
        staging dir installed as "skill-a"); the installed skill is
        identified by the name it was installed under.
        """
        md_path = Path(skill_dir) / "SKILL.md"
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError:
            return
        frontmatter, body = SkillsHub._split_frontmatter(text)
        if not frontmatter:
            return
        lines = []
        replaced = False
        for line in frontmatter.splitlines():
            if ":" in line and line.partition(":")[0].strip().lower() == "name":
                lines.append(f'name: "{name}"')
                replaced = True
            else:
                lines.append(line)
        if not replaced:
            lines.insert(0, f'name: "{name}"')
        md_path.write_text("---\n" + "\n".join(lines) + "\n---\n" + body,
                           encoding="utf-8")

    def install_skill(self, skill_name: str, source_dir: Optional[Path] = None,
                      force: bool = False) -> Dict[str, Any]:
        """Install a skill, comparing versions on reinstall.

        Fresh install -> ``{"installed": True, "version": ..., "mode": "copied"}``.
        Reinstall with a source version older than (or equal to) the installed
        one -> refused with ``{"installed": False, "reason": "already_newer",
        "current_version": ..., "source_version": ...}`` unless ``force=True``.
        A newer source version upgrades the installed copy
        (``upgraded: True``); ``force=True`` also permits downgrades
        (``forced: True``). With no source available the call reports
        ``installed: False`` instead of scaffolding a placeholder skill.
        """
        src = Path(source_dir) if source_dir else (
            self.hub_catalog_dir / skill_name if self.hub_catalog_dir else None)
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

        source_version = self._skill_version_of(src)
        target = self.skills_dir / skill_name
        if target.exists():
            current_version = self._skill_version_of(target)
            cmp = _compare_versions(source_version, current_version)
            if cmp <= 0 and not force:
                return {
                    "installed": False,
                    "reason": "already_newer",
                    "current_version": current_version,
                    "source_version": source_version,
                }
            shutil.rmtree(target)
            shutil.copytree(src, target)
            self._set_installed_name(target, skill_name)
            result = {
                "installed": True,
                "name": skill_name,
                "version": source_version,
                "from_version": current_version,
                "to_version": source_version,
            }
            if cmp > 0:
                result["upgraded"] = True
            if force:
                result["forced"] = True
            return result

        shutil.copytree(src, target)
        self._set_installed_name(target, skill_name)
        return {"installed": True, "name": skill_name,
                "version": source_version, "mode": "copied"}
