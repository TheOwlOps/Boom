---
name: env-setup
description: "Kiểm tra môi trường dev: version Python, package đã cài, biến môi trường bắt buộc, port còn trống. Output chuẩn [{check, status, fix_command}]."
version: 1.0.0
author: Boom Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [environment, setup, diagnostics, onboarding]
    related_skills: [deployer, devops]
---

# Env Setup

"Kiểm tra sức khỏe" môi trường dev trước khi chạy/build dự án: Python
đủ mới không, thiếu package nào, quên set biến môi trường nào, port có
bị chiếm không. Mỗi mục fail đều kèm `fix_command` để agent (hoặc user)
sửa ngay.

## Khi nào dùng

- Trước khi chạy dự án mới clone về: "máy này chạy được chưa?"
- Debug lỗi "module not found", "port already in use", "missing env var"
- Onboarding: sinh checklist môi trường cho contributor mới
- Agent tự kiểm tra trước khi start gateway/worker

**Không dùng cho:** cài đặt package tự động hàng loạt — skill chỉ
**kiểm tra và gợi ý lệnh sửa**, quyết định chạy lệnh là của agent/user.

## Các mục kiểm tra

| Check | Cách kiểm tra | Ví dụ `fix_command` khi fail |
|-------|---------------|------------------------------|
| Python version | `sys.version_info >= min_version` | `sudo apt install python3.12` |
| deps | `importlib.util.find_spec(pkg)` | `pip install httpx` |
| env vars | `os.environ` có set và không rỗng | `export API_KEY=<giá-trị>` |
| ports | thử `bind(127.0.0.1:port)` | `lsof -ti :8000 \| xargs kill -9` |

## Input

- **EnvConfig{min_python, deps, env_vars, ports}** — vd:
  `EnvConfig(min_python="3.10", deps=["httpx", "pydantic"],
  env_vars=["OPENAI_API_KEY"], ports=[8000, 8788])`
- Hoặc gọi lẻ: `check_python("3.10")`, `check_deps([...])`,
  `check_env_vars([...])`, `check_ports([...])`

## Output

`list[CheckResult]`, mỗi phần tử có schema cố định:

```json
[
  {"check": "dep:httpx", "status": "ok", "detail": "đã cài, import được", "fix_command": ""},
  {"check": "port:8000", "status": "fail", "detail": "đang bị tiến trình khác chiếm", "fix_command": "lsof -ti :8000 | xargs kill -9  # hoặc đổi sang port khác"}
]
```

- `status`: `"ok"` | `"fail"` | `"warn"`
- `fix_command` rỗng khi đã ok — agent chỉ cần quan tâm các mục fail
- `all_ok(results)` → `True` khi mọi mục đều ok

## Ví dụ

### Python API

```python
import sys
sys.path.insert(0, "skills/env-setup")  # trỏ vào thư mục của skill
from setup import EnvConfig, run_all, all_ok, results_to_json

config = EnvConfig(
    min_python="3.10",
    deps=["httpx", "pydantic"],
    env_vars=["OPENAI_API_KEY"],
    ports=[8000, 8788],
)
results = run_all(config)
print(results_to_json(results))

if not all_ok(results):
    for r in results:
        if r.status != "ok":
            print(f"Cần sửa [{r.check}]: {r.fix_command}")
```

### CLI

```bash
# Kiểm tra nhanh, in bảng
python3 skills/env-setup/setup.py --min-python 3.10 \
  --dep httpx --dep pydantic \
  --env OPENAI_API_KEY \
  --port 8000 --port 8788

# In JSON để tool khác đọc (exit code 1 nếu có mục fail)
python3 skills/env-setup/setup.py --dep httpx --port 8000 --json
```

## Pitfalls

- Check port bằng cách **thử bind**: có race condition cực nhỏ (port vừa
  check xong thì bị tiến trình khác lấy) — đủ tốt cho mục đích kiểm tra
- `check_deps` dùng `find_spec`: package cài nhưng import lỗi (thiếu
  dependency con) vẫn tính là fail — đúng ý nghĩa "chưa dùng được"
- Biến môi trường chỉ tính là ok khi **đã set và không rỗng**
- Trên Windows, `fix_command` gợi ý `lsof` không chạy được — thay bằng
  `netstat -ano | findstr :PORT` rồi `taskkill /PID ...`
