"""Safe SQLite for Boom: SQL-injection-proof by design.

Principles (never trust input, even from the agent):
  1. NO raw-SQL API — only query()/insert()/update() building statements
     from validated parameters.
  2. Table names must be whitelisted (registered at init or via
     register_table) AND match the identifier regex.
  3. Column names validated against the real table's PRAGMA table_info.
  4. Every value goes through "?" placeholders (parameterized) — never string
     concatenation into the statement.
  5. WHERE clauses only allow simple "col OP ?" joined by AND/OR,
     OP in {=, !=, <>, <, >, <=, >=, LIKE, IS NULL, IS NOT NULL}.
  6. The DB file path must be inside base_dir (anti path traversal),
     defaulting to the current directory.

Stdlib only (sqlite3, re, os).
"""

import os
import re
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["SQLiteError", "SQLiteClient"]

# Valid table/column names: simple SQL identifiers (no spaces, no quotes).
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Allowed WHERE: "col OP ?" or "col IS [NOT] NULL", joined by AND/OR, outer parens ok.
_WHERE_TOKEN_RE = re.compile(
    r"""
    ^\s*
    (?P<col>[A-Za-z_][A-Za-z0-9_]*)          # column name
    \s*
    (?P<op>=|!=|<>|<=|>=|<|>|LIKE|IS)       # allowed operators
    \s*
    (?P<rest>\?|NOT\s+NULL|NULL)?            # "?" or NULL / NOT NULL
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)
_WHERE_SPLIT_RE = re.compile(r"\s+(AND|OR)\s+", re.IGNORECASE)


class SQLiteError(Exception):
    """DB error: validation failed, table not whitelisted, or sqlite error."""


class SQLiteClient:
    """SQLite client with a table whitelist + mandatory parameterized queries."""

    def __init__(
        self,
        db_path: str,
        allowed_tables: Optional[Sequence[str]] = None,
        base_dir: Optional[str] = None,
    ):
        """db_path: the .db file (created if missing). base_dir: allowed root
        for the DB file (default: current working directory)."""
        if not isinstance(db_path, str) or not db_path.strip():
            raise SQLiteError("db_path must be a non-empty string")
        root = os.path.realpath(base_dir or os.getcwd())
        resolved = os.path.realpath(db_path.strip())
        if resolved != root and not resolved.startswith(root + os.sep):
            raise SQLiteError(
                "db_path is outside base_dir (anti path traversal): %r" % db_path
            )
        parent = os.path.dirname(resolved)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            self._conn = sqlite3.connect(resolved, timeout=10.0)
            self._conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            raise SQLiteError(f"could not open DB {resolved}: {exc}")
        self._whitelist: set = set()
        for t in allowed_tables or []:
            self.register_table(t)

    # ---------------------------------------------------------- validation
    def _check_table(self, table: str) -> str:
        """Validates a table name: valid identifier + whitelisted + really exists."""
        if not isinstance(table, str) or not _IDENTIFIER_RE.match(table):
            raise SQLiteError(f"invalid table name: {table!r}")
        if table not in self._whitelist:
            raise SQLiteError(
                f"table {table!r} is not whitelisted — call register_table() first"
            )
        # Whitelisted but deleted out-of-band? verify it really exists.
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        if cur.fetchone() is None:
            raise SQLiteError(f"table {table!r} does not exist in the DB")
        return table

    def _columns(self, table: str) -> List[str]:
        """The table's real column list (from PRAGMA) — used for validation."""
        self._check_table(table)
        cur = self._conn.execute(f'PRAGMA table_info("{table}")')
        return [row["name"] for row in cur.fetchall()]

    def _check_columns(self, table: str, cols: Sequence[str]) -> List[str]:
        real = set(self._columns(table))
        out = []
        for c in cols:
            if not isinstance(c, str) or not _IDENTIFIER_RE.match(c):
                raise SQLiteError(f"invalid column name: {c!r}")
            if c not in real:
                raise SQLiteError(f"column {c!r} does not exist in table {table!r}")
            out.append(c)
        return out

    def _parse_where(
        self, table: str, where: Optional[str]
    ) -> Tuple[str, List[str]]:
        """Validates a simple WHERE clause, returns (sql_where, [columns used]).

        Only accepts "col OP ?" / "col IS [NOT] NULL" conditions joined by
        AND/OR (one outer paren pair allowed). Everything else -> rejected.
        """
        if not where:
            return "", []
        w = where.strip()
        # Allow one wrapping paren pair.
        if w.startswith("(") and w.endswith(")"):
            w = w[1:-1].strip()
        if not w:
            raise SQLiteError("empty WHERE clause")
        if ";" in w or "--" in w or "/*" in w:
            raise SQLiteError("WHERE contains banned chars/sequences (anti injection)")
        cols = self._columns(table)
        parts = _WHERE_SPLIT_RE.split(w)
        used: List[str] = []
        for i, part in enumerate(parts):
            if i % 2 == 1:  # AND / OR
                if part.upper() not in ("AND", "OR"):
                    raise SQLiteError(f"disallowed logic operator: {part!r}")
                continue
            m = _WHERE_TOKEN_RE.match(part)
            if not m:
                raise SQLiteError(
                    f"unsupported WHERE condition: {part!r} "
                    "(only: col = ? / col LIKE ? / col IS [NOT] NULL, joined by AND/OR)"
                )
            col = m.group("col")
            if col not in cols:
                raise SQLiteError(f"column {col!r} does not exist in table {table!r}")
            used.append(col)
        return w, used

    # ------------------------------------------------------------- API
    def register_table(self, table: str) -> None:
        """Adds a table to the whitelist (must be a valid identifier + exist)."""
        if not isinstance(table, str) or not _IDENTIFIER_RE.match(table):
            raise SQLiteError(f"invalid table name: {table!r}")
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        if cur.fetchone() is None:
            raise SQLiteError(
                f"cannot whitelist: table {table!r} does not exist in the DB"
            )
        self._whitelist.add(table)

    def tables(self) -> List[str]:
        """Whitelisted tables."""
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
        """Safe SELECT: read-only, every value through placeholders.

        columns=None -> SELECT *. where: see _parse_where. params: tuple/list
        of values for the "?" marks in where (counts must match).
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
                f"WHERE needs {n_marks} param(s) but got {len(params)}"
            )
        order_sql = ""
        if order_by:
            if not isinstance(order_by, str):
                raise SQLiteError("order_by must be a string")
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
            raise SQLiteError(f"query failed: {exc}")

    def insert(self, table: str, row: Dict[str, Any]) -> int:
        """INSERTs 1 row (dict {column: value}); returns the rowid. Every value
        goes through placeholders — no string concatenation."""
        self._check_table(table)
        if not isinstance(row, dict) or not row:
            raise SQLiteError("row must be a non-empty dict")
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
            raise SQLiteError(f"insert failed: {exc}")

    def update(
        self,
        table: str,
        values: Dict[str, Any],
        where: str,
        params: Sequence[Any] = (),
    ) -> int:
        """Safe UPDATE: values is a dict {column: new value} (placeholders),
        where is MANDATORY (guards against whole-table updates from a forgotten
        condition). Returns affected rows."""
        self._check_table(table)
        if not isinstance(values, dict) or not values:
            raise SQLiteError("values must be a non-empty dict")
        if not where or not where.strip():
            raise SQLiteError("update requires a WHERE (anti whole-table update)")
        cols = self._check_columns(table, list(values.keys()))
        where_sql, _used = self._parse_where(table, where)
        n_marks = where_sql.count("?")
        if n_marks != len(params):
            raise SQLiteError(
                f"WHERE needs {n_marks} param(s) but got {len(params)}"
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
            raise SQLiteError(f"update failed: {exc}")

    def execute_script(self, script: str) -> None:
        """Runs a DDL script (CREATE TABLE...) — for setup only, not an agent
        tool. Validates: bans every statement except CREATE TABLE / CREATE INDEX."""
        if not isinstance(script, str) or not script.strip():
            raise SQLiteError("empty script")
        for stmt in script.split(";"):
            s = stmt.strip()
            if not s:
                continue
            head = s.split(None, 1)[0].upper() if s.split() else ""
            if head != "CREATE":
                raise SQLiteError(
                    f"execute_script only allows CREATE TABLE/INDEX, got: {head!r}"
                )
        try:
            self._conn.executescript(script)
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise SQLiteError(f"script failed: {exc}")

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
