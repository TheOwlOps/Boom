---
name: refactor-assistant
description: "Trợ lý refactor an toàn: rename symbol, extract function, move symbol bằng AST thật, luôn dry-run preview diff trước khi apply."
version: 1.0.0
author: Boom
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [refactor, ast, rename, extract-function, code-quality]
    related_skills: [software-development/simplify-code, software-development/systematic-debugging]
---

# Refactor-Assistant — Trợ lý Refactor An Toàn

## Tên
`refactor-assistant`

## Mô tả
Wrap `RefactorEngine` (AST-based, từ `agent/src/autonomous/refactor.py`, đã vendor vào skill) thành API thân thiện cho agent. Hỗ trợ 3 thao tác:

- **rename_symbol**: đổi tên biến/hàm/class xuyên nhiều file
- **extract_function**: tách đoạn code thành hàm mới (tự suy params/return)
- **move_symbol**: chuyển hàm/class top-level sang file khác

Mọi thao tác đều có **dry-run preview diff trước khi apply**, backup tự động (`.bak-<timestamp>`), validate syntax sau khi sửa và rollback nếu lỗi.

## Khi nào dùng
- Đổi tên symbol mà grep/regex thủ công dễ sót hoặc đổi nhầm
- Tách hàm dài thành hàm nhỏ hơn
- Di chuyển code giữa các file khi tái cấu trúc module
- Khi cần refactor hàng loạt nhưng vẫn muốn xem diff trước

## Input
- `root_dir` (str): thư mục gốc project
- Thao tác:
  - `rename(old_name, new_name, files)` — files: list đường dẫn tương đối
  - `extract(path, start_line, end_line, new_name)`
  - `move(src, dst, symbol_name)`
- Mọi thao tác đều nhận `preview=True` (mặc định) để chỉ xem diff.

## Output
Dict kết quả từ engine: `{ok, dry_run, diffs/diff, occurrences, params, returns, backups, ...}`. Khi `ok=False` sẽ có `error` mô tả lý do.

## Ví dụ

```python
from refactor_assistant.assistant import RefactorAssistant

ra = RefactorAssistant("/srv/myapp")

# 1) Preview trước — không ghi file
p = ra.rename("old_name", "new_name", ["a.py", "b.py"], preview=True)
print(p["diffs"]["a.py"])   # xem diff

# 2) OK thì apply thật (có backup + validate syntax)
r = ra.rename("old_name", "new_name", ["a.py", "b.py"], preview=False)
assert r["ok"]

# 3) Tách hàm
e = ra.extract("a.py", 10, 25, "tinh_tong", preview=True)
print("params:", e["params"], "returns:", e["returns"])
```
