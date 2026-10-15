---
name: cron-manager
description: "Quản lý cron jobs: add/list/remove (lưu JSON) + validate cron expression, hỗ trợ @daily/@hourly và dạng 5 trường như '*/5 * * * *'."
version: 1.0.0
author: Boom Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [cron, scheduler, automation, jobs]
    related_skills: [productivity, devops]
---

# Cron Manager

Quản lý danh sách cron jobs của agent (lưu vào file JSON) kèm bộ
validate cron expression viết tay — không cần cài thêm gì, chạy
được ngay cả khi máy chưa có cron daemon.

## Khi nào dùng

- User nói "chạy cái này mỗi sáng", "lên lịch backup hằng ngày"
- Agent cần validate cron expression trước khi ghi vào crontab thật
- Lưu/tải danh sách job đã lên lịch để agent nhớ giữa các phiên
- Khi user hỏi "job này chạy khi nào" → dùng `describe()` trả lời tiếng Việt

**Không dùng cho:** ghi thẳng vào crontab hệ thống — skill chỉ quản lý
danh sách + validate; muốn cài thật thì tự `crontab -e` hoặc dùng API
cron của Boom.

## Cron expression được hỗ trợ

| Dạng | Ví dụ | Ý nghĩa |
|------|-------|---------|
| Alias | `@daily`, `@hourly` | `@reboot`, `@yearly`, `@monthly`, `@weekly`, `@daily`, `@hourly` |
| 5 trường | `*/5 * * * *` | `phút giờ ngày tháng thứ` |
| Bước nhảy | `*/15`, `0 */2 * * *` | mỗi 15 phút / mỗi 2 giờ |
| Khoảng | `9-17 * * * *` | từ 9h tới 17h |
| Liệt kê | `0 9,18 * * *` | lúc 9h và 18h |
| Tên | `0 9 * * mon-fri` | thứ 2–6 lúc 9h (`jan`–`dec`, `sun`–`sat`) |

Khoảng giá trị: phút 0–59, giờ 0–23, ngày 1–31, tháng 1–12, thứ 0–7
(7 = Chủ nhật, tự chuẩn hóa về 0).

## Input

- **add(name, schedule, command, store):** tên job (chỉ `a-z 0-9 _ . -`),
  schedule (đã validate), command (câu lệnh chạy)
- **list_jobs(store):** không cần input
- **remove(name, store):** tên job
- **parse_cron(expr) / describe(expr):** chuỗi cron
- **store:** đường dẫn file JSON (mặc định `~/.boom/cron_jobs.json`)

## Output

- `parse_cron()` → `CronExpression{minute, hour, day, month, weekday, raw, alias}`
  (mỗi trường là list int đã chuẩn hóa); sai → ném `CronError`
- `add()` → `CronJob{name, schedule, command, created_at, enabled}`; tên
  trùng hoặc schedule sai → ném `CronError`
- `list_jobs()` → `list[CronJob]`
- `remove()` → `True` (đã xóa) / `False` (không tồn tại)
- `describe()` → chuỗi tiếng Việt, vd: `"mỗi 5 phút một lần"`

## Ví dụ

### Python API

```python
import sys
sys.path.insert(0, "skills/cron-manager")  # trỏ vào thư mục của skill
from manager import add, list_jobs, remove, parse_cron, describe

# Validate trước khi làm gì khác
c = parse_cron("*/5 * * * *")
print(c.minute)          # [0, 5, 10, ..., 55]
print(describe("@daily"))  # "mỗi ngày một lần (00:00)"

# Thêm job (tự validate schedule, tên trùng thì báo lỗi)
job = add("backup-db", "0 2 * * *", "python3 /opt/boom/backup.py")
print(job.created_at)

# Liệt kê / xóa
for j in list_jobs():
    print(j.name, "->", describe(j.schedule))
remove("backup-db")
```

### CLI

```bash
# Thêm job
python3 skills/cron-manager/manager.py add backup-db "0 2 * * *" "python3 backup.py"

# Liệt kê
python3 skills/cron-manager/manager.py list

# Xóa
python3 skills/cron-manager/manager.py remove backup-db

# Chỉ validate + xem mô tả tiếng Việt
python3 skills/cron-manager/manager.py validate "*/5 * * * *"
```

## Pitfalls

- Skill **không** tự ghi vào crontab hệ thống — `add()` chỉ lưu vào file
  JSON để agent quản lý; cài lịch thật là bước riêng do user/agent quyết
- `@reboot` parse được nhưng không có 5 trường giờ giấc (các list rỗng)
- Tên job giới hạn ký tự an toàn để tránh injection khi ghép vào crontab
- File JSON hỏng định dạng → `CronError` rõ ràng thay vì crash lặng
