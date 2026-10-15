---
name: db-helper
description: "Wrap SQLite an toàn: query/insert/migrate với whitelist bảng, parameterized query, chặn DROP và DELETE/UPDATE thiếu WHERE."
version: 1.0.0
author: Boom Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [sqlite, database, security, sql-injection]
    related_skills: [security-audit, test-writer]
---

# DB Helper

Wrapper SQLite an toàn cho agent: mọi thao tác đều đi qua lớp kiểm tra
bảo mật trước khi chạm vào database. Triết lý **fail-closed** — cái gì
không rõ ràng an toàn thì từ chối, không đoán mò.

## Khi nào dùng

- Agent cần đọc/ghi SQLite (job store, cache, memory local...) mà không
  muốn tự viết SQL thô dễ dính injection
- Chạy file migrate `.sql` nhưng sợ trong file có lệnh nguy hiểm
- Khi user nói "đọc db", "lưu vào sqlite", "chạy migration"

**Không dùng cho:** database server (Postgres/MySQL) — skill chỉ wrap
sqlite3 của stdlib.

## Quy tắc an toàn

| Quy tắc | Ý nghĩa |
|---------|---------|
| Whitelist bảng | Bảng không nằm trong `allowed_tables` → `UnsafeSQLError` ngay |
| Identifier nghiêm ngặt | Tên bảng/cột phải khớp `[A-Za-z_][A-Za-z0-9_]*` |
| Parameterized query | Mọi **giá trị** đi qua placeholder `?`, không nối chuỗi |
| `delete()` bắt buộc WHERE | `filters` rỗng → từ chối (tránh xóa cả bảng) |
| `migrate()` quét trước | Cấm `DROP/TRUNCATE/ATTACH/...`; cấm `DELETE/UPDATE` thiếu `WHERE` |

## Input

- **db_path:** đường dẫn file `.db` (tự tạo mới nếu chưa có)
- **allowed_tables:** list tên bảng được phép (rỗng = cấm tất cả)
- **query(table, filters, columns, limit):** `filters` là dict `{cột: giá trị}`
- **insert(table, data):** `data` là dict `{cột: giá trị}`
- **delete(table, filters):** `filters` bắt buộc không rỗng
- **migrate(sql_file):** đường dẫn file `.sql`

## Output

- `query()` → `list[dict]`, mỗi dict là một dòng (key = tên cột)
- `insert()` → `InsertResult{table, rowid, rows_affected}`
- `delete()` → `int` (số dòng đã xóa)
- `migrate()` → `MigrationReport{file, applied, statements, message}`

Lỗi bảo mật ném `UnsafeSQLError` (kế thừa `ValueError`); migrate lỗi cú
pháp SQL thì trả `applied=False` kèm `message` thay vì raise.

## Ví dụ

### Python API

```python
import sys
sys.path.insert(0, "skills/db-helper")  # trỏ vào thư mục của skill
from helper import DBHelper, UnsafeSQLError

with DBHelper("app.db", allowed_tables=["users", "jobs"]) as db:
    # Đọc có điều kiện
    admins = db.query("users", {"role": "admin"}, limit=10)

    # Ghi an toàn (giá trị có quote cũng không phá được câu lệnh)
    res = db.insert("users", {"name": "O'Brien", "role": "user"})
    print(res.rowid)

    # Xóa bắt buộc có WHERE
    db.delete("users", {"name": "spam-bot"})

    # Chạy migration (tự chặn DROP / DELETE không WHERE)
    report = db.migrate("migrations/001_init.sql")
    print(report.applied, report.statements)

    # Bảng ngoài whitelist -> bị từ chối ngay
    try:
        db.query("sqlite_master")
    except UnsafeSQLError as e:
        print("Chặn:", e)
```

### CLI

```bash
# Đọc (whitelist bắt buộc, fail-closed)
python3 skills/db-helper/helper.py --db app.db --tables users,jobs query users --where role=admin --limit 5

# Ghi
python3 skills/db-helper/helper.py --db app.db --tables users insert users --data '{"name":"An","role":"admin"}'

# Xóa (bắt buộc --where)
python3 skills/db-helper/helper.py --db app.db --tables users delete users --where name=spam-bot

# Migrate
python3 skills/db-helper/helper.py --db app.db --tables users,jobs migrate migrations/001_init.sql
```

## Pitfalls

- Whitelist rỗng là **cố ý**: fail-closed, mọi thao tác đều bị từ chối
  cho tới khi bạn truyền `allowed_tables` rõ ràng
- `query()` giới hạn tối đa 10.000 dòng/lần để agent không đọc sập RAM
- `migrate()` dùng `executescript`: nếu 1 câu lỗi cú pháp, các câu trước
  đó trong file có thể đã chạy — luôn kiểm tra `MigrationReport.applied`
- Bộ tách câu lệnh xử lý được `;` trong quote và comment `--`, nhưng SQL
  quá "dị" (vd: trigger nhiều dòng phức tạp) nên test trước trên db rỗng
