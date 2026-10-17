---
name: git-workflow
description: "Git workflow helper: smart_commit() auto-generates commit messages from diffs, branch_status() shows branch state, pr_checklist() checks before creating a PR."
version: 1.0.0
author: Boom Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [git, commit, pull-request, workflow]
    related_skills: [code-review, test-writer]
---

# Git Workflow

A helper set that lets an agent follow the git process correctly without
memorizing every command: auto-name commits per conventional commits, quickly
view branch state, and run a 5-item checklist before opening a PR.

## When to use

- An agent just finished coding and needs to commit: `smart_commit()` generates a message from the diff
- Before pushing: `branch_status()` shows dirty files, how far ahead/behind
- Before creating a PR: `pr_checklist()` scans 5 commonly-forgotten conditions
- When the user says "commit this for me", "is this branch OK", "check before PR"

**Not for:** dangerous git operations (`reset --hard`, `push --force`,
deleting branches...) — the skill deliberately has no such functions.

## Input

- **repo:** path to the git repo (default `"."` — current directory)
- **smart_commit(repo, dry_run=True):** `dry_run=True` only returns the plan,
  `dry_run=False` commits for real

## Output

- `smart_commit()` → `CommitPlan{type, scope, message, files, staged}`
  or `None` when there is nothing to commit.
  Message format `type(scope): summary`, e.g. `feat(auth): add login, token refresh`
- `branch_status()` → `BranchStatus{branch, upstream, ahead, behind,
  staged, modified, untracked, clean}`
- `pr_checklist()` → `list[CheckItem{name, passed, detail, fix}]` — 5 items:
  1. Clean working tree (no uncommitted files)
  2. No commits directly on main/master
  3. Branch fully pushed to remote (ahead = 0)
  4. There are changes vs main (PR not empty)
  5. No conflict markers left (`<<<<<<<`)

## Example

### Python API

```python
import sys
sys.path.insert(0, "skills/git-workflow")  # point to the skill's directory
from workflow import smart_commit, branch_status, pr_checklist

# 1. Preview the commit message first (don't rush the commit)
plan = smart_commit("/path/to/repo")
print(plan.message)  # e.g.: "feat(auth): add login, token refresh"
print(plan.files, plan.staged)

# 2. Commit for real once satisfied
smart_commit("/path/to/repo", dry_run=False)

# 3. Branch status
st = branch_status(".")
print(st.branch, "ahead:", st.ahead, "behind:", st.behind, "clean:", st.clean)

# 4. Checklist before opening a PR
for item in pr_checklist("."):
    icon = "✓" if item.passed else "✗"
    print(icon, item.name, "-", item.detail)
    if not item.passed and item.fix:
        print("   Fix:", item.fix)
```

### CLI

```bash
# Preview commit message (dry-run by default)
python3 skills/git-workflow/workflow.py --repo . smart-commit

# Real commit
python3 skills/git-workflow/workflow.py --repo . smart-commit --no-dry-run

# Branch status / PR checklist
python3 skills/git-workflow/workflow.py --repo . branch-status
python3 skills/git-workflow/workflow.py --repo . pr-checklist
```

## Pitfalls

- `smart_commit()` prefers the **staged** diff; if nothing is staged but the tree
  is dirty, it uses the unstaged diff and will `git add` exactly those files on a
  real commit (never adds the whole repo blindly)
- Guessing `type` is a filename heuristic: new files → `feat`, all test files →
  `test`, all docs → `docs`, deletions only → `chore` — always review the message
  in dry-run mode before committing for real
- `pr_checklist()` item 4 needs a local `main`/`master` branch to compare against;
  without one it skips itself (passed=True with a note)
- Every failing git command (not a repo, git not installed) raises `GitError`
