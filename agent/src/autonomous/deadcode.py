"""deadcode.py -- Phat hien dead code (ham/bien/class khong dung) it false positive.

Chi dung stdlib (ast).

Triet ly: THA BO SOT CON HON BAO SAI -- chi tra ve finding khi do tin
cay cao (confidence == "high").

Cac false positive da loai tru (so voi rule dead_code cu trong
agent/src/autonomous/codereview.py):
  1. Bien module chi duoc dung trong than ham -- rule cu khong di vao
     than ham nen bao sai; o day moi Name-Load toan module deu duoc tinh.
  2. Bien dieu khien vong lap for / with-as / except-as -- day la
     binding cua luong dieu khien, khong phai dead code.
  3. Unpacking ``a, b = ...`` -- khong dung ``b`` thuong la chu dich.
  4. Ten trong ``__all__`` (public API duoc export).
  5. Ham co decorator -- framework co the goi (route, register, property...).
  6. Ham/phuong thuc ``__dunder__`` -- Python tu goi.
  7. Tham chieu qua chuoi -- ``getattr(obj, "ten_ham")``, registry dang chuoi.
  8. Entry point -- ham duoc goi trong ``if __name__ == "__main__"``.
  9. Ham public cua module co docstring -- coi la API cua library.
 10. Ham ``test_*`` va hook ``setUp``/``tearDown`` -- framework test tu goi.
 11. Bien bi ``del`` -- xoa chu dich, coi nhu da dung.
  12. Bien rieng tu ``_ten`` khong dung -- chi tin cay trung binh -> bo qua.

Gioi han da biet (chap nhan bo sot de giu do chinh xac):
  - Cap ham private chi goi lan nhau (de quy tuong ho) se bi bo sot.
  - Import duoc bo qua han (import co the mang side-effect).
  - Khong phan tich lien file.
"""

import ast
import re
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    """Mot phat hien dead code."""
    name: str        # ten ham/bien/class
    kind: str        # 'function' | 'method' | 'class' | 'variable'
    lineno: int      # dong dinh nghia
    confidence: str  # luon 'high' -- chi bao khi chac chan
    ly_do: str = ""  # giai thich tieng Viet


# ---------------------------------------------------------------------------
# Hang so
# ---------------------------------------------------------------------------

# Hook ma framework test tu goi -- khong bao gio goi truc tiep trong code.
_FRAMEWORK_HOOKS = frozenset({
    "setUp", "tearDown", "setUpClass", "tearDownClass",
    "setUpModule", "tearDownModule",
    "setup_method", "teardown_method", "setup_class", "teardown_class",
    "setup_function", "teardown_function",
})

_LY_DO = {
    "function": "ham '%s' khong duoc goi hay tham chieu o bat cu dau",
    "method": "phuong thuc '%s' khong duoc goi (ke ca qua self.)",
    "class": "class '%s' khong duoc dung (khong khoi tao, ke thua hay annotation)",
    "variable": "bien '%s' duoc gan nhung khong duoc doc o dau",
}


# ---------------------------------------------------------------------------
# Tien ich
# ---------------------------------------------------------------------------

def _la_dunder(ten):
    """Ten dang __xxx__ -- Python tu goi, khong bao gio la dead code."""
    return ten.startswith("__") and ten.endswith("__") and len(ten) > 4


def _doc_all(tree):
    """Doc ten trong ``__all__ = [...]`` o pham vi module."""
    ten = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for e in node.value.elts:
                            if isinstance(e, ast.Constant) \
                                    and isinstance(e.value, str):
                                ten.add(e.value)
    return ten


# ---------------------------------------------------------------------------
# Bo thu thap "dau vet su dung"
# ---------------------------------------------------------------------------

class _BoThuThapDung(ast.NodeVisitor):
    """Thu thap moi dau vet cho thay ten duoc dung:

    - Name o ngu canh Load (goi ham, doc bien, truyen tham chieu...)
    - Name bi ``del`` (xoa chu dich -> coi nhu da dung)
    - Attribute.attr (self._rieng(), obj.chay, C.TIMEOUT)
    - Moi string literal (de bat getattr(obj, "ten") / registry chuoi)
    - Ten trong global/nonlocal
    - ``x += 1`` vua doc vua ghi -> coi nhu da dung x
    """

    def __init__(self):
        self.ten_load = set()
        self.thuoc_tinh = set()
        self.chuoi = []

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Load, ast.Del)):
            self.ten_load.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        self.thuoc_tinh.add(node.attr)
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            self.chuoi.append(node.value)
        # Constant khong co node con -> khong can generic_visit

    def visit_AugAssign(self, node):
        if isinstance(node.target, ast.Name):
            self.ten_load.add(node.target.id)
        self.generic_visit(node)

    def visit_Global(self, node):
        self.ten_load.update(node.names)
        self.generic_visit(node)

    def visit_Nonlocal(self, node):
        self.ten_load.update(node.names)
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Dinh nghia (ham / method / class / bien)
# ---------------------------------------------------------------------------

