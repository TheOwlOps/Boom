---
name: cron-manager
description: "Manage cron jobs: add/list/remove (stored as JSON) + validate cron expressions, supporting @daily/@hourly and 5-field forms like '*/5 * * * *'."
version: 1.0.0
author: Boom Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [cron, scheduler, automation, jobs]
    related_skills: [productivity, devops]
---

# Cron Manager

Manage an agent's list of cron jobs (stored in a JSON file) with a hand-written
cron expression validator — no extra installs needed, works even when the
machine has no cron daemon running.

## When to use

- The user says "run this every morning", "schedule a daily backup"
- An agent needs to validate a cron expression before writing it to a real crontab
- Save/load a list of scheduled jobs so the agent remembers them across sessions
- When the user asks "when does this job run" → use `describe()` to answer

**Not for:** writing directly to the system crontab — this skill only manages
the list + validation; to actually install, run `crontab -e` yourself or use
Boom's cron API.

## Supported cron expressions

| Form | Example | Meaning |
|------|-------|---------|
| Alias | `@daily`, `@hourly` | `@reboot`, `@yearly`, `@monthly`, `@weekly`, `@daily`, `@hourly` |
| 5 fields | `*/5 * * * *` | `minute hour day month weekday` |
| Step | `*/15`, `0 */2 * * *` | every 15 minutes / every 2 hours |
| Range | `9-17 * * * *` | from 9am to 5pm |
| List | `0 9,18 * * *` | at 9am and 6pm |
| Names | `0 9 * * mon-fri` | Mon–Fri at 9am (`jan`–`dec`, `sun`–`sat`) |

Value ranges: minute 0–59, hour 0–23, day 1–31, month 1–12, weekday 0–7
(7 = Sunday, normalized to 0).

## Input

- **add(name, schedule, command, store):** job name (only `a-z 0-9 _ . -`),
  schedule (validated), command (the command to run)
- **list_jobs(store):** no input needed
- **remove(name, store):** job name
- **parse_cron(expr) / describe(expr):** cron string
- **store:** path to the JSON file (default `~/.boom/cron_jobs.json`)

## Output

- `parse_cron()` → `CronExpression{minute, hour, day, month, weekday, raw, alias}`
  (each field is a normalized list of ints); invalid input → raises `CronError`
- `add()` → `CronJob{name, schedule, command, created_at, enabled}`; duplicate
  name or invalid schedule → raises `CronError`
- `list_jobs()` → `list[CronJob]`
- `remove()` → `True` (deleted) / `False` (not found)
- `describe()` → human-readable string, e.g. `"every 5 minutes"`

## Example

### Python API

```python
import sys
sys.path.insert(0, "skills/cron-manager")  # point to the skill's directory
from manager import add, list_jobs, remove, parse_cron, describe

# Validate before doing anything else
c = parse_cron("*/5 * * * *")
print(c.minute)          # [0, 5, 10, ..., 55]
print(describe("@daily"))  # "once a day (00:00)"

# Add a job (schedule auto-validated, duplicate name raises)
job = add("backup-db", "0 2 * * *", "python3 /opt/boom/backup.py")
print(job.created_at)

# List / delete
for j in list_jobs():
    print(j.name, "->", describe(j.schedule))
remove("backup-db")
```

### CLI

```bash
# Add a job
python3 skills/cron-manager/manager.py add backup-db "0 2 * * *" "python3 backup.py"

# List
python3 skills/cron-manager/manager.py list

# Delete
python3 skills/cron-manager/manager.py remove backup-db

# Only validate + show description
python3 skills/cron-manager/manager.py validate "*/5 * * * *"
```

## Pitfalls

- This skill **does not** write to the system crontab — `add()` only stores into
  the JSON file for the agent to manage; installing the schedule for real is a
  separate step decided by the user/agent
- `@reboot` parses fine but has no 5 time fields (the lists are empty)
- Job names are limited to safe characters to avoid injection when concatenated into a crontab
- Malformed JSON file → explicit `CronError` instead of a silent crash
