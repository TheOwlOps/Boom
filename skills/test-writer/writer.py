"""Skill test-writer: phan tich function bang AST roi sinh test case pytest.

- Happy path: goi ham voi gia tri mau suy luan tu ten bien/annotation/default.
- Edge cases: None, rong (""/[]/{}), bien (0, -1).
- Neu than ham don gian (1 cau return tinh duoc), tu tinh ket qua mong doi
  de sinh assert that; con lai de TODO cho developer dien.

Chay doc lap:
    python3 writer.py <source.py> [-o test_file.py] [--module ten_module]

Chi dung stdlib (ast) + pydantic.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Schema output
# ---------------------------------------------------------------------------

class GeneratedTests(BaseModel):
    """Ket qua sinh test cho 1 file source."""
    source_file: str = Field(description="file source duoc phan tich")
    module_name: str = Field(description="ten module de import trong test")
    functions: List[str] = Field(description="ten cac ham da sinh test")
    test_code: str = Field(description="noi dung file test chuan pytest")
    skipped: List[str] = Field(
        default_factory=list,
        description="cac dinh nghia bi bo qua (method/class phuc tap...)")


# ---------------------------------------------------------------------------
# Suy luan gia tri mau tu ten bien / annotation
# ---------------------------------------------------------------------------

# (regex ten bien, gia tri happy-path dang code-text)
_GOI_Y_TEN: List[tuple] = [
    (r"email|mail", '"test@example.com"'),
    (r"password|passwd|mat_khau", '"Secret123!"'),
    (r"username|user_name|ten_dang_nhap", '"nguoi_dung_1"'),
    (r"\bname\b|ten|full_name|ho_ten", '"Nguyen Van A"'),
    (r"url|link|endpoint|uri", '"https://example.com"'),
    (r"path|file|filename|filepath|duong_dan", '"/tmp/test.txt"'),
    (r"phone|sdt|mobile|dien_thoai", '"0901234567"'),
    (r"token|api_key|secret", '"test-token-123"'),
    (r"price|amount|tien|gia|balance|total", "99.9"),
    (r"rate|ratio|percent|phan_tram", "0.5"),
    (r"count|num|number|so_|quantity|size|length|index|idx|tuoi|age", "5"),
    (r"flag|enabled|active|is_|has_|can_|should_", "True"),
    (r"items|list|data|arr|records|users|danh_sach", "[1, 2, 3]"),
    (r"mapping|dict|config|options|payload|kwargs", '{"key": "value"}'),
    (r"text|content|message|msg|description|desc|title|noi_dung", '"hello world"'),
    (r"\bid\b|_id|ma_", "1"),
    # ten 1 chu cai (a, b, x, y, n, i...) thuong la bien so trong cong thuc
    (r"^[a-z]$", "1"),
]

# (annotation -> gia tri happy-path dang code-text)
_GOI_Y_KIEU: Dict[str, str] = {
    "int": "1", "float": "1.5", "str": '"sample"',
    "bool": "True", "list": "[1, 2, 3]", "dict": '{"a": 1}',
    "tuple": "(1, 2)", "set": "{1, 2}",
}


def _unparse(node: Optional[ast.AST]) -> str:
    """Chuyen AST node ve code-text, loi -> ''."""
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _goi_y_theo_ten(ten: str) -> Optional[str]:
    """Doan gia tri mau dua tren ten bien."""
    t = ten.lower()
    for pattern, gia_tri in _GOI_Y_TEN:
        if re.search(pattern, t):
            return gia_tri
    return None


def _goi_y_theo_kieu(annotation: str) -> Optional[str]:
    """Doan gia tri mau dua tren annotation (ho tro Optional[X], List[X]...)."""
    a = annotation.strip()
    # Optional[X] / X | None -> lay X
    m = re.match(r"Optional\[(.+)\]", a) or re.match(r"(.+)\s*\|\s*None", a)
    if m:
        return _goi_y_theo_kieu(m.group(1)) or "None"
    ten_goc = re.split(r"[\[\(]", a)[0].strip()
    if ten_goc in _GOI_Y_KIEU:
        return _GOI_Y_KIEU[ten_goc]
    if ten_goc in ("List", "list"):
        return "[1, 2, 3]"
    if ten_goc in ("Dict", "dict"):
        return '{"a": 1}'
    return None


def _suy_luan_gia_tri(ten: str, annotation: str, default: str) -> str:
    """Thu tu uu tien: default -> ten bien -> annotation -> 'None'."""
    if default:
        return default
    goi_y = _goi_y_theo_ten(ten)
    if goi_y:
        return goi_y
    goi_y = _goi_y_theo_kieu(annotation)
    if goi_y:
        return goi_y
    return "None"


# ---------------------------------------------------------------------------
# Thu thap thong tin function tu AST
# ---------------------------------------------------------------------------

@dataclass
class _ThamSo:
    ten: str
    annotation: str = ""
    default: str = ""
    kind: str = "pos"  # pos | vararg | kwarg | kwonly


@dataclass
class _HamInfo:
    ten: str
    tham_so: List[_ThamSo] = field(default_factory=list)
    returns: str = ""
    docstring: str = ""
    is_async: bool = False
    node: Any = None


def _thu_thap_ham(tree: ast.AST) -> List[_HamInfo]:
    """Lay cac ham cap module (bo qua method trong class, de placeholder)."""
    ket_qua: List[_HamInfo] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_") and node.name != "__main__":
            continue
        args = node.args
        tham_so: List[_ThamSo] = []
        mac_dinh = [None] * (len(args.args) - len(args.defaults)) \
            + list(args.defaults)
        for arg_node, def_node in zip(args.args, mac_dinh):
            tham_so.append(_ThamSo(
                ten=arg_node.arg,
                annotation=_unparse(arg_node.annotation),
                default=_unparse(def_node),
                kind="pos"))
        if args.vararg:
            tham_so.append(_ThamSo(args.vararg.arg, _unparse(args.vararg.annotation),
                                   kind="vararg"))
        for arg_node, def_node in zip(args.kwonlyargs, args.kw_defaults):
            tham_so.append(_ThamSo(arg_node.arg, _unparse(arg_node.annotation),
                                   _unparse(def_node), kind="kwonly"))
        if args.kwarg:
            tham_so.append(_ThamSo(args.kwarg.arg, _unparse(args.kwarg.annotation),
                                   kind="kwarg"))
        ket_qua.append(_HamInfo(
            ten=node.name, tham_so=tham_so,
            returns=_unparse(node.returns),
            docstring=ast.get_docstring(node) or "",
            is_async=isinstance(node, ast.AsyncFunctionDef),
            node=node))
    return ket_qua


# ---------------------------------------------------------------------------
# Tinh ket qua mong doi cho ham don gian (1 cau return tinh duoc)
# ---------------------------------------------------------------------------

# Cac node AST duoc phep khi danh gia bieu thuc return (khong Call/Attribute).
_NODE_AN_TOAN = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.IfExp, ast.Name, ast.Constant, ast.JoinedStr, ast.FormattedValue,
    ast.Subscript, ast.List, ast.Tuple, ast.Dict, ast.Set,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UAdd, ast.USub, ast.Not, ast.Invert,
    ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Is, ast.IsNot, ast.In, ast.NotIn, ast.Load,
)


def _tinh_ket_qua_mong_doi(ham: _HamInfo,
                           gia_tri_goi: Dict[str, str]) -> Optional[str]:
    """Neu ham chi co 1 cau return tinh duoc -> tra ve repr(ket qua).

    Thay Name (ten tham so) bang gia tri literal roi eval an toan
    (whitelist node, __builtins__ rong).
    """
    node = ham.node
    cau_lenh = [s for s in node.body
                if not (isinstance(s, ast.Expr)
                        and isinstance(s.value, ast.Constant))]
    if len(cau_lenh) != 1 or not isinstance(cau_lenh[0], ast.Return):
        return None
    bieu_thuc = cau_lenh[0].value
    if bieu_thuc is None:
        return None
    for n in ast.walk(bieu_thuc):
        if not isinstance(n, _NODE_AN_TOAN):
            return None  # co Call/Attribute/... -> khong dam tinh

    # Thay the ten tham so bang literal
    def thay_the(n: ast.AST) -> ast.AST:
        if isinstance(n, ast.Name) and n.id in gia_tri_goi:
            try:
                return ast.Constant(ast.literal_eval(gia_tri_goi[n.id]))
            except Exception:
                raise ValueError("khong phai literal")
        for truong, gia_tri_cu in ast.iter_fields(n):
            if isinstance(gia_tri_cu, list):
                setattr(n, truong,
                        [thay_the(c) if isinstance(c, ast.AST) else c
                         for c in gia_tri_cu])
            elif isinstance(gia_tri_cu, ast.AST):
                setattr(n, truong, thay_the(gia_tri_cu))
        return n

    try:
        bieu_thuc_moi = thay_the(ast.parse(ast.unparse(bieu_thuc),
                                           mode="eval").body)
        # Constant moi tao khong co lineno -> fix truoc khi compile
        bieu_thuc_moi = ast.fix_missing_locations(bieu_thuc_moi)
        ket_qua = eval(compile(ast.Expression(bieu_thuc_moi), "<test>",
                               "eval"),
                       {"__builtins__": {}})
        return repr(ket_qua)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Sinh code test
# ---------------------------------------------------------------------------

def _doi_so_goi(tham_so: List[_ThamSo]) -> Dict[str, str]:
    """Tao dict ten -> gia tri code-text cho loi goi ham.

    Cac doi so so duoc lam khac nhau (1, 2, 3...) de test co y nghia hon.
    """
    ket_qua: Dict[str, str] = {}
    da_dung: set = set()
    for ts in tham_so:
        if ts.kind in ("vararg", "kwarg"):
            continue  # *args/**kwargs: bo qua o loi goi mau
        gia_tri = _suy_luan_gia_tri(ts.ten, ts.annotation, ts.default)
        # so nguyen trung nhau -> tang dan de moi doi so khac nhau
        if re.fullmatch(r"-?\d+", gia_tri or ""):
            n = int(gia_tri)
            while str(n) in da_dung:
                n += 1
            gia_tri = str(n)
        ket_qua[ts.ten] = gia_tri
        da_dung.add(gia_tri)
    return ket_qua


def _chuoi_goi(ten_ham: str, doi_so: Dict[str, str]) -> str:
    return "%s(%s)" % (ten_ham, ", ".join(doi_so.values()))


def _edge_cases(doi_so: Dict[str, str]) -> List[tuple]:
    """Sinh cac case bien: (ten_case, doi_so_da_sua, ghi_chu)."""
    cases: List[tuple] = []
    if not doi_so:
        return cases
    ten_dau = next(iter(doi_so))

    # 1. None cho tham so dau tien
    ds_none = dict(doi_so)
    ds_none[ten_dau] = "None"
    cases.append(("none_input", ds_none,
                  "input None — kiem tra ham xu ly an toan, khong crash"))

    # 2/3. Rong + bien dua tren kieu gia tri happy-path
    try:
        gia_tri = ast.literal_eval(doi_so[ten_dau])
    except Exception:
        gia_tri = None
    if isinstance(gia_tri, str):
        ds_rong = dict(doi_so)
        ds_rong[ten_dau] = '""'
        cases.append(("empty_string", ds_rong, "chuoi rong"))
    elif isinstance(gia_tri, list):
        ds_rong = dict(doi_so)
        ds_rong[ten_dau] = "[]"
        cases.append(("empty_list", ds_rong, "list rong"))
    elif isinstance(gia_tri, dict):
        ds_rong = dict(doi_so)
        ds_rong[ten_dau] = "{}"
        cases.append(("empty_dict", ds_rong, "dict rong"))
    if isinstance(gia_tri, (int, float)) and not isinstance(gia_tri, bool):
        for nhan, val in (("zero", "0"), ("negative", "-1")):
            ds_bien = dict(doi_so)
            ds_bien[ten_dau] = val
            cases.append(("boundary_" + nhan, ds_bien,
                          "gia tri bien: %s" % val))
    return cases


def _sinh_test_cho_ham(ham: _HamInfo) -> str:
    """Sinh cac test function cho 1 ham."""
    doi_so = _doi_so_goi(ham.tham_so)
    loi_goi = _chuoi_goi(ham.ten, doi_so)
    mong_doi = _tinh_ket_qua_mong_doi(ham, doi_so)
    dong: List[str] = []

    # -- happy path --
    dong.append("")
    dong.append("")
    dong.append("def test_%s_happy_path():" % ham.ten)
    dong.append('    """Happy path: goi voi gia tri mau hop le."""')
    if mong_doi is not None:
        dong.append("    assert %s == %s" % (loi_goi, mong_doi))
    else:
        dong.append("    result = %s" % loi_goi)
        if ham.returns:
            dong.append("    # TODO: khang dinh kieu/ket qua mong doi (%s)"
                        % ham.returns)
        else:
            dong.append("    # TODO: khang dinh ket qua mong doi cua ham")
        dong.append("    assert result is not None")

    # -- edge cases --
    for ten_case, ds_sua, ghi_chu in _edge_cases(doi_so):
        dong.append("")
        dong.append("")
        dong.append("def test_%s_%s():" % (ham.ten, ten_case))
        dong.append('    """Edge case: %s."""' % ghi_chu)
        dong.append("    # TODO: khang dinh hanh vi mong doi (raise hay tra ve gi)")
        dong.append("    %s" % _chuoi_goi(ham.ten, ds_sua))

    return "\n".join(dong)


def generate_tests(source: str, module_name: str = "module_under_test",
                   source_file: str = "<string>") -> GeneratedTests:
    """Sinh file test pytest tu source code (chuoi)."""
    tree = ast.parse(source)
    ham_list = _thu_thap_ham(tree)

    phan_dau = '''"""Test tu sinh boi skill test-writer — kiem tra lai va bo sung assert TODO."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from %s import (%s)
