---
name: test-writer
description: "Sinh test pytest từ AST: phân tích function, suy luận giá trị mẫu, sinh happy path + edge cases (None, rỗng, biên)."
version: 1.0.0
author: Boom Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [testing, pytest, test-generation, ast]
    related_skills: [code-review, security-audit]
---

# Test Writer

Phân tích function bằng AST rồi sinh test case pytest tự động:

- **Happy path:** gọi hàm với giá trị mẫu suy luận từ tên biến / annotation /
  default (vd: `email` → `"test@example.com"`, `count: int` → `5`).
- **Edge cases:** `None`, rỗng (`""`/`[]`/`{}`), biên (`0`, `-1`).
- **Assert thông minh:** nếu thân hàm đơn giản (1 câu `return` tính được),
  tự tính kết quả mong đợi và sinh assert thật; còn lại để `TODO` cho
  developer điền.

## Khi nào dùng

- Vừa viết xong function mới, cần test nhanh
- Bổ sung test cho code cũ chưa có test
- Khi user nói "viết test cho hàm này", "sinh test", "thêm test case"

**Không dùng cho:** method phức tạp cần khởi tạo object (skill sinh
placeholder, developer tự điền); code bất đồng bộ phức tạp.

## Input

- **File source:** 1 file `.py` (phân tích các function cấp module)
- **Tham số:** tên module để import trong test (mặc định: tên file source)

## Output

File `test_<tên>.py` chuẩn pytest:

```python
"""Test tự sinh bởi skill test-writer — kiểm tra lại và bổ sung assert TODO."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from my_module import add, greet


def test_add_happy_path():
    """Happy path: gọi với giá trị mẫu hợp lệ."""
    assert add(1, 2) == 3


def test_add_none_input():
    """Edge case: input None — kiểm tra hàm xử lý an toàn, không crash."""
    # TODO: khẳng định hành vi mong đợi (raise hay trả về gì)
    add(None, 2)


def test_add_boundary_zero():
    """Edge case: giá trị biên: 0."""
    # TODO: khẳng định hành vi mong đợi (raise hay trả về gì)
    add(0, 2)
```

## Ví dụ

### CLI

```bash
# In test ra stdout
python3 skills/test-writer/writer.py agent/src/my_module.py

# Ghi ra file test_my_module.py cạnh source
python3 skills/test-writer/writer.py agent/src/my_module.py -o tests/test_my_module.py

# Chạy test đã sinh
python3 -m pytest tests/test_my_module.py -v
```

### Python API

```python
from skills.test_writer.writer import generate_tests, write_tests

# Sinh từ source string
result = generate_tests("def add(a, b):\n    return a + b\n", module_name="my_module")
print(result.test_code)       # nội dung file test
print(result.functions)       # ['add']

# Sinh và ghi file cạnh source
out = write_tests("agent/src/my_module.py")
print("Đã ghi:", out)
```

## Cách suy luận giá trị mẫu

Thứ tự ưu tiên: **default** → **tên biến** (regex: email, password, url, path,
count, price, items...) → **annotation** (`int`→`1`, `str`→`"sample"`,
`list`→`[1, 2, 3]`...) → `"None"`.

## Pitfalls

- Test edge case sinh ra là scaffolding: chạy được ngay nhưng assert còn là
  `TODO` — developer phải khẳng định hành vi mong đợi rồi mới tin được
- Chỉ tính được kết quả mong đợi cho hàm đơn giản (1 `return`, không gọi hàm
  khác, không truy cập attribute) — hàm phức tạp hơn thì happy path cũng là TODO
- Bỏ qua method trong class và hàm tên bắt đầu `_`
