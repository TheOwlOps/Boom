#!/usr/bin/env python3
"""Skill db-helper: wrap SQLite an toàn cho agent.

Ba thao tác chính:
  - query(table, filters): SELECT có điều kiện, trả về list[dict]
  - insert(table, data): INSERT tham số hóa, trả về InsertResult
  - migrate(sql_file): chạy file .sql, chặn DROP và DELETE/UPDATE thiếu WHERE

Bảo mật (fail-closed):
  - whitelist bảng: bảng không nằm trong danh sách cho phép -> từ chối ngay
  - tên bảng/cột phải khớp identifier [A-Za-z_][A-Za-z0-9_]* (chống injection
    qua tên bảng/cột, vì phần này không parameterize được)
  - mọi GIÁ TRỊ đều đi qua placeholder "?" (parameterized query)
  - migrate() quét từng câu lệnh trước khi chạy: cấm DROP/TRUNCATE/...,
    cấm DELETE/UPDATE không có WHERE
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Model dữ liệu (pydantic)
# ---------------------------------------------------------------------------

class InsertResult(BaseModel):
    """Kết quả trả về của insert()."""

    table: str
    rowid: int
    rows_affected: int = 1


class MigrationReport(BaseModel):
    """Báo cáo sau khi chạy migrate()."""

    file: str
    applied: bool
    statements: int
    message: str = ""


# ---------------------------------------------------------------------------
# Lỗi
# ---------------------------------------------------------------------------

class UnsafeSQLError(ValueError):
    """Thao tác SQL bị coi là không an toàn (whitelist / thiếu WHERE / bị cấm)."""


# Tên bảng/cột chỉ được chứa chữ, số, gạch dưới, không bắt đầu bằng số.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Từ khóa đứng đầu câu bị cấm tuyệt đối trong file migrate.
_BLOCKED_FIRST_WORDS = {"DROP", "TRUNCATE", "ATTACH", "DETACH", "VACUUM", "REINDEX"}

# Tìm mệnh đề WHERE (không phân biệt hoa thường).
_WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)


def _check_identifier(name: str, kind: str = "tên") -> str:
    """Kiểm tra tên bảng/cột có an toàn không, ném UnsafeSQLError nếu không."""
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise UnsafeSQLError(f"{kind} không hợp lệ (nguy cơ SQL injection): {name!r}")
    return name


def _split_statements(text: str) -> list[str]:
    """Tách file SQL thành từng câu lệnh.

    Bỏ qua dấu ';' nằm trong quote đơn/kép và comment dòng bắt đầu bằng '--'.
    """
    stmts: list[str] = []
    buf: list[str] = []
    quote: Optional[str] = None  # đang ở trong quote nào: None / "'" / '"'
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                # Quote thoát bằng cách nhân đôi, vd: 'O''Brien'
                if i + 1 < n and text[i + 1] == quote:
                    buf.append(quote)
                    i += 1
                else:
                    quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == "-" and i + 1 < n and text[i + 1] == "-":
            # Comment dòng: bỏ qua tới hết dòng
            while i < n and text[i] != "\n":
                i += 1
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def _assert_statement_safe(stmt: str) -> None:
    """Quét 1 câu lệnh SQL, ném UnsafeSQLError nếu thấy mẫu nguy hiểm."""
    head = stmt.strip().split(None, 1)
    if not head:
        return
    word = head[0].upper().rstrip(";")
    if word in _BLOCKED_FIRST_WORDS:
        raise UnsafeSQLError(
            f"Cấm câu lệnh {word} trong migrate (nguy cơ mất dữ liệu): {stmt[:60]!r}..."
        )
    # DELETE / UPDATE bắt buộc phải có WHERE để không xóa/sửa cả bảng
    if word in ("DELETE", "UPDATE") and not _WHERE_RE.search(stmt):
        raise UnsafeSQLError(
            f"Cấm {word} không có WHERE trong migrate (sẽ tác động cả bảng): {stmt[:60]!r}..."
        )


class DBHelper:
    """Wrapper SQLite an toàn, dùng qua context manager hoặc close() thủ công."""

    def __init__(
        self,
        db_path: str | Path,
        allowed_tables: Optional[Iterable[str]] = None,
    ):
        self.db_path = Path(db_path)
        # Whitelist: None hoặc rỗng nghĩa là KHÔNG cho phép bảng nào (fail-closed).
        self.allowed_tables = set(allowed_tables or [])
        for t in self.allowed_tables:
            _check_identifier(t, "tên bảng trong whitelist")
        self._conn = sqlite3.connect(str(self.db_path))
        # row_factory = Row: truy cập cột theo tên, dễ chuyển thành dict.
        self._conn.row_factory = sqlite3.Row

    # -- kiểm tra nội bộ ----------------------------------------------------
    def _validate_table(self, table: str) -> str:
        _check_identifier(table, "tên bảng")
        if table not in self.allowed_tables:
            raise UnsafeSQLError(
                f"Bảng {table!r} không nằm trong whitelist {sorted(self.allowed_tables)}"
            )
        return table

    # -- API chính ------------------------------------------------------------
    def query(
        self,
        table: str,
        filters: Optional[Mapping[str, Any]] = None,
        columns: Optional[Iterable[str]] = None,
        limit: int = 100,
    ) -> list[dict]:
        """SELECT an toàn: chỉ bảng trong whitelist, điều kiện qua placeholder."""
        self._validate_table(table)
        if columns:
            cols = [_check_identifier(c, "tên cột") for c in columns]
            select = ", ".join(f'"{c}"' for c in cols)
        else:
            select = "*"
        sql = f'SELECT {select} FROM "{table}"'
        params: list[Any] = []
        if filters:
            conds = []
            for key, value in filters.items():
                _check_identifier(key, "tên cột")
                if value is None:
                    conds.append(f'"{key}" IS NULL')
                else:
                    conds.append(f'"{key}" = ?')
                    params.append(value)
            sql += " WHERE " + " AND ".join(conds)
        # Giới hạn số dòng để agent không vô tình đọc cả bảng khổng lồ.
        limit = max(1, min(int(limit), 10_000))
        sql += " LIMIT ?"
        params.append(limit)
        cur = self._conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    def insert(self, table: str, data: Mapping[str, Any]) -> InsertResult:
        """INSERT tham số hóa, trả về rowid của dòng mới."""
        self._validate_table(table)
        if not data:
            raise ValueError("data rỗng: không có gì để insert")
        keys = [_check_identifier(k, "tên cột") for k in data.keys()]
        cols = ", ".join(f'"{k}"' for k in keys)
        placeholders = ", ".join("?" for _ in keys)
        sql = f'INSERT INTO "{table}" ({cols}) VALUES ({placeholders})'
        cur = self._conn.execute(sql, list(data.values()))
        self._conn.commit()
        return InsertResult(table=table, rowid=cur.lastrowid or 0)

    def delete(self, table: str, filters: Mapping[str, Any]) -> int:
        """DELETE an toàn: filters bắt buộc, rỗng là từ chối ngay (thiếu WHERE)."""
        self._validate_table(table)
        if not filters:
            raise UnsafeSQLError(
                "DELETE bị chặn: filters rỗng (thiếu WHERE sẽ xóa cả bảng)"
            )
        conds: list[str] = []
        params: list[Any] = []
        for key, value in filters.items():
            _check_identifier(key, "tên cột")
            conds.append(f'"{key}" = ?')
            params.append(value)
        sql = f'DELETE FROM "{table}" WHERE ' + " AND ".join(conds)
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur.rowcount

    def migrate(self, sql_file: str | Path) -> MigrationReport:
        """Chạy file .sql sau khi quét an toàn từng câu lệnh.

        Trả về MigrationReport(applied=False, message=...) thay vì raise
        nếu SQL lỗi cú pháp lúc chạy (để agent đọc được lý do).
        """
        path = Path(sql_file)
        text = path.read_text(encoding="utf-8")
        statements = _split_statements(text)
        # Quét an toàn TRƯỚC khi chạy bất kỳ câu nào (không chạy nửa chừng).
        for stmt in statements:
            _assert_statement_safe(stmt)
        try:
            self._conn.executescript(text)
        except sqlite3.Error as exc:
            return MigrationReport(
                file=str(path), applied=False, statements=0, message=str(exc)
            )
        return MigrationReport(file=str(path), applied=True, statements=len(statements))

    # -- dọn dẹp ---------------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "DBHelper":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# CLI nhỏ để dùng nhanh từ terminal
# ---------------------------------------------------------------------------

def _parse_kv(pairs: list[str]) -> dict[str, str]:
    """Chuyển ['a=1', 'b=2'] thành {'a': '1', 'b': '2'}."""
    out: dict[str, str] = {}
    for p in pairs:
        if "=" not in p:
            raise ValueError(f"điều kiện phải dạng key=value: {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="db-helper: thao tác SQLite an toàn")
    ap.add_argument("--db", required=True, help="đường dẫn file .db")
    ap.add_argument(
        "--tables",
        default="",
        help="whitelist bảng, cách nhau bằng dấu phẩy (vd: users,orders)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("query", help="SELECT")
    q.add_argument("table")
    q.add_argument("--where", action="append", default=[], help="điều kiện key=value")
    q.add_argument("--columns", default="", help="cột cần lấy, cách nhau dấu phẩy")
    q.add_argument("--limit", type=int, default=100)

    ins = sub.add_parser("insert", help="INSERT")
    ins.add_argument("table")
    ins.add_argument("--data", required=True, help='JSON object, vd: \'{"name":"An"}\'')

    d = sub.add_parser("delete", help="DELETE (bắt buộc có --where)")
    d.add_argument("table")
    d.add_argument("--where", action="append", default=[], required=True)

    m = sub.add_parser("migrate", help="chạy file .sql")
    m.add_argument("sql_file")

    args = ap.parse_args(argv)
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    if not tables:
        print("Lỗi: phải truyền --tables (whitelist, fail-closed).", file=sys.stderr)
        return 2

    with DBHelper(args.db, allowed_tables=tables) as db:
        if args.cmd == "query":
            cols = [c.strip() for c in args.columns.split(",") if c.strip()] or None
            rows = db.query(args.table, _parse_kv(args.where), columns=cols, limit=args.limit)
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif args.cmd == "insert":
            res = db.insert(args.table, json.loads(args.data))
            print(res.model_dump_json(indent=2))
        elif args.cmd == "delete":
            n = db.delete(args.table, _parse_kv(args.where))
            print(json.dumps({"deleted": n}, ensure_ascii=False))
        elif args.cmd == "migrate":
            rep = db.migrate(args.sql_file)
            print(rep.model_dump_json(indent=2))
            return 0 if rep.applied else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
