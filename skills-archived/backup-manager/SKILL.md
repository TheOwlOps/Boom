---
name: backup-manager
description: Create, list, restore, and prune tar.gz backups.
version: 1.0.0
author: boom-overnight
license: MIT
platforms: [linux, macos]
tags: [backup, archive, recovery]
category: devops
---

# Backup Manager

Back up a directory into a `tar.gz` file with a timestamp and SHA-256 checksum,
for quick recovery when needed:

- `create_backup(src, dest)` — compress the source directory; returns the backup
  file path and checksum; the checksum is re-verified immediately after creation.
- `list_backups(dir)` — list backups, newest first.
- `restore(backup, dest)` — extract into the destination directory.
- `prune(dir, keep_n)` — keep only the `keep_n` newest backups, delete older ones.

## Usage

```python
from backup import create_backup, list_backups, restore, prune

info = create_backup("/data/app", "/backups")
# {"path": "/backups/app-20261016-020000.tar.gz", "sha256": "...", "verified": True}
restore(info["path"], "/data/restored")
prune("/backups", keep_n=5)
```

Backup files are named `<directory-name>-YYYYMMDD-HHMMSS.tar.gz`.
The SHA-256 checksum is stored in a `.sha256` file next to the backup.
Use `terminal` to check disk usage before backing up large directories.
