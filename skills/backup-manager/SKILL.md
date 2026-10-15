---
name: backup-manager
description: Tạo, liệt kê, khôi phục và dọn backup tar.gz.
version: 1.0.0
author: boom-overnight
license: MIT
platforms: [linux, macos]
tags: [backup, archive, recovery]
category: devops
---

# Backup Manager

Sao lưu thư mục thành file `tar.gz` kèm timestamp và checksum SHA-256,
giúp khôi phục nhanh khi cần:

- `create_backup(src, dest)` — nén thư mục nguồn, trả về đường dẫn file backup
  và checksum; checksum được verify lại ngay sau khi tạo.
- `list_backups(dir)` — liệt kê các bản backup, mới nhất trước.
- `restore(backup, dest)` — giải nén về thư mục đích.
- `prune(dir, keep_n)` — chỉ giữ `keep_n` bản mới nhất, xoá bản cũ hơn.

## Cách dùng

```python
from backup import create_backup, list_backups, restore, prune

info = create_backup("/data/app", "/backups")
# {"path": "/backups/app-20261016-020000.tar.gz", "sha256": "...", "verified": True}
restore(info["path"], "/data/restored")
prune("/backups", keep_n=5)
```

File backup có tên `<tên-thư-mục>-YYYYMMDD-HHMMSS.tar.gz`.
Checksum SHA-256 được lưu trong file `.sha256` cạnh backup.
Dùng `terminal` để kiểm tra dung lượng trước khi backup thư mục lớn.
