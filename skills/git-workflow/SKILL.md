---
name: git-workflow
description: "Git workflow helper: smart_commit() tự sinh commit message từ diff, branch_status() xem trạng thái nhánh, pr_checklist() kiểm tra trước khi tạo PR."
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

Bộ helper giúp agent làm đúng quy trình git mà không cần nhớ hết lệnh:
tự đặt tên commit theo chuẩn conventional commits, xem nhanh trạng
thái nhánh, và checklist 5 mục trước khi mở PR.

## Khi nào dùng

- Agent vừa code xong, cần commit: `smart_commit()` sinh message từ diff
- Trước khi push: `branch_status()` xem còn file dirty, ahead/behind bao nhiêu
- Trước khi tạo PR: `pr_checklist()` quét 5 điều kiện thường bị quên
- Khi user nói "commit giúp anh", "branch này ổn chưa", "check trước khi PR"

**Không dùng cho:** thao tác git nguy hiểm (`reset --hard`, `push --force`,
xóa nhánh...) — skill cố ý không có các hàm đó.

## Input

- **repo:** đường dẫn repo git (mặc định `"."` — thư mục hiện tại)
- **smart_commit(repo, dry_run=True):** `dry_run=True` chỉ trả kế hoạch,
  `dry_run=False` mới commit thật

## Output

- `smart_commit()` → `CommitPlan{type, scope, message, files, staged}`
  hoặc `None` khi không có gì để commit.
  Message dạng `type(scope): tóm tắt`, vd: `feat(auth): add login, token refresh`
- `branch_status()` → `BranchStatus{branch, upstream, ahead, behind,
  staged, modified, untracked, clean}`
- `pr_checklist()` → `list[CheckItem{name, passed, detail, fix}]` — 5 mục:
  1. Cây làm việc sạch (không còn file chưa commit)
  2. Không commit trực tiếp lên main/master
  3. Nhánh đã push hết lên remote (ahead = 0)
  4. Có thay đổi so với main (PR không rỗng)
  5. Không còn dấu conflict (`<<<<<<<`)

## Ví dụ

### Python API

```python
import sys
sys.path.insert(0, "skills/git-workflow")  # trỏ vào thư mục của skill
from workflow import smart_commit, branch_status, pr_checklist

# 1. Xem trước message sẽ commit (không commit vội)
plan = smart_commit("/path/to/repo")
print(plan.message)  # vd: "feat(auth): add login, token refresh"
print(plan.files, plan.staged)

# 2. Commit thật khi đã ưng
smart_commit("/path/to/repo", dry_run=False)

# 3. Trạng thái nhánh
st = branch_status(".")
print(st.branch, "ahead:", st.ahead, "behind:", st.behind, "clean:", st.clean)

# 4. Checklist trước khi mở PR
for item in pr_checklist("."):
    icon = "✓" if item.passed else "✗"
    print(icon, item.name, "-", item.detail)
    if not item.passed and item.fix:
        print("   Sửa:", item.fix)
```

### CLI

```bash
# Xem trước commit message (dry-run mặc định)
python3 skills/git-workflow/workflow.py --repo . smart-commit

# Commit thật
python3 skills/git-workflow/workflow.py --repo . smart-commit --no-dry-run

# Trạng thái nhánh / checklist PR
python3 skills/git-workflow/workflow.py --repo . branch-status
python3 skills/git-workflow/workflow.py --repo . pr-checklist
```

## Pitfalls

- `smart_commit()` ưu tiên diff đã **staged**; nếu chưa stage gì mà cây
  đang dirty, nó dùng diff chưa staged và sẽ `git add` đúng các file đó
  khi commit thật (không add bừa cả repo)
- Đoán `type` là heuristic theo tên file: file mới → `feat`, toàn file
  test → `test`, toàn docs → `docs`, chỉ xóa → `chore` — luôn xem lại
  message ở chế độ dry-run trước khi commit thật
- `pr_checklist()` mục 4 cần nhánh `main`/`master` tồn tại local để so
  sánh; không có thì mục đó tự bỏ qua (passed=True kèm ghi chú)
- Mọi lệnh git lỗi (không phải repo, git chưa cài) đều ném `GitError`
