---
name: deployer
description: "Pre-deploy checklist tự động: tests pass, no secrets, version bumped, CHANGELOG, env vars, backup plan."
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

## Tên
`deployer`

## Mô tả
Chạy bộ kiểm tra tự động trước khi deploy: test có pass không, code có dính secret không, version đã bump chưa, CHANGELOG có cập nhật không, biến môi trường có được document không, và đã có kế hoạch backup/rollback chưa. Kết quả là danh sách các check với trạng thái `pass` / `fail` / `warn`.

## Khi nào dùng
- Trước mọi lần deploy lên staging/production
- Trước khi tag release hoặc merge vào `main`
- Khi cần gate tự động trong CI: fail nếu có check nào `fail`

## Input
- `project_dir` (str): thư mục gốc của project cần kiểm tra
- `test_command` (str, optional): lệnh chạy test, mặc định `python3 -m pytest -q`
- `version_file` (str, optional): file chứa version (mặc định tự phát hiện `pyproject.toml`, `package.json`, `VERSION`)

## Output
List các dict: `{check, status, detail}`, với `status` ∈ `pass | fail | warn`.

Ví dụ:

```python
[
  {"check": "tests_pass",        "status": "pass", "detail": "pytest: 42 passed"},
  {"check": "no_secrets",        "status": "fail", "detail": "phát hiện AWS key ở config.py:17"},
  {"check": "version_bumped",    "status": "warn", "detail": "không có git tag để đối chiếu"},
  {"check": "changelog_updated", "status": "pass", "detail": "CHANGELOG.md thay đổi trong 5 commit gần nhất"},
  {"check": "env_documented",    "status": "pass", "detail": "12/12 biến môi trường có trong .env.example"},
  {"check": "backup_plan",       "status": "warn", "detail": "không tìm thấy script backup/rollback"},
]
```

Quy ước: **có bất kỳ check nào `fail` → KHÔNG deploy.**

## Ví dụ

```python
from deployer.checklist import run_checklist

results = run_checklist("/srv/myapp")
for r in results:
    print(f"[{r['status']:4}] {r['check']}: {r['detail']}")

if any(r["status"] == "fail" for r in results):
    raise SystemExit("BLOCKED: có check fail, không deploy")
```
