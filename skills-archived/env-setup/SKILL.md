---
name: env-setup
description: "Check the dev environment: Python version, installed packages, required env vars, free ports. Standard output [{check, status, fix_command}]."
version: 1.0.0
author: Boom Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [environment, setup, diagnostics, onboarding]
    related_skills: [deployer, devops]
---

# Env Setup

A "health check" for the dev environment before running/building a project: is
Python new enough, which packages are missing, which env vars were forgotten,
is a port taken. Every failing check comes with a `fix_command` so the agent
(or user) can fix it right away.

## When to use

- Before running a freshly cloned project: "can this machine run it?"
- Debugging "module not found", "port already in use", "missing env var"
- Onboarding: generate an environment checklist for new contributors
- An agent self-checking before starting the gateway/worker

**Not for:** bulk automatic package installation — this skill only **checks and
suggests fix commands**; deciding to run them is up to the agent/user.

## Checks performed

| Check | How it checks | Example `fix_command` on failure |
|-------|---------------|------------------------------|
| Python version | `sys.version_info >= min_version` | `sudo apt install python3.12` |
| deps | `importlib.util.find_spec(pkg)` | `pip install httpx` |
| env vars | `os.environ` is set and non-empty | `export API_KEY=<value>` |
| ports | try `bind(127.0.0.1:port)` | `lsof -ti :8000 \| xargs kill -9` |

## Input

- **EnvConfig{min_python, deps, env_vars, ports}** — e.g.
  `EnvConfig(min_python="3.10", deps=["httpx", "pydantic"],
  env_vars=["OPENAI_API_KEY"], ports=[8000, 8788])`
- Or call individually: `check_python("3.10")`, `check_deps([...])`,
  `check_env_vars([...])`, `check_ports([...])`

## Output

`list[CheckResult]`, each item with a fixed schema:

```json
[
  {"check": "dep:httpx", "status": "ok", "detail": "installed, importable", "fix_command": ""},
  {"check": "port:8000", "status": "fail", "detail": "taken by another process", "fix_command": "lsof -ti :8000 | xargs kill -9  # or switch to another port"}
]
```

- `status`: `"ok"` | `"fail"` | `"warn"`
- `fix_command` is empty when already ok — the agent only needs to look at failing items
- `all_ok(results)` → `True` when every item is ok

## Example

### Python API

```python
import sys
sys.path.insert(0, "skills/env-setup")  # point to the skill's directory
from setup import EnvConfig, run_all, all_ok, results_to_json

config = EnvConfig(
    min_python="3.10",
    deps=["httpx", "pydantic"],
    env_vars=["OPENAI_API_KEY"],
    ports=[8000, 8788],
)
results = run_all(config)
print(results_to_json(results))

if not all_ok(results):
    for r in results:
        if r.status != "ok":
            print(f"Needs fixing [{r.check}]: {r.fix_command}")
```

### CLI

```bash
# Quick check, print table
python3 skills/env-setup/setup.py --min-python 3.10 \
  --dep httpx --dep pydantic \
  --env OPENAI_API_KEY \
  --port 8000 --port 8788

# Print JSON for other tools to read (exit code 1 if any item fails)
python3 skills/env-setup/setup.py --dep httpx --port 8000 --json
```

## Pitfalls

- Port checking **tries to bind**: there is a tiny race condition (the port may
  be grabbed by another process right after the check) — good enough for diagnostics
- `check_deps` uses `find_spec`: a package that is installed but fails to import
  (missing sub-dependency) still counts as fail — correctly meaning "not usable yet"
- An env var only counts as ok when it is **set and non-empty**
- On Windows, the suggested `lsof` in `fix_command` doesn't work — use
  `netstat -ano | findstr :PORT` then `taskkill /PID ...` instead
