"""Offline tests for the feature-watch skill (stage at ~/workspace/boom-mods/feature-watch).

Covers, without cloning the repo:
  1. Skill dir exists with SKILL.md + YAML frontmatter (name=feature-watch,
     description non-empty) — the same convention SkillLoader enforces.
  2. skills/feature-watch/cron-example.json is valid JSON with a proper cron
     expression (5-field or @daily/@weekly), non-empty prompt, and a deliver_to
     dict with non-empty channel+target (matching _checked_deliver_to in
     cronjob_tool.py).

Run:  python3 evals/test_feature_watch.py   (from the module root)
"""

import json
import os
import re
import sys

MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(MODULE_ROOT, "skills", "feature-watch")
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
CRON_EXAMPLE = os.path.join(SKILL_DIR, "cron-example.json")


def parse_frontmatter(path):
    """Minimal YAML-frontmatter parser: returns dict of top-level keys.

    Handles the simple `key: value` scalars SkillLoader cares about
    (name, description, version). Enough to assert the skill convention.
    """
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert lines and lines[0].strip() == "---", "SKILL.md must start with ---"
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise AssertionError("SKILL.md frontmatter is not closed with ---")
    data = {}
    for line in lines[1:end]:
        if ":" not in line or line.startswith((" ", "\t")):
            continue  # only top-level scalar keys
        key, _, value = line.partition(":")
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        data[key.strip()] = value
    return data


def valid_cron(expr):
    """True if expr is a 5-field cron or a supported @macro."""
    if expr in ("@daily", "@weekly"):
        return True
    fields = expr.split()
    if len(fields) != 5:
        return False
    field_re = re.compile(
        r"^(\*|(\d+|\*)([\/\-\,](\d+|\*))*|\d+-\d+(\/\d+)?)$"
    )
    return all(field_re.match(f) for f in fields)


def test_skill_file():
    assert os.path.isdir(SKILL_DIR), f"skill dir missing: {SKILL_DIR}"
    assert os.path.isfile(SKILL_MD), f"SKILL.md missing: {SKILL_MD}"
    fm = parse_frontmatter(SKILL_MD)
    assert fm.get("name") == "feature-watch", f"bad frontmatter name: {fm!r}"
    assert fm.get("description"), "frontmatter description must be non-empty"
    assert fm.get("version"), "frontmatter version should be present"
    print("PASS: SKILL.md exists with valid frontmatter (name=feature-watch)")


def test_cron_example():
    with open(CRON_EXAMPLE, "r", encoding="utf-8") as f:
        raw = f.read()
    # tolerate leading // comment lines (jsonc-style)
    body = "\n".join(
        ln for ln in raw.splitlines() if not ln.lstrip().startswith("//")
    )
    cfg = json.loads(body)
    assert "cron" in cfg, "cron-example.json must contain 'cron'"
    assert valid_cron(cfg["cron"]), f"invalid cron expression: {cfg['cron']!r}"
    assert cfg.get("prompt"), "prompt must be non-empty"
    deliver = cfg.get("deliver_to")
    assert isinstance(deliver, dict), "deliver_to must be a dict"
    assert deliver.get("channel"), "deliver_to.channel must be non-empty"
    assert deliver.get("target"), "deliver_to.target must be non-empty"
    print("PASS: cron-example.json valid (cron, prompt, deliver_to)")


def main():
    test_skill_file()
    test_cron_example()
    print("ALL FEATURE-WATCH TESTS PASSED")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"FAIL: {e}")
        sys.exit(1)
