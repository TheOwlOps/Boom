---
name: code-review
description: "Statically review Python code: complexity, dead code, bare except, print, TODO. Wraps Boom's codereview.py."
version: 1.0.0
author: Boom Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [code-review, static-analysis, quality, ast]
    related_skills: [security-audit, test-writer]
---

# Code Review

Statically review Python files using real AST (no blind regex), wrapping Boom's
`agent/src/autonomous/codereview.py` module and adding `file` and `suggestion`
(fix suggestion) fields to each issue.

## When to use

- Before commit/push: check whether newly written code has problems
- After an agent finishes coding a feature: review it again
- Review git diff: check only added lines
- When the user says "review code", "check the code", "is this code OK"

**Not for:** languages other than Python (this skill only supports Python).

## Rules checked

| Rule | Severity | Meaning |
|------|----------|---------|
| `complexity` | warning | Cyclomatic complexity > 10 |
| `long_function` | warning | Function > 50 lines |
| `long_file` | info | File > 500 lines |
| `dead_code` | warning | Variable assigned but never used |
| `bare_except` | error | Bare `except:` |
| `todo` | info | TODO/FIXME comment |
| `print_stmt` | info | Uses `print()` instead of logging |
| `syntax` | error | Syntax error |

## Input

- **Path:** 1 `.py` file, 1 directory (recursive scan), or multiple files
- **Git diff:** unified diff text (checks only added lines)
- **Source string:** a Python code snippet (via the `review_source` API)

## Output

List of issues, each with this schema:

```json
[
  {
    "file": "agent/src/example.py",
    "line": 42,
    "severity": "error",
    "rule": "bare_except",
    "message": "Bare `except:` catches all exceptions...",
    "suggestion": "Catch specific exceptions, e.g.: except (ValueError, TypeError) as e:"
  }
]
```

## Example

### CLI

```bash
# Review 1 file, print markdown
python3 skills/code-review/review.py agent/src/my_module.py

# Review a whole directory, print JSON
python3 skills/code-review/review.py agent/src/ --json

# Review git diff
git diff HEAD~1 > changes.diff
python3 skills/code-review/review.py --diff-file changes.diff

# Fail CI if there is an error
python3 skills/code-review/review.py agent/src/ --fail-on error
```

### Python API

```python
from skills.code_review.review import review_file, review_source, issues_to_json

# Review 1 file
issues = review_file("agent/src/my_module.py")
for i in issues:
    print(i.severity, i.file, i.line, i.message)
    print("  Suggestion:", i.suggestion)

# Review a code snippet directly
issues = review_source("try:\n    x = 1\nexcept:\n    pass\n", filename="demo.py")
print(issues_to_json(issues))
```

## Configuration

Environment variable `BOOM_REPO`: path to the Boom repo containing
`agent/src/autonomous/codereview.py` (default: `/tmp/boom_git`).

## Pitfalls

- `review_diff` can only check 3 line-based rules (todo, print_stmt, bare_except)
- Default thresholds: complexity 10, 50 lines per function, 500 lines per file (taken from the original module)
- A file with a syntax error → returns 1 `syntax` issue, all other rules are skipped
