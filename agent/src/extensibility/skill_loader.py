"""SkillLoader with in-memory caching.

Cache behavior (all stdlib, thread-safe):

- ``list_skills()``: results are cached for ``cache_ttl`` seconds (default 60).
  The cache is also invalidated early when the mtime of ``skills_dir`` (or any
  direct sub-directory) changes, so newly added / removed / modified skills
  are picked up without waiting for the TTL to expire.
- ``view_skill()``: file contents are cached per ``{skill_name: {file_path: (mtime, content)}}``.
  On every read the current file mtime is compared against the cached mtime;
  a mismatch triggers a fresh disk read.

API is fully backward compatible with the original SkillLoader.
"""

from pathlib import Path
from typing import Dict, List, Optional
import threading
import time

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


class SkillViewResult(dict):
    def __contains__(self, item):
        return super().__contains__(item) or (isinstance(item, str) and item in self.get("content", ""))

    def __str__(self):
        return self.get("content", "")


def extract_skill_version(skill_md_text: str) -> Optional[str]:
    """Extract the ``version:`` field from SKILL.md frontmatter.

    Returns the version string (e.g. "2.1.0", "v1.2-beta"), or None when
    the document has no frontmatter version field. Pure stdlib — does not
    depend on yaml, so it also works when PyYAML is not installed.
    """
    if not skill_md_text:
        return None
    text = skill_md_text.strip()
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip().lower() == "version":
            v = value.strip().strip("\"'")
            return v or None
    return None


class SkillLoader:
    def __init__(self, skills_dir: Path, cache_ttl: float = 60.0):
        self.skills_dir = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.cache_ttl = float(cache_ttl)

        # Cache state, guarded by _lock.
        self._lock = threading.Lock()
        # list_skills cache: (timestamp_monotonic, dir_mtime_ns, results)
        self._list_cache: Optional[tuple] = None
        # view_skill cache: {skill_name: {resolved_path_str: (mtime_ns, content)}}
        self._file_cache: Dict[str, Dict[str, tuple]] = {}

    # ------------------------------------------------------------------
    # list_skills with TTL + mtime invalidation
    # ------------------------------------------------------------------

    def list_skills(self) -> List[Dict[str, str]]:
        dir_mtime = self._dir_mtime_ns()
        now = time.monotonic()
        with self._lock:
            cached = self._list_cache
            if (
                cached is not None
                and (now - cached[0]) < self.cache_ttl
                and cached[1] == dir_mtime
            ):
                return cached[2]
        # Cache miss: scan disk (outside the lock so readers are not blocked).
        skills = self._scan_skills()
        with self._lock:
            self._list_cache = (time.monotonic(), self._dir_mtime_ns(), skills)
        return skills

    def _scan_skills(self) -> List[Dict[str, str]]:
        """Full disk scan of skills_dir (the expensive part)."""
        skills = []
        for p in self.skills_dir.iterdir():
            if p.is_dir() and (p / "SKILL.md").exists():
                skill_md = self._read_text(p / "SKILL.md")
                meta = self._parse_frontmatter(skill_md)
                skills.append({
                    "name": meta.get("name", p.name),
                    "description": meta.get("description", "No description provided."),
                    "version": meta.get("version", "1.0.0"),
                })
        return skills

    def get_version(self, name: str) -> Optional[str]:
        """Get version of a skill by name.

        Returns the skill's version ("1.0.0" when the skill exists but its
        SKILL.md declares no version), or None when no such skill exists.
        """
        for s in self.list_skills():
            if s["name"] == name:
                return s.get("version", "1.0.0")
        return None

    def _dir_mtime_ns(self) -> int:
        """Fingerprint of skills_dir: max mtime of the dir and its direct subdirs."""
        mtimes = []
        try:
            mtimes.append(self.skills_dir.stat().st_mtime_ns)
            for p in self.skills_dir.iterdir():
                if p.is_dir():
                    try:
                        mtimes.append(p.stat().st_mtime_ns)
                    except OSError:
                        pass
        except OSError:
            pass
        return max(mtimes) if mtimes else 0

    def _invalidate_list_cache(self) -> None:
        with self._lock:
            self._list_cache = None

    # ------------------------------------------------------------------
    # view_skill with per-file mtime-checked cache
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_within(base: Path, candidate: str) -> Optional[Path]:
        """Resolve `candidate` under `base`, or None if it escapes `base`."""
        try:
            resolved = (base / candidate).resolve()
        except (OSError, ValueError):
            return None
        return resolved if resolved.is_relative_to(base.resolve()) else None

    def _read_text(self, path: Path) -> str:
        """Single place where file bytes hit disk (patchable for tests)."""
        return path.read_text(encoding="utf-8")

    def _read_cached(self, skill_name: str, target_file: Path) -> str:
        """Return file content, re-reading from disk only if mtime changed."""
        key = str(target_file)
        try:
            current_mtime = target_file.stat().st_mtime_ns
        except OSError:
            current_mtime = None
        with self._lock:
            entry = self._file_cache.get(skill_name, {}).get(key)
            if entry is not None and entry[0] == current_mtime:
                return entry[1]
        # Miss or stale: read from disk.
        content = self._read_text(target_file)
        try:
            fresh_mtime = target_file.stat().st_mtime_ns
        except OSError:
            fresh_mtime = current_mtime
        with self._lock:
            self._file_cache.setdefault(skill_name, {})[key] = (fresh_mtime, content)
        return content

    def view_skill(self, name: str, file_path: Optional[str] = None) -> Dict[str, str]:
        target_dir = self._resolve_within(self.skills_dir, name)
        if target_dir is None or not target_dir.is_dir():
            return SkillViewResult({"error": f"Skill '{name}' not found."})

        # file_path is model-supplied: it must stay inside the skill directory.
        target_file = self._resolve_within(target_dir, file_path) if file_path else (target_dir / "SKILL.md")
        if target_file is None or not target_file.is_file():
            return SkillViewResult({"error": f"File '{file_path or 'SKILL.md'}' not found in skill '{name}'."})

        # A failed read should not poison the cache.
        try:
            content = self._read_cached(name, target_file)
        except OSError as e:
            return SkillViewResult({"error": f"Could not read file '{file_path or 'SKILL.md'}': {e}"})
        return SkillViewResult({"name": name, "content": content})

    def _parse_frontmatter(self, text: str) -> Dict[str, str]:
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3 and yaml is not None:
                try:
                    return yaml.safe_load(parts[1]) or {}
                except Exception:
                    return {}
        return {}
