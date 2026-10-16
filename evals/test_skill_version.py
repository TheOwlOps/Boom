"""Tests for skill versioning (skill_loader.py + skills_hub.py patches).

Chạy:  python3 test_skill_version.py
Yêu cầu: Python stdlib (+ PyYAML, do module gốc của Boom đã import yaml).
"""
import shutil
import sys
import tempfile
from pathlib import Path

# Import các module patch cùng thư mục
# Import root is agent/src (repo convention); the modules under test live
# in subpackages there (they previously lived next to this file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))
from extensibility.skill_loader import SkillLoader, extract_skill_version  # noqa: E402
from extensibility.skills_hub import (  # noqa: E402
    SkillsHub,
    _compare_versions,
    _parse_version,
)

PASSED = []
FAILED = []


def check(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print(f"  PASS: {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL: {name} {detail}")


def make_skill_md(version=None, name="demo-skill", description="Demo skill."):
    fm = f'name: "{name}"\ndescription: "{description}"\n'
    if version is not None:
        fm += f"version: {version}\n"
    return f"---\n{fm}---\n\n# {name}\n\nNội dung skill.\n"


def make_skill_dir(parent: Path, name: str, version=None) -> Path:
    d = parent / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(make_skill_md(version=version, name=name), encoding="utf-8")
    return d


# ---------------------------------------------------------------- Test 1
print("Test 1: SKILL.md có version: 2.1.0 -> list_skills() trả về version đúng")
with tempfile.TemporaryDirectory() as tmp:
    skills_dir = Path(tmp) / "skills"
    make_skill_dir(skills_dir, "myskill", version="2.1.0")
    loader = SkillLoader(skills_dir)
    skills = loader.list_skills()
    check("list_skills has 1 skill", len(skills) == 1, skills)
    check("version == '2.1.0'",
          skills and skills[0].get("version") == "2.1.0",
          f"got: {skills}")
    check("get_version('myskill') == '2.1.0'",
          loader.get_version("myskill") == "2.1.0")

# ---------------------------------------------------------------- Test 2
print("Test 2: SKILL.md không có version -> default '1.0.0'")
with tempfile.TemporaryDirectory() as tmp:
    skills_dir = Path(tmp) / "skills"
    make_skill_dir(skills_dir, "noversion", version=None)
    loader = SkillLoader(skills_dir)
    skills = loader.list_skills()
    check("version defaults to '1.0.0'",
          skills and skills[0].get("version") == "1.0.0",
          f"got: {skills}")
    check("get_version defaults to '1.0.0'",
          loader.get_version("noversion") == "1.0.0")
    check("get_version(missing) is None",
          loader.get_version("does-not-exist") is None)

# ------------------------------------------------- Tests 3, 4, 5, 6
print("Test 3-6: install version comparison")
with tempfile.TemporaryDirectory() as tmp:
    installed_dir = Path(tmp) / "installed"
    catalog_dir = Path(tmp) / "catalog"
    hub = SkillsHub(installed_dir, hub_catalog_dir=catalog_dir)

    # cài lần đầu: v1.0.0
    src_v1 = make_skill_dir(catalog_dir, "skill-a", version="1.0.0")
    r = hub.install_skill("skill-a", source_dir=src_v1)
    check("fresh install v1.0.0 -> installed=True",
          r.get("installed") is True and r.get("version") == "1.0.0", r)

    # Test 3: install lại đúng v1.0.0 -> bị từ chối already_newer
    r = hub.install_skill("skill-a", source_dir=src_v1)
    check("reinstall same v1.0.0 -> rejected",
          r.get("installed") is False and r.get("reason") == "already_newer"
          and r.get("current_version") == "1.0.0"
          and r.get("source_version") == "1.0.0", r)

    # Test 4: source v2.0.0 đè lên v1.0.0 -> thành công, upgraded=True
    src_v2 = make_skill_dir(catalog_dir, "skill-a-v2", version="2.0.0")
    r = hub.install_skill("skill-a", source_dir=src_v2)
    check("install v2.0.0 over v1.0.0 -> upgraded",
          r.get("installed") is True and r.get("upgraded") is True
          and r.get("from_version") == "1.0.0" and r.get("to_version") == "2.0.0", r)
    check("installed dir now holds v2.0.0",
          SkillLoader(installed_dir).get_version("skill-a") == "2.0.0")

    # Test 5: source v0.9.0 lên v2.0.0 -> bị từ chối
    src_old = make_skill_dir(catalog_dir, "skill-a-old", version="0.9.0")
    r = hub.install_skill("skill-a", source_dir=src_old)
    check("install v0.9.0 over v2.0.0 -> rejected already_newer",
          r.get("installed") is False and r.get("reason") == "already_newer"
          and r.get("current_version") == "2.0.0"
          and r.get("source_version") == "0.9.0", r)
    check("installed version unchanged after rejection",
          SkillLoader(installed_dir).get_version("skill-a") == "2.0.0")

    # Test 6: force=True đè được version cũ hơn
    r = hub.install_skill("skill-a", source_dir=src_old, force=True)
    check("force=True downgrades v2.0.0 -> v0.9.0",
          r.get("installed") is True and r.get("forced") is True
          and r.get("from_version") == "2.0.0" and r.get("to_version") == "0.9.0", r)
    check("installed dir now holds v0.9.0",
          SkillLoader(installed_dir).get_version("skill-a") == "0.9.0")

    # regression: install khi không có source -> lỗi cũ vẫn giữ
    r = hub.install_skill("ghost-skill", source_dir=Path(tmp) / "nope")
    check("missing source -> installed=False (backward-compat)",
          r.get("installed") is False and "No source" in r.get("error", ""), r)

# ---------------------------------------------------------------- Test 7
print("Test 7: version format lạ 'v1.2-beta' -> không crash")
try:
    check("_parse_version('v1.2-beta') == (1, 2)",
          _parse_version("v1.2-beta") == (1, 2), _parse_version("v1.2-beta"))
    check("_parse_version('') == (0,)", _parse_version("") == (0,))
    check("_parse_version(None) == (0,)", _parse_version(None) == (0,))
    check("_compare_versions('v1.2-beta', '1.2') == 0",
          _compare_versions("v1.2-beta", "1.2") == 0)
    check("_compare_versions('1.2', '1.2.0') == 0",
          _compare_versions("1.2", "1.2.0") == 0)
    check("_compare_versions('1.10.0', '1.9.9') == 1",
          _compare_versions("1.10.0", "1.9.9") == 1)
    check("_compare_versions('0.9.0', '1.0.0') == -1",
          _compare_versions("0.9.0", "1.0.0") == -1)
    check("extract_skill_version với 'v1.2-beta'",
          extract_skill_version(make_skill_md(version="v1.2-beta")) == "v1.2-beta")
except Exception as e:  # pragma: no cover
    check("no crash on weird versions", False, repr(e))

# install end-to-end với version lạ trong SKILL.md thật
with tempfile.TemporaryDirectory() as tmp:
    installed_dir = Path(tmp) / "installed"
    hub = SkillsHub(installed_dir)
    src_weird = make_skill_dir(Path(tmp) / "src", "weird", version="v1.2-beta")
    r = hub.install_skill("weird", source_dir=src_weird)
    check("install skill with 'v1.2-beta' -> ok",
          r.get("installed") is True and r.get("version") == "v1.2-beta", r)
    # cài lại cùng version lạ -> bị từ chối, không crash
    r = hub.install_skill("weird", source_dir=src_weird)
    check("reinstall 'v1.2-beta' -> already_newer, no crash",
          r.get("installed") is False and r.get("reason") == "already_newer", r)

print()
print(f"KẾT QUẢ: {len(PASSED)} passed, {len(FAILED)} failed")
if FAILED:
    print("FAILED:", FAILED)
    sys.exit(1)
print("ALL TESTS PASSED")
