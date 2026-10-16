"""Skill perf-optimizer: phan tich code Python tim bottleneck hieu nang.

Phat hien bang AST (stdlib):
  - nested_loop      : vong lap long nhau O(n^2) tro len
  - io_in_loop       : I/O lap lai trong vong lap (open, requests, socket)
  - string_concat    : noi chuoi bang += trong vong lap
  - n_plus_one       : query DB trong vong lap (N+1 query)
  - missing_cache    : goi ham ton kem lap lai trong vong lap
  - list_membership  : `x in list` trong vong lap (nen dung set)

Output: suggestions [{file, line, problem, optimized_code}].

Chay doc lap:
    python3 optimizer.py <file.py|thu_muc> [--json]

Chi dung stdlib (ast) + pydantic.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Dict, List, Literal, Set

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Schema output
# ---------------------------------------------------------------------------

class PerfSuggestion(BaseModel):
    """1 goi y toi uu hieu nang."""
    file: str = Field(description="duong dan file")
    line: int = Field(description="so dong bat dau vong lap/ham")
    pattern: str = Field(
        description="mau bottleneck: nested_loop|io_in_loop|string_concat|"
                    "n_plus_one|missing_cache|list_membership")
    severity: Literal["high", "medium", "low"] = Field(description="muc do")
    problem: str = Field(description="mo ta van de (tieng Viet)")
    optimized_code: str = Field(description="code mau da toi uu (tieng Viet)")


# ---------------------------------------------------------------------------
# Nhan dien pattern
# ---------------------------------------------------------------------------

# call I/O: (ten doi tuong, tien to ten ham)
_IO_CALLS = [
    ("", "open"),                       # open(...)
    ("requests", "get"), ("requests", "post"), ("requests", "put"),
    ("requests", "delete"), ("requests", "request"),
    ("urllib", "urlopen"), ("urllib.request", "urlopen"),
    ("socket", "send"), ("socket", "recv"), ("socket", "connect"),
    ("os", "read"), ("os", "write"),
]

_DB_CALLS = ("execute", "executemany", "query", "fetchone", "fetchall",
             "fetchmany", "get", "filter", "first")


def _ten_day_du(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        trong = _ten_day_du(func.value)
        return ("%s.%s" % (trong, func.attr)) if trong else func.attr
    return ""


def _la_vong_lap(node: ast.AST) -> bool:
    return isinstance(node, (ast.For, ast.AsyncFor, ast.While))


def _do_sau_lap(node: ast.AST) -> int:
    """Do sau vong lap long nhau lon nhat trong cay, tinh ca node goc.

    Vd: for > for -> 2; for > if > while -> 2; khong co lap -> 0.
    """
    if _la_vong_lap(node):
        sau_trong = 0
        for con in ast.iter_child_nodes(node):
            sau_trong = max(sau_trong, _do_sau_lap(con))
        return 1 + sau_trong
    sau_max = 0
    for con in ast.iter_child_nodes(node):
        sau_max = max(sau_max, _do_sau_lap(con))
    return sau_max


def _ham_ton_kem(tree: ast.AST) -> Set[str]:
    """Ten cac ham do nguoi dung dinh nghia ma ben trong co vong lap."""
    ket_qua: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(_la_vong_lap(n) for n in ast.walk(node)
                   if n is not node):
                ket_qua.add(node.name)
    return ket_qua


def _bien_lap(node: ast.For | ast.AsyncFor) -> str:
    try:
        return ast.unparse(node.target).strip()
    except Exception:
        return "item"


def _iter_lap(node: ast.For | ast.AsyncFor | ast.While) -> str:
    try:
        if isinstance(node, ast.While):
            return ast.unparse(node.test).strip()
        return ast.unparse(node.iter).strip()
    except Exception:
        return "..."


class _QuetVongLap(ast.NodeVisitor):
    """Duyet moi vong lap cap ngoai cung, kiem tra cac pattern ben trong."""

    def __init__(self, ham_ton_kem: Set[str], file: str):
        self.ham_ton_kem = ham_ton_kem
        self.file = file
        self.suggestions: List[PerfSuggestion] = []
        self._dang_trong_lap = False

    def _them(self, node, pattern, severity, problem, code):
        self.suggestions.append(PerfSuggestion(
            file=self.file, line=node.lineno, pattern=pattern,
            severity=severity, problem=problem, optimized_code=code))

    # -- cac loai vong lap --
    def _visit_lap(self, node):
        # chi check vong lap cap ngoai cung; lap con van duoc duyet
        # (de xuong sau hon) nhung khong check lai -> tranh trung lap
        if self._dang_trong_lap:
            self.generic_visit(node)
            return
        self._dang_trong_lap = True
        try:
            self._check_nested_loop(node)
            self._check_io_trong_lap(node)
            self._check_noi_chuoi(node)
            self._check_n_plus_one(node)
            self._check_thieu_cache(node)
            self._check_list_membership(node)
        finally:
            self._dang_trong_lap = False
        self.generic_visit(node)

    def visit_For(self, node: ast.For):
        self._visit_lap(node)

    def visit_AsyncFor(self, node: ast.AsyncFor):
        self._visit_lap(node)

    def visit_While(self, node: ast.While):
        self._visit_lap(node)

    # -- pattern 1: nested loop --
    def _check_nested_loop(self, node):
        sau = _do_sau_lap(node)
        if sau >= 2:
            bien = _bien_lap(node) if not isinstance(node, ast.While) else "item"
            nguon = _iter_lap(node)
            self._them(
                node, "nested_loop", "high" if sau >= 3 else "medium",
                "Vong lap long nhau %d cap — do phuc tap O(n^%d), cham dan "
                "theo kich thuoc du lieu" % (sau, sau),
                "# Truoc: for ... in %s: for ... in ...  (O(n^%d))\n"
                "# Sau: dung set/dict de tra cuu O(1)\n"
                "lookup = set(%s)  # hoac dict neu can anh xa gia tri\n"
                "for %s in %s:\n"
                "    if %s in lookup:  # O(1) thay vi lap long nhau\n"
                "        ..." % (nguon, sau, nguon, bien, nguon, bien))

    # -- pattern 2: I/O trong vong lap --
    def _check_io_trong_lap(self, node):
        for sub in ast.walk(node):
            if sub is node or not isinstance(sub, ast.Call):
                continue
            ten = _ten_day_du(sub.func)
            for doi_tuong, ham in _IO_CALLS:
                khop = (ten == ham) if not doi_tuong else \
                    ten.startswith(doi_tuong + ".") and ten.endswith("." + ham)
                if khop:
                    self._them(
                        sub, "io_in_loop", "high",
                        "Goi I/O '%s' lap lai trong vong lap — moi lan lap "
                        "ton 1 round-trip I/O" % ten,
                        "# Truoc: for ...: %s(...)  # N lan I/O\n"
                        "# Sau: gom batch roi xu ly 1 lan\n"
                        "#  - doc file: doc 1 lan truoc vong lap\n"
                        "#  - HTTP: dung batch API / asyncio.gather / thread pool\n"
                        "#  - DB: dung executemany / bulk insert" % ten)
                    return  # 1 goi y cho moi vong lap la du

    # -- pattern 3: noi chuoi += trong vong lap --
    def _check_noi_chuoi(self, node):
        for sub in ast.walk(node):
            if isinstance(sub, ast.AugAssign) and isinstance(sub.op, ast.Add):
                muc_tieu = _ten_day_du(sub.target)
                if muc_tieu:
                    self._them(
                        sub, "string_concat", "medium",
                        "Noi chuoi bang '+=' trong vong lap — moi lan noi "
                        "tao chuoi moi O(n), tong O(n^2)",
                        "# Truoc: %s += chunk  (trong vong lap)\n"
                        "# Sau: gom vao list roi join 1 lan\n"
                        "parts = []\n"
                        "for ...:\n"
                        "    parts.append(chunk)\n"
                        "%s = \"\".join(parts)" % (muc_tieu, muc_tieu))
                    return

    # -- pattern 4: N+1 query --
    def _check_n_plus_one(self, node):
        for sub in ast.walk(node):
            if sub is node or not isinstance(sub, ast.Call):
                continue
            ten_ngan = _ten_day_du(sub.func).split(".")[-1]
            if ten_ngan in _DB_CALLS:
                self._them(
                    sub, "n_plus_one", "high",
                    "Query DB '%s' trong vong lap — mau N+1 query: N lan lap "
                    "= N+1 round-trip DB" % _ten_day_du(sub.func),
                    "# Truoc: for id in ids: db.execute('... WHERE id=?', (id,))\n"
                    "# Sau: 1 query duy nhat voi IN\n"
                    "placeholders = ', '.join('?' for _ in ids)\n"
                    "rows = db.execute(\n"
                    "    f'SELECT ... WHERE id IN ({placeholders})', ids).fetchall()")
                return

    # -- pattern 5: thieu cache --
    def _check_thieu_cache(self, node):
        for sub in ast.walk(node):
            if sub is node or not isinstance(sub, ast.Call):
                continue
            ten = _ten_day_du(sub.func)
            if ten in self.ham_ton_kem:
                self._them(
                    sub, "missing_cache", "medium",
                    "Goi ham ton kem '%s' (ben trong co vong lap) lap lai "
                    "trong vong lap — ket qua co the giong nhau" % ten,
                    "# Sau: cache ket qua neu ham thuan (pure function)\n"
                    "from functools import lru_cache\n\n"
                    "@lru_cache(maxsize=1024)\n"
                    "def %s(...):  # tham so phai hashable\n"
                    "    ..." % ten)
                return

    # -- pattern 6: x in list trong vong lap --
    def _check_list_membership(self, node):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Compare):
                for op, comp in zip(sub.ops, sub.comparators):
                    if isinstance(op, (ast.In, ast.NotIn)):
                        # `x in bien` voi bien la Name (kha nang la list)
                        if isinstance(comp, ast.Name):
                            ten_list = comp.id
                            self._them(
                                sub, "list_membership", "low",
                                "'in' tren '%s' trong vong lap — neu la list thi "
                                "moi lan kiem tra la O(n)" % ten_list,
                                "# Sau: doi sang set de kiem tra O(1)\n"
                                "%s_set = set(%s)  # lam 1 lan truoc vong lap\n"
                                "for ...:\n"
                                "    if x in %s_set: ...  # O(1)"
                                % (ten_list, ten_list, ten_list))
                            return


# ---------------------------------------------------------------------------
# API chinh cua skill
# ---------------------------------------------------------------------------

def optimize_source(source: str,
                    filename: str = "<string>") -> List[PerfSuggestion]:
    """Phan tich 1 doan source, tra ve list goi y toi uu."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    quet = _QuetVongLap(_ham_ton_kem(tree), filename)
    quet.visit(tree)
    ket_qua = sorted(quet.suggestions,
                     key=lambda s: ({"high": 0, "medium": 1, "low": 2}[s.severity],
                                    s.line))
    return ket_qua


