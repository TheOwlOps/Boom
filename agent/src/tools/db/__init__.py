"""Tools nhóm db: truy vấn SQLite an toàn (chống SQL injection).

Hiện tại chỉ có SQLite (đủ cho nhu cầu local của Boom: memory, cron store,
audit...). Thiết kế tool:
  - db_query(table, columns, where, params, ...)  -> SELECT chỉ đọc
  - db_insert(table, row)                         -> INSERT, trả rowid
  - db_update(table, values, where, params)       -> UPDATE, bắt buộc WHERE

Mỗi tool mở 1 SQLiteClient riêng theo db_path truyền vào (không giữ state
global). Đường dẫn DB bị giới hạn trong base_dir (mặc định: thư mục làm việc
hiện tại) để chống path traversal. Bảng phải được whitelist qua
allowed_tables (list truyền vào tool).
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
        raise ValueError("db_path phải là string không rỗng")
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
    """SELECT an toàn trên bảng đã whitelist. Không có SQL thô.

    where chỉ cho dạng "col OP ?" nối AND/OR (xem sqlite._parse_where);
    mọi giá trị qua params (placeholder). Trả về {table, rows, count}.
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
    """INSERT 1 dòng {cột: giá trị} vào bảng đã whitelist. Trả về {rowid}."""
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
    """UPDATE bảng đã whitelist; WHERE bắt buộc (chống update toàn bảng).
    Trả về {table, affected} = số dòng bị ảnh hưởng."""
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
    except Exception as exc:  # tool không bao giờ raise vào agent loop
        return {"error": f"{type(exc).__name__}: {exc}"}


def _schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {},
            "required": required or []}


def _db_props(extra: dict) -> dict:
    base = {
        "db_path": {"type": "string", "description": "Đường dẫn file .db."},
        "table": {"type": "string", "description": "Tên bảng (phải có trong allowed_tables)."},
        "allowed_tables": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Whitelist bảng được phép chạm.",
        },
        "base_dir": {
            "type": "string",
            "description": "Root cho phép chứa file DB. Mặc định thư mục hiện tại.",
        },
    }
    base.update(extra)
    return base


def build_tools(env=None) -> Dict[str, Dict[str, Any]]:
    """Registry-ready tool dicts cho environment của Boom."""

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
                "SELECT chỉ-đọc trên bảng SQLite đã whitelist. Không có SQL "
                "thô: where chỉ cho 'col OP ?' nối AND/OR, giá trị qua params."
            ),
            "schema": _schema(
                _db_props({
                    "columns": {"type": "array", "items": {"type": "string"},
                                "description": "Cột cần lấy. Mặc định *."},
                    "where": {"type": "string",
                              "description": "VD 'age > ? AND name = ?'."},
                    "params": {"type": "array",
                               "description": "Giá trị cho các dấu ? trong where."},
                    "order_by": {"type": "string",
                                 "description": "VD 'created DESC'."},
                    "limit": {"type": "integer", "description": "Mặc định 100."},
                }),
                ["db_path", "table", "allowed_tables"],
            ),
        },
        "db_insert": {
            "name": "db_insert",
            "func": t_db_insert,
            "description": "INSERT 1 dòng {cột: giá trị} vào bảng đã whitelist. Trả về rowid.",
            "schema": _schema(
                _db_props({
                    "row": {"type": "object", "description": "{cột: giá trị} cần insert."},
                }),
                ["db_path", "table", "allowed_tables", "row"],
            ),
        },
        "db_update": {
            "name": "db_update",
            "func": t_db_update,
            "description": (
                "UPDATE bảng đã whitelist. WHERE bắt buộc (chống update toàn "
                "bảng). Trả về số dòng bị ảnh hưởng."
            ),
            "schema": _schema(
                _db_props({
                    "values": {"type": "object", "description": "{cột: giá trị mới}."},
                    "where": {"type": "string", "description": "Điều kiện, VD 'id = ?'."},
                    "params": {"type": "array", "description": "Giá trị cho các dấu ?."},
                }),
                ["db_path", "table", "allowed_tables", "values", "where"],
            ),
        },
    }