@dataclass
class _DinhNghia:
    name: str
    kind: str          # 'function' | 'method' | 'class' | 'variable'
    lineno: int
    co_decorator: bool = False
    co_docstring: bool = False
    pham_vi: str = "module"  # 'module' | 'class:<Ten>'


class _ThuDinhNghia(ast.NodeVisitor):
    """Thu thap dinh nghia o pham vi module / than class.

    - Khong di vao than ham long (scope rieng, xu ly o buoc sau).
    - Chi xet gan don gian 1 target (``x = ...``); unpacking ``a, b = ...``
      la chu dich nen bo qua; bien lap for / with-as / except-as la
      binding cua luong dieu khien nen bo qua.
    - ``f = lambda: ...`` tinh nhu function.
    - Khai bao kieu tran ``x: int`` (khong gan gia tri) khong tinh.
    """

    def __init__(self):
        self.dinh_nghias = []
        self._pham_vi = "module"

    def _them_bien(self, target, lineno):
        if isinstance(target, ast.Name) and target.id != "_" \
                and not _la_dunder(target.id) and target.id != "__all__":
            self.dinh_nghias.append(_DinhNghia(
                target.id, "variable", lineno, pham_vi=self._pham_vi))

    def visit_Assign(self, node):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, ast.Lambda):
                self.dinh_nghias.append(_DinhNghia(
                    node.targets[0].id, "function", node.lineno,
                    pham_vi=self._pham_vi))
            else:
                self._them_bien(node.targets[0], node.lineno)
        self.visit(node.value)

    def visit_AnnAssign(self, node):
        # chi tinh khi co gan gia tri that (``x: int = 5``)
        if node.value is not None:
            self._them_bien(node.target, node.lineno)
            self.visit(node.value)

    def visit_NamedExpr(self, node):
        self._them_bien(node.target, node.lineno)
        self.visit(node.value)

    def visit_FunctionDef(self, node):
        kind = "method" if self._pham_vi.startswith("class:") else "function"
        self.dinh_nghias.append(_DinhNghia(
            node.name, kind, node.lineno,
            co_decorator=bool(node.decorator_list),
            co_docstring=bool(ast.get_docstring(node)),
            pham_vi=self._pham_vi))
        # than ham la scope rieng -> khong di vao (bien cuc bo xu ly rieng)

    def visit_AsyncFunctionDef(self, node):
        self.visit_FunctionDef(node)

    def visit_ClassDef(self, node):
        self.dinh_nghias.append(_DinhNghia(
            node.name, "class", node.lineno,
            co_decorator=bool(node.decorator_list),
            co_docstring=bool(ast.get_docstring(node)),
            pham_vi=self._pham_vi))
        # di vao than class de lay method / bien class
        truoc = self._pham_vi
        self._pham_vi = "class:" + node.name
        for stmt in node.body:
            self.visit(stmt)
        self._pham_vi = truoc


class _ThuBienCucBo(ast.NodeVisitor):
    """Bien gan trong 1 than ham; khong di vao ham/class/lambda long."""

    def __init__(self):
        self.gan = {}  # name -> lineno (lan gan dau tien)

    def _ghi(self, target, lineno):
        if isinstance(target, ast.Name) and target.id != "_" \
                and not _la_dunder(target.id):
            self.gan.setdefault(target.id, lineno)

    def visit_Assign(self, node):
        if len(node.targets) == 1:
            self._ghi(node.targets[0], node.lineno)
        self.visit(node.value)

    def visit_AnnAssign(self, node):
        if node.value is not None:
            self._ghi(node.target, node.lineno)
            self.visit(node.value)

    def visit_NamedExpr(self, node):
        self._ghi(node.target, node.lineno)
        self.visit(node.value)

    def visit_FunctionDef(self, node):
        pass  # scope rieng

    def visit_AsyncFunctionDef(self, node):
        pass

    def visit_ClassDef(self, node):
        pass

    def visit_Lambda(self, node):
        pass


# ---------------------------------------------------------------------------
# Analyzer chinh
# ---------------------------------------------------------------------------

