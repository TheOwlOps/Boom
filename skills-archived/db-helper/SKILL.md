---
name: db-helper
description: "Safe SQLite wrapper: query/insert/migrate with a table whitelist, parameterized queries, blocking DROP and DELETE/UPDATE without WHERE."
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

A safe SQLite wrapper for agents: every operation passes through a security
check before touching the database. The philosophy is **fail-closed** — anything
not clearly safe is rejected, never guessed.

## When to use

- An agent needs to read/write SQLite (job store, cache, local memory...) without
  hand-writing raw SQL that's prone to injection
- Running a `.sql` migrate file but worried it contains dangerous statements
- When the user says "read the db", "save to sqlite", "run the migration"

**Not for:** database servers (Postgres/MySQL) — this skill only wraps the
stdlib's sqlite3.

## Safety rules

| Rule | Meaning |
|---------|---------|
| Table whitelist | A table not in `allowed_tables` → `UnsafeSQLError` immediately |
| Strict identifiers | Table/column names must match `[A-Za-z_][A-Za-z0-9_]*` |
| Parameterized queries | All **values** go through `?` placeholders, no string concatenation |
| `delete()` requires WHERE | Empty `filters` → rejected (avoids wiping the whole table) |
| `migrate()` pre-scan | Blocks `DROP/TRUNCATE/ATTACH/...`; blocks `DELETE/UPDATE` without `WHERE` |

## Input

- **db_path:** path to the `.db` file (created automatically if missing)
- **allowed_tables:** list of permitted table names (empty = forbid everything)
- **query(table, filters, columns, limit):** `filters` is a dict `{column: value}`
- **insert(table, data):** `data` is a dict `{column: value}`
- **delete(table, filters):** `filters` must not be empty
- **migrate(sql_file):** path to a `.sql` file

## Output

- `query()` → `list[dict]`, each dict is a row (keys = column names)
- `insert()` → `InsertResult{table, rowid, rows_affected}`
- `delete()` → `int` (number of rows deleted)
- `migrate()` → `MigrationReport{file, applied, statements, message}`

Security violations raise `UnsafeSQLError` (subclasses `ValueError`); a migrate
with SQL syntax errors returns `applied=False` with a `message` instead of raising.

## Example

### Python API

```python
import sys
sys.path.insert(0, "skills/db-helper")  # point to the skill's directory
from helper import DBHelper, UnsafeSQLError

with DBHelper("app.db", allowed_tables=["users", "jobs"]) as db:
    # Conditional read
    admins = db.query("users", {"role": "admin"}, limit=10)

    # Safe write (quoted values can't break the statement)
    res = db.insert("users", {"name": "O'Brien", "role": "user"})
    print(res.rowid)

    # Delete requires WHERE
    db.delete("users", {"name": "spam-bot"})

    # Run migration (auto-blocks DROP / WHERE-less DELETE)
    report = db.migrate("migrations/001_init.sql")
    print(report.applied, report.statements)

    # Table outside the whitelist -> rejected immediately
    try:
        db.query("sqlite_master")
    except UnsafeSQLError as e:
        print("Blocked:", e)
```

### CLI

```bash
# Read (whitelist mandatory, fail-closed)
python3 skills/db-helper/helper.py --db app.db --tables users,jobs query users --where role=admin --limit 5

# Write
python3 skills/db-helper/helper.py --db app.db --tables users insert users --data '{"name":"An","role":"admin"}'

# Delete (--where mandatory)
python3 skills/db-helper/helper.py --db app.db --tables users delete users --where name=spam-bot

# Migrate
python3 skills/db-helper/helper.py --db app.db --tables users,jobs migrate migrations/001_init.sql
```

## Pitfalls

- An empty whitelist is **intentional**: fail-closed, every operation is rejected
  until you pass an explicit `allowed_tables`
- `query()` caps at 10,000 rows per call so an agent can't OOM the machine
- `migrate()` uses `executescript`: if one statement has a syntax error, earlier
  statements in the file may have already run — always check `MigrationReport.applied`
- The statement splitter handles `;` inside quotes and `--` comments, but very
  "exotic" SQL (e.g. complex multi-line triggers) should be tested on an empty db first
