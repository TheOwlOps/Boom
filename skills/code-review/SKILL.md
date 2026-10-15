---
name: code-review
description: "Review tĩnh code Python: complexity, dead code, bare except, print, TODO. Wrap codereview.py của Boom."
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

Review tĩnh file Python bằng AST thật (không regex mù), wrap module
`agent/src/autonomous/codereview.py` của Boom và bổ sung trường `file`
và `suggestion` (gợi ý sửa tiếng Việt) cho mỗi issue.

## Khi nào dùng

- Trước khi commit/push: kiểm tra code mới viết có vấn đề gì không
- Sau khi agent tự code xong một tính năng: tự review lại
- Review git diff: chỉ check các dòng thêm mới
- Khi user nói "review code", "kiểm tra code", "code này ổn không"

**Không dùng cho:** ngôn ngữ khác Python (skill chỉ hỗ trợ Python).

## Rules được check

| Rule | Severity | Ý nghĩa |
|------|----------|---------|
| `complexity` | warning | Cyclomatic complexity > 10 |
| `long_function` | warning | Hàm > 50 dòng |
| `long_file` | info | File > 500 dòng |
| `dead_code` | warning | Biến gán nhưng không dùng |
| `bare_except` | error | `except:` trần |
| `todo` | info | Comment TODO/FIXME |
| `print_stmt` | info | Dùng `print()` thay vì logging |
| `syntax` | error | Lỗi cú pháp |

## Input

- **Đường dẫn:** 1 file `.py`, 1 thư mục (quét đệ quy), hoặc nhiều file
- **Git diff:** text unified diff (chỉ check dòng thêm mới)
- **Source string:** đoạn code Python (qua API `review_source`)

## Output

List issues, mỗi issue có schema:

```json
[
  {
    "file": "agent/src/example.py",
    "line": 42,
    "severity": "error",
    "rule": "bare_except",
    "message": "`except:` trần bắt mọi exception...",
    "suggestion": "Bắt exception cụ thể, vd: except (ValueError, TypeError) as e:"
  }
]
```

## Ví dụ

### CLI

```bash
# Review 1 file, in markdown tiếng Việt
python3 skills/code-review/review.py agent/src/my_module.py

# Review cả thư mục, in JSON
python3 skills/code-review/review.py agent/src/ --json

# Review git diff
git diff HEAD~1 > thay_doi.diff
python3 skills/code-review/review.py --diff-file thay_doi.diff

# Fail CI nếu có error
python3 skills/code-review/review.py agent/src/ --fail-on error
```

### Python API

```python
from skills.code_review.review import review_file, review_source, issues_to_json

# Review 1 file
issues = review_file("agent/src/my_module.py")
for i in issues:
    print(i.severity, i.file, i.line, i.message)
    print("  Gợi ý:", i.suggestion)

# Review đoạn code trực tiếp
issues = review_source("try:\n    x = 1\nexcept:\n    pass\n", filename="demo.py")
print(issues_to_json(issues))
```

## Cấu hình

Biến môi trường `BOOM_REPO`: đường dẫn repo Boom chứa
`agent/src/autonomous/codereview.py` (mặc định: `/tmp/boom_git`).

## Pitfalls

- `review_diff` chỉ check được 3 rules theo dòng (todo, print_stmt, bare_except)
- Ngưỡng mặc định: complexity 10, hàm 50 dòng, file 500 dòng (lấy từ module gốc)
- File lỗi cú pháp → trả về 1 issue `syntax`, bỏ qua các rule khác