class DeadCodeAnalyzer:
    """Phan tich dead code voi do tin cay cao."""

    def analyze(self, source):
        """Phan tich source code, tra ve list[Finding] (chi confidence 'high').

        Syntax loi -> tra ve [] (khong doan mo).
        """
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []

        # 1) thu thap moi dau vet su dung tren toan module
        dung = _BoThuThapDung()
        dung.visit(tree)

        ctx = {
            "ten_load": dung.ten_load,
            "thuoc_tinh": dung.thuoc_tinh,
            "chuoi": dung.chuoi,
            "all_names": _doc_all(tree),
            "module_doc": bool(ast.get_docstring(tree)),
        }

        findings = []

        # 2) dinh nghia o pham vi module / class
        thu_dn = _ThuDinhNghia()
        thu_dn.visit(tree)
        for dn in thu_dn.dinh_nghias:
            if not self._duoc_mien_tru(dn, ctx):
                findings.append(Finding(
                    dn.name, dn.kind, dn.lineno, "high",
                    _LY_DO[dn.kind] % dn.name))

        # 3) bien cuc bo trong tung ham / method
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                thu_bien = _ThuBienCucBo()
                for stmt in node.body:
                    thu_bien.visit(stmt)
                # dau vet dung: quet ca ham long ben trong
                dung_trong = _BoThuThapDung()
                dung_trong.visit(node)
                for ten, lineno in thu_bien.gan.items():
                    if ten in dung_trong.ten_load:
                        continue
                    if self._nhac_trong_chuoi(ten, dung_trong.chuoi):
                        continue
                    if ten.startswith("_"):
                        # bien rieng tu khong dung: tin cay trung binh -> bo qua
                        continue
                    findings.append(Finding(
                        ten, "variable", lineno, "high",
                        "bien cuc bo '%s' duoc gan nhung khong dung "
                        "trong ham '%s'" % (ten, node.name)))

        findings.sort(key=lambda f: (f.lineno, f.name))
        return findings

    # -- mien tru false positive -------------------------------------------

    @staticmethod
    def _duoc_mien_tru(dn, ctx):
        """True = dinh nghia nay duoc mien, khong phai dead code."""
        ten = dn.name

        # ten public duoc export qua __all__
        if ten in ctx["all_names"]:
            return True
        # co decorator -> framework co the goi (route, register, property...)
        if dn.co_decorator:
            return True
        # __dunder__ do Python tu goi
        if _la_dunder(ten):
            return True

        if dn.kind in ("function", "method"):
            # framework test tu goi
            if ten.startswith("test_") or ten in _FRAMEWORK_HOOKS:
                return True
            # duoc goi / tham chieu truc tiep o dau do (ke ca entry point
            # trong if __name__ == "__main__" -- do cung la Name-Load)
            if ten in ctx["ten_load"]:
                return True
            # goi qua thuoc tinh: self.x(), obj.x, mod.x
            if ten in ctx["thuoc_tinh"]:
                return True
            # tham chieu qua chuoi: getattr(obj, "ten"), registry dang chuoi
            if DeadCodeAnalyzer._nhac_trong_chuoi(ten, ctx["chuoi"]):
                return True
            # ham public cua module library (co docstring) la API chu dich
            if not ten.startswith("_") and ctx["module_doc"]:
                return True
            # ham public co docstring rieng cung coi la API chu dich
            if not ten.startswith("_") and dn.co_docstring:
                return True
            return False

        if dn.kind == "class":
            if ten in ctx["ten_load"]:
                return True
            if ten in ctx["thuoc_tinh"]:
                return True
            if DeadCodeAnalyzer._nhac_trong_chuoi(ten, ctx["chuoi"]):
                return True
            if not ten.startswith("_") and (ctx["module_doc"] or dn.co_docstring):
                return True
            return False

        # dn.kind == "variable"
        if ten in ctx["ten_load"]:
            return True
        # bien cua class doc qua Class.TEN / self.TEN
        if dn.pham_vi.startswith("class:") and ten in ctx["thuoc_tinh"]:
            return True
        if DeadCodeAnalyzer._nhac_trong_chuoi(ten, ctx["chuoi"]):
            return True
        if not ten.startswith("_") and ctx["module_doc"]:
            return True
        # bien rieng tu khong dung: chi tin cay trung binh -> mien (khong bao)
        if ten.startswith("_"):
            return True
        return False

    @staticmethod
    def _nhac_trong_chuoi(ten, chuoi_list):
        """Ten co duoc nhac den (nguyen tu) trong string literal nao khong.

        Dung de bat getattr(obj, "ten_ham") va registry dang chuoi.
        Ten qua ngan (<3 ky tu) de trung ngau nhien -> bo qua.
        """
        if len(ten) < 3:
            return False
        for s in chuoi_list:
            if ten in s and re.search(r"\b%s\b" % re.escape(ten), s):
                return True
        return False
