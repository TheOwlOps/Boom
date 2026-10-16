---
name: security-audit
description: "Security audit of Python code: hardcoded secrets, dangerous functions (eval/exec/os.system), SQL injection, missing input validation."
version: 1.0.0
author: Boom Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [security, audit, secrets, injection, sast]
    related_skills: [code-review, test-writer]
---

# Security Audit

Static security audit for Python code, combining:

1. **Boom's `agent/src/security/secretscan.py`** (loaded read-only from the repo):
   scans for hardcoded secrets — AWS keys, GitHub tokens, Telegram bot tokens,
   PEM private keys, directly-assigned password/api_key/api_secret.
2. **Boom's `agent/src/security/sanitize.py`**: detects string literals
   containing prompt injection patterns (useful when auditing agent code).
3. **Hand-written AST checks**: dangerous functions, SQL string concatenation, missing input validation.

## When to use

- Before committing/pushing code that handles sensitive data
- Periodic codebase audits, or before making a repo public
- When the user says "security audit", "check secrets", "does this code have vulnerabilities"
- Reviewing code that uses `eval`, `os.system`, DB queries, reads user input

**Not for:** runtime monitoring (this skill only does static analysis);
non-Python languages.

## Input

- **Path:** 1 `.py` file or 1 directory (recursive scan, skipping
  `__pycache__`, `.venv`, `.git`, `node_modules`)

## Output

Report — a list of findings, each with this schema:

```json
[
  {
    "severity": "high",
    "file": "agent/src/config.py",
    "line": 12,
    "category": "hardcoded_secret",
    "issue": "Hardcoded secret detected (api_key, preview: sk-t****)",
    "fix": "Move the secret to an environment variable (os.environ.get(...)) or a secret manager; ..."
  }
]
```

| Category | Severity | Meaning |
|----------|----------|---------|
| `hardcoded_secret` | high | Secret assigned directly in code |
| `dangerous_function` | high/medium | `eval`/`exec`, `os.system`, `subprocess(shell=True)`, `pickle.loads`, `yaml.load` |
| `sql_injection` | high | SQL queries built with f-strings/string concatenation/format |
| `missing_validation` | medium | Parameters flow straight into a dangerous sink without validation |
| `prompt_injection_string` | low | String contains a prompt injection pattern |

## Example

### CLI

```bash
# Audit 1 file, print markdown
python3 skills/security-audit/audit.py agent/src/config.py

# Audit a whole directory, print JSON (for CI to parse)
python3 skills/security-audit/audit.py agent/src/ --json

# Fail CI if there is a high finding
python3 skills/security-audit/audit.py agent/src/ --fail-on high
```

### Python API

```python
from skills.security_audit.audit import audit_file, audit_source, to_markdown

findings = audit_file("agent/src/config.py")
for f in findings:
    print(f.severity, f.file, f.line, f.issue)
    print("  Fix:", f.fix)

# Audit a code snippet directly
code = 'api_key = "sk-test-1234567890abcdef"\neval(user_input)\n'
print(to_markdown(audit_source(code, filename="demo.py")))
```

## Whitelist (inherited from secretscan)

No findings reported when: reading from `os.environ.get(...)`, placeholders
(`***`, `xxx`, `<redacted>`), or lines with the `# nosec-test` marker.

## Configuration

Environment variable `BOOM_REPO`: path to the Boom repo containing
`agent/src/security/secretscan.py` and `sanitize.py` (default: `/tmp/boom_git`).

## Pitfalls

- Static analysis can have false positives (e.g. `eval` used with safe internal
  input) — verify manually before fixing
- `missing_validation` only detects parameters reaching a sink via dynamic
  strings (f-string/`+`/`%`/`.format`); sinks receiving plain variables are not flagged
- Not a replacement for pentests/manual review of sensitive systems
