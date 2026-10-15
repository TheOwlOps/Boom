---
name: security-audit
description: "Kiểm toán bảo mật code Python: hardcoded secret, hàm nguy hiểm (eval/exec/os.system), SQL injection, thiếu validate input."
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

Kiểm toán bảo mật tĩnh cho code Python, kết hợp:

1. **`agent/src/security/secretscan.py` của Boom** (load read-only từ repo):
   quét hardcoded secret — AWS key, GitHub token, Telegram bot token,
   private key PEM, password/api_key/api_secret gán trực tiếp.
2. **`agent/src/security/sanitize.py` của Boom**: phát hiện string literal
   chứa pattern prompt injection (hữu ích khi audit code của agent).
3. **AST check tự viết**: hàm nguy hiểm, SQL string concat, thiếu validate input.

## Khi nào dùng

- Trước khi commit/push code xử lý dữ liệu nhạy cảm
- Audit định kỳ codebase, hoặc trước khi public repo
- Khi user nói "audit bảo mật", "check secret", "code này có lỗ hổng không"
- Review code có dùng `eval`, `os.system`, query DB, đọc input người dùng

**Không dùng cho:** runtime monitoring (skill chỉ phân tích tĩnh);
ngôn ngữ khác Python.

## Input

- **Đường dẫn:** 1 file `.py` hoặc 1 thư mục (quét đệ quy, bỏ qua
  `__pycache__`, `.venv`, `.git`, `node_modules`)

## Output

Report — list findings, mỗi finding có schema:

```json
[
  {
    "severity": "high",
    "file": "agent/src/config.py",
    "line": 12,
    "category": "hardcoded_secret",
    "issue": "Phát hiện secret dạng hardcode (api_key, xem trước: sk-t****)",
    "fix": "Đưa secret ra biến môi trường (os.environ.get(...)) hoặc secret manager; ..."
  }
]
```

| Category | Severity | Ý nghĩa |
|----------|----------|---------|
| `hardcoded_secret` | high | Secret gán trực tiếp trong code |
| `dangerous_function` | high/medium | `eval`/`exec`, `os.system`, `subprocess(shell=True)`, `pickle.loads`, `yaml.load` |
| `sql_injection` | high | Query SQL xây bằng f-string/nối chuỗi/format |
| `missing_validation` | medium | Tham số đi thẳng vào sink nguy hiểm mà không validate |
| `prompt_injection_string` | low | String chứa pattern prompt injection |

## Ví dụ

### CLI

```bash
# Audit 1 file, in markdown tiếng Việt
python3 skills/security-audit/audit.py agent/src/config.py

# Audit cả thư mục, in JSON (để CI parse)
python3 skills/security-audit/audit.py agent/src/ --json

# Fail CI nếu có finding high
python3 skills/security-audit/audit.py agent/src/ --fail-on high
```

### Python API

```python
from skills.security_audit.audit import audit_file, audit_source, to_markdown

findings = audit_file("agent/src/config.py")
for f in findings:
    print(f.severity, f.file, f.line, f.issue)
    print("  Sửa:", f.fix)

# Audit đoạn code trực tiếp
code = 'api_key = "sk-test-1234567890abcdef"\neval(user_input)\n'
print(to_markdown(audit_source(code, filename="demo.py")))
```

## Whitelist (kế thừa từ secretscan)

Không báo lỗi khi: đọc từ `os.environ.get(...)`, placeholder
(`***`, `xxx`, `<redacted>`), hoặc dòng có marker `# nosec-test`.

## Cấu hình

Biến môi trường `BOOM_REPO`: đường dẫn repo Boom chứa
`agent/src/security/secretscan.py` và `sanitize.py` (mặc định: `/tmp/boom_git`).

## Pitfalls

- Phân tích tĩnh có thể có false positive (vd: `eval` dùng với input nội bộ
  an toàn) — kiểm tra thủ công trước khi sửa
- `missing_validation` chỉ phát hiện được khi tham số đi vào sink qua chuỗi
  động (f-string/`+`/`%`/`.format`); sink nhận biến thuần thì không báo
- Không thay thế pentest/review thủ công cho hệ thống nhạy cảm
