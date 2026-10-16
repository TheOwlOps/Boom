"""DB tool group: safe SQLite queries (SQL-injection-proof).

SQLite only for now (enough for Boom's local needs: memory, cron store,
audit...). Tool design:
  - db_query(table, columns, where, params, ...)  -> read-only SELECT
  - db_insert(table, row)                         -> INSERT, returns rowid
  - db_update(table, values, where, params)       -> UPDATE, WHERE mandatory

Each tool opens its own SQLiteClient per the given db_path (no global state
kept). DB paths are confined to base_dir (default: current working dir) to
block path traversal. Tables must be whitelisted via allowed_tables (a list
passed into the tool).
"""

from typing import Any, Dict, List, Optional, Sequence

from .sqlite import SQLiteClient, SQLiteError

__all__ = [
    "SQLiteClient",
    "SQLiteError",
    "db_query",
    "db_insert",
    "db_update",
    "build_tools",
]


def _client(db_path: str, allowed_tables: Optional[Sequence[str]],
            base_dir: Optional[str]) -> SQLiteClient:
    if not isinstance(db_path, str) or not db_path.strip():
        raise ValueError("db_path must be a non-empty string")
    return SQLiteClient(
        db_path.strip(),
        allowed_tables=list(allowed_tables or []),
        base_dir=base_dir,
    )


def db_query(
    db_path: str,
    table: str,
    allowed_tables: Sequence[str],
    columns: Optional[Sequence[str]] = None,
    where: Optional[str] = None,
    params: Sequence[Any] = (),
    order_by: Optional[str] = None,
    limit: int = 100,
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Safe SELECT on a whitelisted table. No raw SQL.

    where only accepts "col OP ?" joined by AND/OR (see sqlite._parse_where);
    all values go through params (placeholders). Returns {table, rows, count}.
    """
    client = _client(db_path, allowed_tables, base_dir)
    try:
        rows = client.query(table, columns=columns, where=where,
                            params=list(params or []), order_by=order_by,
                            limit=limit)
        return {"table": table, "rows": rows, "count": len(rows)}
    finally:
        client.close()


def db_insert(
    db_path: str,
    table: str,
    allowed_tables: Sequence[str],
    row: Dict[str, Any],
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """INSERTs 1 row {column: value} into a whitelisted table. Returns {rowid}."""
    client = _client(db_path, allowed_tables, base_dir)
    try:
        rowid = client.insert(table, dict(row or {}))
        return {"table": table, "rowid": rowid}
    finally:
        client.close()


def db_update(
    db_path: str,
    table: str,
    allowed_tables: Sequence[str],
    values: Dict[str, Any],
    where: str,
    params: Sequence[Any] = (),
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """UPDATEs a whitelisted table; WHERE is mandatory (blocks whole-table updates).
    Returns {table, affected} = rows affected."""
    client = _client(db_path, allowed_tables, base_dir)
    try:
        affected = client.update(table, dict(values or {}), where,
                                 list(params or []))
        return {"table": table, "affected": affected}
    finally:
        client.close()


# ------------------------------------------------------------- registry
def _guard(fn):
    try:
        return fn()
    except Exception as exc:  # tools never raise into the agent loop
        return {"error": f"{type(exc).__name__}: {exc}"}


def _schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {},
            "required": required or []}


def _db_props(extra: dict) -> dict:
    base = {
        "db_path": {"type": "string", "description": "Path to the .db file."},
        "table": {"type": "string", "description": "Table name (must be in allowed_tables)."},
        "allowed_tables": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Whitelist of tables that may be touched.",
        },
        "base_dir": {
            "type": "string",
            "description": "Root allowed to contain the DB file. Defaults to the current dir.",
        },
    }
    base.update(extra)
    return base


def build_tools(env=None) -> Dict[str, Dict[str, Any]]:
    """Registry-ready tool dicts for Boom's environment."""

    def t_db_query(db_path: str, table: str, allowed_tables: list,
                   columns=None, where: str = "", params=None,
                   order_by: str = "", limit: int = 100, base_dir: str = ""):
        return _guard(lambda: db_query(
            db_path, table, list(allowed_tables or []),
            columns=list(columns) if columns else None,
            where=where or None, params=list(params or []),
            order_by=order_by or None, limit=int(limit),
            base_dir=base_dir or None))

    def t_db_insert(db_path: str, table: str, allowed_tables: list,
                    row: dict, base_dir: str = ""):
        return _guard(lambda: db_insert(
            db_path, table, list(allowed_tables or []),
            dict(row or {}), base_dir=base_dir or None))

    def t_db_update(db_path: str, table: str, allowed_tables: list,
                    values: dict, where: str, params=None, base_dir: str = ""):
        return _guard(lambda: db_update(
            db_path, table, list(allowed_tables or []),
            dict(values or {}), where, list(params or []),
            base_dir=base_dir or None))

    return {
        "db_query": {
            "name": "db_query",
            "func": t_db_query,
            "description": (
                "Read-only SELECT on a whitelisted SQLite table. No raw SQL: "
                "where only accepts 'col OP ?' joined by AND/OR, values via params."
            ),
            "schema": _schema(
                _db_props({
                    "columns": {"type": "array", "items": {"type": "string"},
                                "description": "Columns to fetch. Default *."},
                    "where": {"type": "string",
                              "description": "E.g. 'age > ? AND name = ?'."},
                    "params": {"type": "array",
                               "description": "Values for the ? placeholders in where."},
                    "order_by": {"type": "string",
                                 "description": "E.g. 'created DESC'."},
                    "limit": {"type": "integer", "description": "Default 100."},
                }),
                ["db_path", "table", "allowed_tables"],
            ),
        },
        "db_insert": {
            "name": "db_insert",
            "func": t_db_insert,
            "description": "INSERTs 1 row {column: value} into a whitelisted table. Returns rowid.",
            "schema": _schema(
                _db_props({
                    "row": {"type": "object", "description": "{column: value} to insert."},
                }),
                ["db_path", "table", "allowed_tables", "row"],
            ),
        },
        "db_update": {
            "name": "db_update",
            "func": t_db_update,
            "description": (
                "UPDATEs a whitelisted table. WHERE is mandatory (blocks "
                "whole-table updates). Returns rows affected."
            ),
            "schema": _schema(
                _db_props({
                    "values": {"type": "object", "description": "{column: new value}."},
                    "where": {"type": "string", "description": "Condition, e.g. 'id = ?'."},
                    "params": {"type": "array", "description": "Values for the ? placeholders."},
                }),
                ["db_path", "table", "allowed_tables", "values", "where"],
            ),
        },
    }
