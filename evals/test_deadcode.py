"""test_deadcode.py -- Test cho deadcode.py.

Chay: python3 test_deadcode.py
Yeu cau: it nhat 100 assertions, tat ca PASS that.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# chạy từ repo root: deadcode.py nằm ở ../agent/src/autonomous/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src" / "autonomous"))

from deadcode import DeadCodeAnalyzer, Finding

_SO_ASSERT = 0


def _kiem_tra(dieu_kien, mo_ta):
    global _SO_ASSERT
    _SO_ASSERT += 1
    if not dieu_kien:
        raise AssertionError("FAIL: " + mo_ta)


def _ten(findings):
    return {f.name for f in findings}


def _tim(findings, ten):
    for f in findings:
        if f.name == ten:
            return f
    return None


_ANALYZER = DeadCodeAnalyzer()


def khong_bao(nguon, ten, mo_ta):
    """Case false-positive: ten KHONG duoc bao."""
    fs = _ANALYZER.analyze(nguon)
    _kiem_tra(isinstance(fs, list), mo_ta + " (phai tra ve list)")
    _kiem_tra(ten not in _ten(fs),
              "%s: '%s' la FP, khong duoc bao" % (mo_ta, ten))


def bi_bao(nguon, ten, kind, mo_ta):
    """Case dead code that: ten PHAI bi bao dung kind, confidence high."""
    fs = _ANALYZER.analyze(nguon)
    f = _tim(fs, ten)
    _kiem_tra(f is not None, "%s: '%s' phai bi bao dead" % (mo_ta, ten))
    _kiem_tra(f.kind == kind if f else False,
              "%s: kind cua '%s' phai la '%s'" % (mo_ta, ten, kind))
    _kiem_tra(f.confidence == "high" if f else False,
              "%s: confidence cua '%s' phai la high" % (mo_ta, ten))
    _kiem_tra(f.lineno > 0 if f else False,
              "%s: lineno cua '%s' phai > 0" % (mo_ta, ten))
    _kiem_tra(bool(f.ly_do) if f else False,
              "%s: finding cua '%s' phai co ly do" % (mo_ta, ten))


# ===========================================================================
# PHAN 1: FALSE POSITIVE -- khong duoc bao (43 case)
# ===========================================================================

# --- 1. Ten trong __all__ ---
khong_bao('__all__ = ["handler"]\n\ndef handler():\n    pass\n',
          "handler", "__all__ export ham")
khong_bao('__all__ = ("KHO",)\n\nKHO = {}\n',
          "KHO", "__all__ export bien")
khong_bao('__all__ = ["Lop"]\n\nclass Lop:\n    pass\n',
          "Lop", "__all__ export class")

# --- 2. Ham co decorator (framework co the goi) ---
khong_bao('@app.route("/x")\ndef view():\n    pass\n',
          "view", "decorator co doi so")
khong_bao('@register\ndef plugin():\n    pass\n',
          "plugin", "decorator tran")
khong_bao('class A:\n    @property\n    def ten(self):\n        return 1\n',
          "ten", "@property")
khong_bao('import abc\nclass B:\n    @abc.abstractmethod\n'
          '    def chay(self):\n        pass\n',
          "chay", "@abstractmethod")

# --- 3. __dunder__ do Python tu goi ---
khong_bao('class A:\n    def __eq__(self, o):\n        return True\n',
          "__eq__", "dunder method")
khong_bao('class A:\n    def __repr__(self):\n        return "a"\n',
          "__repr__", "dunder __repr__")
khong_bao('__version__ = "1.0"\n', "__version__", "bien dunder module")

# --- 4. Tham chieu qua chuoi / getattr ---
khong_bao('def helper():\n    pass\n\ngetattr(obj, "helper")()\n',
          "helper", "getattr voi chuoi")
khong_bao('class B:\n    def run_task(self):\n        pass\n\n'
          'f = getattr(b, "run_task")\nprint(f)\n',
          "run_task", "getattr method qua chuoi")
khong_bao('def xu_ly():\n    pass\n\nprint("se goi xu_ly sau")\n',
          "xu_ly", "ten xuat hien nguyen tu trong chuoi")

# --- 5. Entry point if __name__ == "__main__" ---
khong_bao('def main():\n    pass\n\nif __name__ == "__main__":\n    main()\n',
          "main", "entry point main()")
khong_bao('def _main():\n    pass\n\nif __name__ == "__main__":\n    _main()\n',
          "_main", "entry point ham private")

# --- 6. Ham public cua module library (co docstring) ---
khong_bao('"""Thu vien tien ich."""\n\ndef cong(a, b):\n    return a + b\n',
          "cong", "public trong module library")
khong_bao('"""Thu vien."""\n\nclass TienIch:\n    pass\n',
          "TienIch", "class public trong library")
khong_bao('"""Thu vien."""\n\nHANG_SO = 10\n',
          "HANG_SO", "bien public trong library")

# --- 7. Callback / truyen tham chieu ---
khong_bao('def on_click(e):\n    pass\n\nbutton.bind(on_click)\n',
          "on_click", "callback truyen tham chieu")
khong_bao('import atexit\ndef on_exit():\n    pass\n\natexit.register(on_exit)\n',
          "on_exit", "dang ky atexit")
khong_bao('import functools\ndef g(x):\n    pass\n\n'
          'h = functools.partial(g, 1)\nprint(h)\n',
          "g", "functools.partial")

# --- 8. Method goi qua self / instance ---
khong_bao('class C:\n    def _tinh(self):\n        return 1\n'
          '    def chay(self):\n        return self._tinh()\n',
          "_tinh", "goi qua self._tinh()")
khong_bao('class D:\n    def chay(self):\n        pass\n\nd = D()\nd.chay()\n',
          "chay", "goi tren instance")

# --- 9. Bien module chi dung trong than ham (FP cua rule cu) ---
khong_bao('CACHE = {}\n\ndef lay(k):\n    return CACHE.get(k)\n',
          "CACHE", "bien module dung trong ham")
khong_bao('DEM = 0\n\ndef tang():\n    global DEM\n    DEM += 1\n',
          "DEM", "global + augassign")

# --- 10. De quy ---
khong_bao('def _dequy(n):\n    return _dequy(n - 1) if n else 0\n',
          "_dequy", "ham de quy tu goi")

# --- 11. Framework test tu goi ---
khong_bao('def test_cong():\n    assert 1 + 1 == 2\n',
          "test_cong", "ham test_*")
khong_bao('class T:\n    def setUp(self):\n        pass\n',
          "setUp", "hook setUp")
khong_bao('class T:\n    def tearDown(self):\n        pass\n',
          "tearDown", "hook tearDown")

# --- 12. Dung lam base class / annotation ---
khong_bao('class Cha:\n    pass\n\nclass Con(Cha):\n    pass\n',
          "Cha", "lam base class")
khong_bao('class CauHinh:\n    pass\n\ndef tai() -> CauHinh:\n    pass\n',
          "CauHinh", "dung trong annotation")

# --- 13. FP cua rule cu: for / with-as / except-as / unpacking ---
khong_bao('for i in range(3):\n    print("x")\n',
          "i", "bien lap for khong dung")
khong_bao('try:\n    pass\nexcept Exception as e:\n    print("loi")\n',
          "e", "bien except khong dung")
khong_bao('with open("f") as f:\n    print("x")\n',
          "f", "bien with-as khong dung")
khong_bao('a, b = 1, 2\nprint(a)\n',
          "b", "unpacking bo qua chu dich")

# --- 14. del / f-string / lambda duoc dung ---
khong_bao('tmp = 1\ndel tmp\n', "tmp", "del coi nhu dung")
khong_bao('ten = "boom"\nprint(f"xin chao {ten}")\n',
          "ten", "dung trong f-string")
khong_bao('f = lambda x: x * 2\nprint(f(3))\n',
          "f", "lambda duoc goi")

# --- 15. Ham public co docstring / dang ky trong dict ---
khong_bao('def cong(a, b):\n    """Cong hai so."""\n    return a + b\n',
          "cong", "ham public co docstring")
khong_bao('def xu_ly():\n    pass\n\nHANDLERS = {"x": xu_ly}\nprint(HANDLERS)\n',
          "xu_ly", "dang ky trong dict")

# --- 16. Bien class doc qua Class.TEN ---
khong_bao('class C:\n    TIMEOUT = 5\n\nprint(C.TIMEOUT)\n',
          "TIMEOUT", "doc qua Class.TEN")

# --- 17. Walrus duoc dung ---
khong_bao('if (m := 5):\n    print(m)\n', "m", "walrus duoc dung")


# ===========================================================================
# PHAN 2: DEAD CODE THAT -- phai bi bat (14 case)
# ===========================================================================

bi_bao('def _helper():\n    pass\n', "_helper", "function",
       "ham private khong ai goi")
bi_bao('class KhongDung:\n    pass\n', "KhongDung", "class",
       "class khong ai dung")
bi_bao('TEMP = 123\n', "TEMP", "variable",
       "bien module khong doc")
bi_bao('def f():\n    x = 1\n    return 2\n', "x", "variable",
       "bien cuc bo khong dung")
bi_bao('class A:\n    def _rieng(self):\n        pass\n', "_rieng", "method",
       "method private khong goi")
bi_bao('async def _tai():\n    pass\n', "_tai", "function",
       "async function khong goi")
bi_bao('g = lambda: 1\n', "g", "function",
       "lambda gan ten nhung khong goi")
bi_bao('dem = 0\n', "dem", "variable",
       "bien thuong khong dung")
bi_bao('TIMEOUT_MAC_DINH = 30\n', "TIMEOUT_MAC_DINH", "variable",
       "hang so khong dung")
bi_bao('def xuat_bao_cao():\n    pass\n', "xuat_bao_cao", "function",
       "ham public trong script khong docstring, khong ai goi")
bi_bao('class A:\n    KHONG_DUNG = 1\n', "KHONG_DUNG", "variable",
       "bien class khong doc")
bi_bao('def _tinh(a, b):\n    return a\n', "_tinh", "function",
       "ham private co tham so, khong goi")
bi_bao('class A:\n    def _f(self):\n        y = 2\n        return 1\n',
       "y", "variable", "bien cuc bo trong method")
bi_bao('x = 1\nx = 2\n', "x", "variable",
       "gan 2 lan nhung khong doc")

# kiem tra lineno chinh xac: lay lan gan DAU tien
_fs = _ANALYZER.analyze('x = 1\nx = 2\n')
_fx = _tim(_fs, "x")
_kiem_tra(_fx is not None and _fx.lineno == 1,
          "lineno tro dung dong gan dau tien")


# ===========================================================================
# PHAN 3: Kiem tra cau truc & tich hop
# ===========================================================================

_kiem_tra(_ANALYZER.analyze("") == [], "source rong -> khong finding")
_kiem_tra(_ANALYZER.analyze("def f(:\n") == [], "syntax loi -> khong finding")
_kiem_tra(_ANALYZER.analyze("# chi comment\n") == [], "chi comment -> khong finding")

_fs = _ANALYZER.analyze("zzz_chet = 1\n")
_kiem_tra(len(_fs) == 1 and _fs[0].name == "zzz_chet",
          "chi bao dung 1 finding cho bien chet don gian")
_kiem_tra(_fs[0].kind == "variable", "kind dung cho bien chet")
_kiem_tra(all(isinstance(f, Finding) for f in _fs), "moi phan tu la Finding")
_kiem_tra(all(f.confidence == "high" for f in _fs),
          "chi tra ve confidence high")

_fs = _ANALYZER.analyze("bbb = 1\naaa = 2\n")
_kiem_tra([f.lineno for f in _fs] == sorted(f.lineno for f in _fs),
          "findings sap xep theo lineno")

# tich hop: module thuc te voi nhieu pattern
_nguon = '''"""Thu vien."""
__all__ = ["cong"]

def cong(a, b):
    return a + b

def _rieng():
    pass

TRAC = 0
'''
# TRAC la bien public trong module library (co docstring) -> duoc mien
# theo quy tac "public API cua library", giong nhu ham cong.
_fs = _ANALYZER.analyze(_nguon)
_kiem_tra(_ten(_fs) == {"_rieng"},
          "tich hop: chi bao _rieng; mien cong (__all__+library), TRAC (public library)")

_f0 = Finding("x", "variable", 1, "high")
_kiem_tra(_f0.ly_do == "", "ly_do mac dinh rong")
_kiem_tra((_f0.name, _f0.kind, _f0.lineno, _f0.confidence)
          == ("x", "variable", 1, "high"), "Finding du truong co ban")

print("TAT CA %d ASSERTIONS PASS" % _SO_ASSERT)
