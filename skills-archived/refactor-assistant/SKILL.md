---
name: refactor-assistant
description: "Safe refactor assistant: rename symbols, extract functions, move symbols with real AST, always dry-run preview the diff before applying."
version: 1.0.0
author: Boom
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [refactor, ast, rename, extract-function, code-quality]
    related_skills: [software-development/simplify-code, software-development/systematic-debugging]
---

# Refactor-Assistant — Safe Refactor Assistant

## Name
`refactor-assistant`

## Description
Wraps `RefactorEngine` (AST-based, from `agent/src/autonomous/refactor.py`, vendored into the skill) as a friendly API for the agent. Supports 3 operations:

- **rename_symbol**: rename a variable/function/class across multiple files
- **extract_function**: extract a code block into a new function (params/return inferred automatically)
- **move_symbol**: move a top-level function/class to another file

Every operation has a **dry-run preview diff before applying**, automatic backups (`.bak-<timestamp>`), syntax validation after edits, and rollback on error.

## When to use
- Renaming a symbol where manual grep/regex is easy to miss or over-rename
- Splitting a long function into smaller ones
- Moving code between files when restructuring modules
- When refactoring in bulk but still wanting to see the diff first

## Input
- `root_dir` (str): project root directory
- Operations:
  - `rename(old_name, new_name, files)` — files: list of relative paths
  - `extract(path, start_line, end_line, new_name)`
  - `move(src, dst, symbol_name)`
- Every operation takes `preview=True` (default) to only show the diff.

## Output
Result dict from the engine: `{ok, dry_run, diffs/diff, occurrences, params, returns, backups, ...}`. When `ok=False`, `error` describes the reason.

## Example

```python
from refactor_assistant.assistant import RefactorAssistant

ra = RefactorAssistant("/srv/myapp")

# 1) Preview first — no files written
p = ra.rename("old_name", "new_name", ["a.py", "b.py"], preview=True)
print(p["diffs"]["a.py"])   # review the diff

# 2) Apply for real once OK (with backup + syntax validation)
r = ra.rename("old_name", "new_name", ["a.py", "b.py"], preview=False)
assert r["ok"]

# 3) Extract a function
e = ra.extract("a.py", 10, 25, "calculate_total", preview=True)
print("params:", e["params"], "returns:", e["returns"])
```