def optimize_file(path: str | Path) -> List[PerfSuggestion]:
    """Phan tich 1 file."""
    path = Path(path)
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    if "\x00" in source:
        return []
    return optimize_source(source, filename=str(path))


def optimize_dir(root: str | Path) -> List[PerfSuggestion]:
    """Phan tich de quy 1 thu muc."""
    bo_qua = {"__pycache__", ".venv", ".git", "node_modules"}
    ket_qua: List[PerfSuggestion] = []
    for p in sorted(Path(root).rglob("*.py")):
        if any(ph in p.parts for ph in bo_qua):
            continue
        ket_qua.extend(optimize_file(p))
    return ket_qua


def suggestions_to_json(suggestions: List[PerfSuggestion]) -> str:
    return json.dumps([s.model_dump() for s in suggestions],
                      ensure_ascii=False, indent=2)


def to_markdown(suggestions: List[PerfSuggestion],
                tieu_de: str = "Goi y toi uu hieu nang") -> str:
    nhan = {"high": "🔴 Nghiem trong", "medium": "🟡 Trung binh",
            "low": "🔵 Nhe"}
    dong = ["# %s" % tieu_de, "",
            "Tong: %d goi y" % len(suggestions), ""]
    nhom = None
    for s in suggestions:
        if s.severity != nhom:
            nhom = s.severity
            dong += ["## %s" % nhan[nhom], ""]
        dong.append("- `%s:%d` [`%s`] %s" % (s.file, s.line, s.pattern, s.problem))
        dong.append("  ```python")
        dong.extend("  " + d for d in s.optimized_code.splitlines())
        dong.append("  ```")
    if not suggestions:
        dong.append("Khong phat hien bottleneck nao. 🎉")
    dong.append("")
    return "\n".join(dong)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Skill perf-optimizer: tim bottleneck hieu nang trong code Python")
    parser.add_argument("path", help="file .py hoac thu muc can phan tich")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    p = Path(args.path)
    suggestions = optimize_dir(p) if p.is_dir() else optimize_file(p)

    if args.json:
        print(suggestions_to_json(suggestions))
    else:
        print(to_markdown(suggestions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
