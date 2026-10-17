---
name: deployer
description: "Automated pre-deploy checklist: tests pass, no secrets, version bumped, CHANGELOG, env vars, backup plan."
version: 1.0.0
author: Boom
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [deploy, checklist, devops, release, quality-gate]
    related_skills: [software-development/test-driven-development, software-development/systematic-debugging]
---

# Deployer — Pre-deploy Checklist

## Name
`deployer`

## Description
Runs an automated check suite before deployment: do tests pass, is the code free of secrets, has the version been bumped, is the CHANGELOG updated, are environment variables documented, and is there a backup/rollback plan. The result is a list of checks with status `pass` / `fail` / `warn`.

## When to use
- Before every deploy to staging/production
- Before tagging a release or merging into `main`
- When you need an automatic gate in CI: fail if any check is `fail`

## Input
- `project_dir` (str): root directory of the project to check
- `test_command` (str, optional): test command, default `python3 -m pytest -q`
- `version_file` (str, optional): file containing the version (auto-detects `pyproject.toml`, `package.json`, `VERSION` by default)

## Output
A list of dicts: `{check, status, detail}`, with `status` ∈ `pass | fail | warn`.

Example:

```python
[
  {"check": "tests_pass",        "status": "pass", "detail": "pytest: 42 passed"},
  {"check": "no_secrets",        "status": "fail", "detail": "found AWS key in config.py:17"},
  {"check": "version_bumped",    "status": "warn", "detail": "no git tag to compare against"},
  {"check": "changelog_updated", "status": "pass", "detail": "CHANGELOG.md changed in the last 5 commits"},
  {"check": "env_documented",    "status": "pass", "detail": "12/12 env vars present in .env.example"},
  {"check": "backup_plan",       "status": "warn", "detail": "no backup/rollback script found"},
]
```

Convention: **any check with status `fail` → DO NOT deploy.**

## Example

```python
from deployer.checklist import run_checklist

results = run_checklist("/srv/myapp")
for r in results:
    print(f"[{r['status']:4}] {r['check']}: {r['detail']}")

if any(r["status"] == "fail" for r in results):
    raise SystemExit("BLOCKED: a check failed, will not deploy")
```