''' % (module_name, ", ".join(h.ten for h in ham_list) or "pass")

    than = "".join(_sinh_test_cho_ham(h) for h in ham_list)
    if not ham_list:
        than = '\n\n# Khong tim thay ham cap module nao de sinh test.\n'

    return GeneratedTests(
        source_file=source_file,
        module_name=module_name,
        functions=[h.ten for h in ham_list],
        test_code=phan_dau + than + "\n",
        skipped=[],
    )


def write_tests(source_path: str | Path,
                out_path: str | Path | None = None) -> Path:
    """Doc file source, sinh test va ghi ra test_<ten>.py. Tra ve Path output."""
    source_path = Path(source_path)
    source = source_path.read_text(encoding="utf-8")
    if out_path is None:
        out_path = source_path.parent / ("test_%s.py" % source_path.stem)
    ket_qua = generate_tests(source, module_name=source_path.stem,
                             source_file=str(source_path))
    Path(out_path).write_text(ket_qua.test_code, encoding="utf-8")
    return Path(out_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Skill test-writer: sinh test pytest tu AST cua function")
    parser.add_argument("source", help="file .py can sinh test")
    parser.add_argument("-o", "--output", default=None,
                        help="file output (mac dinh: test_<ten>.py canh source)")
    parser.add_argument("--module", default=None,
                        help="ten module de import (mac dinh: ten file source)")
    args = parser.parse_args(argv)

    source_path = Path(args.source)
    source = source_path.read_text(encoding="utf-8")
    ket_qua = generate_tests(
        source,
        module_name=args.module or source_path.stem,
        source_file=str(source_path))
    if args.output:
        Path(args.output).write_text(ket_qua.test_code, encoding="utf-8")
        print("Da ghi: %s (%d ham)" % (args.output, len(ket_qua.functions)))
    else:
        print(ket_qua.test_code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
