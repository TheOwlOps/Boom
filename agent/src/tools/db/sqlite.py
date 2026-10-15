"""SQLite an toàn cho Boom: chống SQL injection bằng thiết kế.

Nguyên tắc (không tin input dù là từ agent):
  1. KHÔNG có API chạy SQL thô — chỉ có query()/insert()/update() dựng câu
     lệnh từ tham số đã validate.
  2. Tên bảng phải nằm trong whitelist (đăng ký lúc khởi tạo hoặc
     register_table) VÀ khớp regex identifier.
  3. Tên cột validate qua PRAGMA table_info của bảng thật.
  4. Mọi giá trị đi qua placeholder "?" (parameterized) — không bao giờ nối
     chuỗi vào câu lệnh.
  5. Mệnh đề WHERE chỉ cho phép dạng đơn giản: "col OP ?" nối bằng AND/OR,
     OP thuộc {=, !=, <>, <, >, <=, >=, LIKE, IS NULL, IS NOT NULL}.
  6. Đường dẫn file DB phải nằm trong base_dir (chống path traversal),
     mặc định là thư mục hiện tại.

Chỉ dùng stdlib (sqlite3, re, os).
"""

import os
import re
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["SQLiteError", "SQLiteClient"]

# Tên bảng/cột hợp lệ: identifier SQL đơn giản (không dấu cách, không quote).
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# WHERE cho phép: "col OP ?" hoặc "col IS [NOT] NULL", nối AND/OR, có ngoặc.
_WHERE_TOKEN_RE = re.compile(
    r"""
    ^\s*
    (?P<col>[A-Za-z_][A-Za-z0-9_]*)          # tên cột
    \s*
    (?P<op>=|!=|<>|<=|>=|<|>|LIKE|IS)       # toán tử cho phép
    \s*
    (?P<rest>\?|NOT\s+NULL|NULL)?            # "?" hoặc NULL / NOT NULL
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)
_WHERE_SPLIT_RE = re.compile(r"\s+(AND|OR)\s+", re.IGNORECASE)


class SQLiteError(Exception):
    """Lỗi DB: validate thất bại, bảng chưa whitelist, hoặc lỗi sqlite."""


class SQLiteClient:
    """Client SQLite với whitelist bảng + parameterized query bắt buộc."""

    def __init__(
        self,
        db_path: str,
        allowed_tables: Optional[Sequence[str]] = None,
        base_dir: Optional[str] = None,
    ):
        """db_path: file .db (tạo mới nếu chưa có). base_dir: root cho phép
        chứa file DB (mặc định: thư mục làm việc hiện tại)."""
        if not isinstance(db_path, str) or not db_path.strip():
            raise SQLiteError("db_path phải là string không rỗng")
        root = os.path.realpath(base_dir or os.getcwd())
        resolved = os.path.realpath(db_path.strip())
        if resolved != root and not resolved.startswith(root + os.sep):
            raise SQLiteError(
                "db_path nằm ngoài base_dir (chống path traversal): %r" % db_path
            )
        parent = os.path.dirname(resolved)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            self._conn = sqlite3.connect(resolved, timeout=10.0)
            self._conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            raise SQLiteError(f"không mở được DB {resolved}: {exc}")
        self._whitelist: set = set()
        for t in allowed_tables or []:
            self.register_table(t)

    # ---------------------------------------------------------- validate
    def _check_table(self, table: str) -> str:
        """Validate tên bảng: identifier hợp lệ + đã whitelist + tồn tại thật."""
        if not isinstance(table, str) or not _IDENTIFIER_RE.match(table):
            raise SQLiteError(f"tên bảng không hợp lệ: {table!r}")
        if table not in self._whitelist:
            raise SQLiteError(
                f"bảng {table!r} chưa được whitelist — gọi register_table() trước"
            )
        # Bảng whitelist nhưng bị xóa ngoài luồng? kiểm tra tồn tại thật.
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        if cur.fetchone() is None:
            raise SQLiteError(f"bảng {table!r} không tồn tại trong DB")
        return table

    def _columns(self, table: str) -> List[str]:
        """Danh sách cột thật của bảng (từ PRAGMA) — dùng để validate."""
        self._check_table(table)
        cur = self._conn.execute(f'PRAGMA table_info("{table}")')
        return [row["name"] for row in cur.fetchall()]

    def _check_columns(self, table: str, cols: Sequence[str]) -> List[str]:
        real = set(self._columns(table))
        out = []
        for c in cols:
            if not isinstance(c, str) or not _IDENTIFIER_RE.match(c):
                raise SQLiteError(f"tên cột không hợp lệ: {c!r}")
            if c not in real:
                raise SQLiteError(f"cột {c!r} không tồn tại trong bảng {table!r}")
            out.append(c)
        return out

    def _parse_where(
        self, table: str, where: Optional[str]
    ) -> Tuple[str, List[str]]:
        """Validate mệnh đề WHERE đơn giản, trả về (sql_where, [cột dùng]).

        Chỉ chấp nhận các điều kiện "col OP ?" / "col IS [NOT] NULL" nối
        bằng AND/OR (có thể bọc ngoặc tròn ở ngoài). Mọi thứ khác -> từ chối.
        """
        if not where:
            return "", []
        w = where.strip()
        # Cho phép 1 cặp ngoặc bọc ngoài cùng.
        if w.startswith("(") and w.endswith(")"):
            w = w[1:-1].strip()
        if not w:
            raise SQLiteError("mệnh đề WHERE rỗng")
        if ";" in w or "--" in w or "/*" in w:
            raise SQLiteError("WHERE chứa ký tự/cụm bị cấm (chống injection)")
        cols = self._columns(table)
        parts = _WHERE_SPLIT_RE.split(w)
        used: List[str] = []
        for i, part in enumerate(parts):
            if i % 2 == 1:  # AND / OR
                if part.upper() not in ("AND", "OR"):
                    raise SQLiteError(f"toán tử logic không cho phép: {part!r}")
                continue
            m = _WHERE_TOKEN_RE.match(part)
            if not m:
                raise SQLiteError(
                    f"điều kiện WHERE không hỗ trợ: {part!r} "
                    "(chỉ cho: col = ? / col LIKE ? / col IS [NOT] NULL, nối AND/OR)"
                )
            col = m.group("col")
            if col not in cols:
                raise SQLiteError(f"cột {col!r} không tồn tại trong bảng {table!r}")
            used.append(col)
        return w, used

    # ------------------------------------------------------------- API
    def register_table(self, table: str) -> None:
        """Thêm bảng vào whitelist (tên phải là identifier + bảng tồn tại)."""
        if not isinstance(table, str) or not _IDENTIFIER_RE.match(table):
            raise SQLiteError(f"tên bảng không hợp lệ: {table!r}")
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        if cur.fetchone() is None:
            raise SQLiteError(
                f"không whitelist được: bảng {table!r} không tồn tại trong DB"
            )
        self._whitelist.add(table)

    def tables(self) -> List[str]:
        """Danh sách bảng đã whitelist."""
        return sorted(self._whitelist)

    def query(
        self,
        table: str,
        columns: Optional[Sequence[str]] = None,
        where: Optional[str] = None,
        params: Sequence[Any] = (),
        order_by: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """SELECT an toàn: chỉ đọc, mọi giá trị qua placeholder.

        columns=None -> SELECT *. where: xem _parse_where. params: tuple/list
        giá trị cho các dấu "?" trong where (đếm phải khớp).
        """
        self._check_table(table)
        if columns:
            cols = self._check_columns(table, list(columns))
            col_sql = ", ".join(f'"{c}"' for c in cols)
        else:
            col_sql = "*"
        where_sql, _used = self._parse_where(table, where)
        n_marks = where_sql.count("?") if where_sql else 0
        if n_marks != len(params):
            raise SQLiteError(
                f"WHERE cần {n_marks} param nhưng nhận {len(params)}"
            )
        order_sql = ""
        if order_by:
            if not isinstance(order_by, str):
                raise SQLiteError("order_by phải là string")
            ob = order_by.strip()
            desc = ob.upper().endswith(" DESC")
            base = ob[:-5].strip() if desc else ob[:-4].strip() if ob.upper().endswith(" ASC") else ob
            self._check_columns(table, [base])
            order_sql = f' ORDER BY "{base}"' + (" DESC" if desc else "")
        try:
            limit = max(1, min(int(limit), 10_000))
        except (TypeError, ValueError):
            limit = 100
        sql = f'SELECT {col_sql} FROM "{table}"'
        if where_sql:
            sql += f" WHERE {where_sql}"
        sql += order_sql + f" LIMIT {limit}"
        try:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(row) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            raise SQLiteError(f"query thất bại: {exc}")

    def insert(self, table: str, row: Dict[str, Any]) -> int:
        """INSERT 1 dòng (dict {cột: giá trị}); trả về rowid. Toàn bộ giá trị
        qua placeholder — không nối chuỗi."""
        self._check_table(table)
        if not isinstance(row, dict) or not row:
            raise SQLiteError("row phải là dict không rỗng")
        cols = self._check_columns(table, list(row.keys()))
        placeholders = ", ".join("?" for _ in cols)
        col_sql = ", ".join(f'"{c}"' for c in cols)
        sql = f'INSERT INTO "{table}" ({col_sql}) VALUES ({placeholders})'
        try:
            cur = self._conn.execute(sql, tuple(row[c] for c in cols))
            self._conn.commit()
            return int(cur.lastrowid)
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise SQLiteError(f"insert thất bại: {exc}")

    def update(
        self,
        table: str,
        values: Dict[str, Any],
        where: str,
        params: Sequence[Any] = (),
    ) -> int:
        """UPDATE an toàn: values là dict {cột: giá trị mới} (placeholder),
        where BẮT BUỘC (chống update toàn bảng do quên điều kiện). Trả về số
        dòng bị ảnh hưởng."""
        self._check_table(table)
        if not isinstance(values, dict) or not values:
            raise SQLiteError("values phải là dict không rỗng")
        if not where or not where.strip():
            raise SQLiteError("update bắt buộc có WHERE (chống update toàn bảng)")
        cols = self._check_columns(table, list(values.keys()))
        where_sql, _used = self._parse_where(table, where)
        n_marks = where_sql.count("?")
        if n_marks != len(params):
            raise SQLiteError(
                f"WHERE cần {n_marks} param nhưng nhận {len(params)}"
            )
        set_sql = ", ".join(f'"{c}" = ?' for c in cols)
        sql = f'UPDATE "{table}" SET {set_sql} WHERE {where_sql}'
        try:
            cur = self._conn.execute(
                sql, tuple(values[c] for c in cols) + tuple(params)
            )
            self._conn.commit()
            return int(cur.rowcount)
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise SQLiteError(f"update thất bại: {exc}")

    def execute_script(self, script: str) -> None:
        """Chạy script DDL (CREATE TABLE...) — chỉ cho setup, không phải tool
        cho agent. Validate: cấm mọi statement ngoài CREATE TABLE / CREATE INDEX."""
        if not isinstance(script, str) or not script.strip():
            raise SQLiteError("script rỗng")
        for stmt in script.split(";"):
            s = stmt.strip()
            if not s:
                continue
            head = s.split(None, 1)[0].upper() if s.split() else ""
            if head != "CREATE":
                raise SQLiteError(
                    f"execute_script chỉ cho CREATE TABLE/INDEX, gặp: {head!r}"
                )
        try:
            self._conn.executescript(script)
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise SQLiteError(f"script thất bại: {exc}")

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
