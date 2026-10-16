"""Tests for the caching SkillLoader (stdlib only, run with plain `python3`).

Run:  python3 test_skill_cache.py
"""

import os
import sys
import tempfile
import time
from pathlib import Path

# Import root is agent/src (repo convention); the modules under test live
# in subpackages there (they previously lived next to this file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))
from extensibility.skill_loader import SkillLoader


def make_skill(base: Path, name: str, description: str) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\nBody v1.\n",
        encoding="utf-8",
    )
    return d


def setup_skills(tmp: Path):
    make_skill(tmp, "alpha", "Alpha skill")
    make_skill(tmp, "beta", "Beta skill")
    make_skill(tmp, "gamma", "Gamma skill")


def test1_list_cached_no_disk_rescan():
    """list_skills() 2 lan -> lan 2 dung cache, khong quet disk."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        setup_skills(tmp)
        loader = SkillLoader(tmp)

        scans = {"n": 0}
        orig_scan = loader._scan_skills

        def counting_scan():
            scans["n"] += 1
            return orig_scan()

        loader._scan_skills = counting_scan

        r1 = loader.list_skills()
        r2 = loader.list_skills()
        assert scans["n"] == 1, f"expected 1 disk scan, got {scans['n']}"
        assert [s["name"] for s in r1] == [s["name"] for s in r2]
        assert len(r1) == 3
        print("Test 1 PASS: second list_skills() served from cache (1 disk scan)")


def test2_ttl_expiry_rescans():
    """Het TTL (cache_ttl=1s) -> quet lai disk."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        setup_skills(tmp)
        loader = SkillLoader(tmp, cache_ttl=1.0)

        scans = {"n": 0}
        orig_scan = loader._scan_skills

        def counting_scan():
            scans["n"] += 1
            return orig_scan()

        loader._scan_skills = counting_scan

        loader.list_skills()
        assert scans["n"] == 1
        loader.list_skills()  # still within TTL
        assert scans["n"] == 1, f"expected still 1 scan within TTL, got {scans['n']}"
        time.sleep(1.2)
        loader.list_skills()  # TTL expired -> rescan
        assert scans["n"] == 2, f"expected 2 scans after TTL expiry, got {scans['n']}"
        print("Test 2 PASS: TTL expiry triggers a fresh disk scan")


def test3_view_skill_invalidate_on_mtime_change():
    """Sua SKILL.md (doi mtime) -> view_skill() tra noi dung moi."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        setup_skills(tmp)
        loader = SkillLoader(tmp)

        first = loader.view_skill("alpha")
        assert "Body v1" in first["content"], first

        # Modify the file and force a newer mtime (filesystem granularity safety).
        skill_md = tmp / "alpha" / "SKILL.md"
        skill_md.write_text(
            "---\nname: alpha\ndescription: Alpha skill\n---\n\n# alpha\nBody v2 UPDATED.\n",
            encoding="utf-8",
        )
        new_mtime = skill_md.stat().st_mtime_ns + 2_000_000_000  # +2s
        os.utime(skill_md, ns=(new_mtime, new_mtime))

        second = loader.view_skill("alpha")
        assert "Body v2 UPDATED" in second["content"], f"stale content: {second['content'][:80]}"
        print("Test 3 PASS: mtime change invalidates view_skill cache")


def test4_view_skill_reads_disk_once():
    """view_skill() 2 lan cung file khong doi -> chi doc disk 1 lan."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        setup_skills(tmp)
        loader = SkillLoader(tmp)

        reads = {"n": 0}
        orig_read = loader._read_text

        def counting_read(path):
            reads["n"] += 1
            return orig_read(path)

        loader._read_text = counting_read

        loader.view_skill("beta")
        loader.view_skill("beta")
        assert reads["n"] == 1, f"expected 1 disk read, got {reads['n']}"
        print("Test 4 PASS: repeated view_skill() hits cache (1 disk read)")


def test5_list_invalidate_on_dir_mtime_change():
    """Them skill moi (doi mtime skills_dir) -> list_skills() thay ngay, khong doi TTL."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        setup_skills(tmp)
        loader = SkillLoader(tmp, cache_ttl=3600.0)  # huge TTL, must not matter

        assert len(loader.list_skills()) == 3
        make_skill(tmp, "delta", "Delta skill")
        # Ensure the dir fingerprint changes (new subdir bumps parent mtime).
        time.sleep(0.02)
        names = [s["name"] for s in loader.list_skills()]
        assert "delta" in names, f"new skill not picked up: {names}"
        print("Test 5 PASS: skills_dir mtime change invalidates list cache before TTL")


def test6_api_compatibility():
    """Signature/API tuong thich: mac dinh cache_ttl=60, ket qua giong ban goc."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        setup_skills(tmp)
        loader = SkillLoader(tmp)  # original signature, no extra args
        assert loader.cache_ttl == 60.0
        skills = loader.list_skills()
        assert isinstance(skills, list) and all(
            set(s.keys()) == {"name", "description", "version"} for s in skills
        )
        v = loader.view_skill("gamma")
        assert v["name"] == "gamma" and "Body v1" in v["content"]
        assert "error" in loader.view_skill("nope")
        assert "error" in loader.view_skill("gamma", file_path="../../evil")
        print("Test 6 PASS: backward-compatible API (defaults, error paths, path traversal guard)")


if __name__ == "__main__":
    test1_list_cached_no_disk_rescan()
    test2_ttl_expiry_rescans()
    test3_view_skill_invalidate_on_mtime_change()
    test4_view_skill_reads_disk_once()
    test5_list_invalidate_on_dir_mtime_change()
    test6_api_compatibility()
    print("\nALL 6 TESTS PASSED")
