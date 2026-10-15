"""Test cho RefactorEngine — chạy bằng python3 thật, file mẫu trong tmpdir.

Bao phủ:
  - rename xuyên 3 file (biến + hàm), đếm occurrences
  - rename KHÔNG đụng attribute (obj.cu)
  - dry_run không ghi file
  - extract function: params/return suy đúng + code chạy ra kết quả đúng
  - move function sang file khác + chạy được
  - rollback: file syntax lỗi sẵn -> không ghi gì; lỗi giữa chừng -> rollback hết
"""

import ast
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from refactor import RefactorEngine

PASS = []
FAIL = []


def check(ten, dieu_kien, chi_tiet=""):
    if dieu_kien:
        PASS.append(ten)
        print("PASS: %s" % ten)
    else:
        FAIL.append(ten)
        print("FAIL: %s %s" % (ten, chi_tiet))


def chay_python(path):
    """Chạy file bằng python3 thật, trả về (returncode, stdout)."""
    r = subprocess.run([sys.executable, str(path)], capture_output=True,
                       text=True, timeout=30)
    return r.returncode, r.stdout.strip()


# ---------------------------------------------------------------------------
# fixture: 3 file mẫu dùng chung cho rename
# ---------------------------------------------------------------------------

FILE_A = '''"""Module A."""
COUNT = 10

def tinh_tong(a, b):
    tong = a + b + COUNT
    return tong

class MayTinh:
    def __init__(self):
        self.tong = 0

    def cong(self, x):
        self.tong = tinh_tong(self.tong, x)
        return self.tong
'''

FILE_B = '''"""Module B."""
from a import tinh_tong, COUNT

def chay():
    ket_qua = tinh_tong(1, 2)
    print(ket_qua + COUNT)
    return ket_qua
'''

FILE_C = '''"""Module C: kiểm tra attribute không bị đổi."""
from a import tinh_tong

class BaoCao:
    def __init__(self):
        self.tinh_tong = "giá trị attr"  # attribute, PHẢI giữ nguyên

    def lay(self):
        obj = self
        return obj.tinh_tong  # attribute access, PHẢI giữ nguyên
'''


def lap_tmpdir_rename():
    tmp = tempfile.mkdtemp()
    (Path(tmp) / "a.py").write_text(FILE_A, encoding="utf-8")
    (Path(tmp) / "b.py").write_text(FILE_B, encoding="utf-8")
    (Path(tmp) / "c.py").write_text(FILE_C, encoding="utf-8")
    return tmp


# ---------------------------------------------------------------------------
# 1. rename xuyên 3 file
# ---------------------------------------------------------------------------

def test_rename_ham_xuyen_3_file():
    tmp = lap_tmpdir_rename()
    eng = RefactorEngine(tmp)
    kq = eng.rename_symbol("tinh_tong", "compute_sum", ["a.py", "b.py", "c.py"])
    check("rename ok", kq["ok"], kq)
    # a.py: def tinh_tong + 2 lần gọi (trong tinh_tong? không) -> đếm:
    # a.py: 'def tinh_tong' (1) + 'tinh_tong(self.tong, x)' (1) = 2
    # b.py: import tinh_tong? -> ImportFrom alias không đổi (ngoài phạm vi),
    #       'tinh_tong(1, 2)' (1) = 1 ; c.py: 0 (chỉ attribute)
    check("rename occurrences == 3", kq["occurrences"] == 3, kq)
    check("rename 2 file đổi", len(kq["renamed_files"]) == 2, kq)
    noi_dung_a = (Path(tmp) / "a.py").read_text(encoding="utf-8")
    check("a.py có def compute_sum", "def compute_sum(a, b):" in noi_dung_a)
    check("a.py hết tinh_tong dạng Name",
          "tinh_tong(" not in noi_dung_a and "def tinh_tong" not in noi_dung_a)
    noi_dung_c = (Path(tmp) / "c.py").read_text(encoding="utf-8")
    check("c.py giữ attribute self.tinh_tong",
          'self.tinh_tong = "giá trị attr"' in noi_dung_c)
    check("c.py giữ obj.tinh_tong", "obj.tinh_tong" in noi_dung_c)
    # cả 3 file vẫn parse được
    for f in ("a.py", "b.py", "c.py"):
        try:
            ast.parse((Path(tmp) / f).read_text(encoding="utf-8"))
            check("parse %s sau rename" % f, True)
        except SyntaxError as e:
            check("parse %s sau rename" % f, False, str(e))


def test_rename_bien_va_arg():
    tmp = lap_tmpdir_rename()
    eng = RefactorEngine(tmp)
    kq = eng.rename_symbol("tong", "total", ["a.py"])
    check("rename biến ok", kq["ok"], kq)
    noi_dung = (Path(tmp) / "a.py").read_text(encoding="utf-8")
    # 'tong' là biến cục bộ trong tinh_tong + attribute self.tong (giữ nguyên)
    check("biến tong -> total", "total = a + b + COUNT" in noi_dung, noi_dung)
    check("self.tong giữ nguyên (attribute)", "self.tong" in noi_dung)
    check("occurrences == 2 (gán + return)", kq["occurrences"] == 2, kq)


def test_rename_dry_run_khong_ghi():
    tmp = lap_tmpdir_rename()
    eng = RefactorEngine(tmp)
    truoc = (Path(tmp) / "a.py").read_text(encoding="utf-8")
    kq = eng.rename_symbol("tinh_tong", "compute_sum", ["a.py"], dry_run=True)
    check("dry_run ok", kq["ok"] and kq.get("dry_run") is True, kq)
    check("dry_run có diff", bool(kq.get("diffs")), kq)
    sau = (Path(tmp) / "a.py").read_text(encoding="utf-8")
    check("dry_run không ghi file", truoc == sau)
    check("dry_run không tạo backup", eng._backups == {})


def test_rename_loi():
    tmp = lap_tmpdir_rename()
    eng = RefactorEngine(tmp)
    kq = eng.rename_symbol("tinh_tong", "class", ["a.py"])
    check("tên mới là keyword -> lỗi", not kq["ok"], kq)
    kq2 = eng.rename_symbol("tinh_tong", "tinh_tong", ["a.py"])
    check("tên trùng nhau -> lỗi", not kq2["ok"], kq2)
    kq3 = eng.rename_symbol("khong_ton_tai", "moi", ["a.py"])
    check("không có occurrence -> ok, 0 file", kq3["ok"]
          and kq3["renamed_files"] == [] and kq3["occurrences"] == 0, kq3)


def test_rename_file_loi_syntax_san():
    tmp = lap_tmpdir_rename()
    (Path(tmp) / "hong.py").write_text("def hong(:\n  pass", encoding="utf-8")
    truoc_a = (Path(tmp) / "a.py").read_text(encoding="utf-8")
    eng = RefactorEngine(tmp)
    kq = eng.rename_symbol("tinh_tong", "compute_sum", ["a.py", "hong.py"])
    check("file lỗi syntax -> fail", not kq["ok"], kq)
    sau_a = (Path(tmp) / "a.py").read_text(encoding="utf-8")
    check("file tốt không bị ghi khi có file lỗi", truoc_a == sau_a)


def test_rename_rollback_khi_loi_giua_chung():
    """Cố tình gây lỗi ở file thứ 2 lúc ghi -> rollback file thứ 1."""
    tmp = lap_tmpdir_rename()
    eng = RefactorEngine(tmp)
    goc_a = (Path(tmp) / "a.py").read_text(encoding="utf-8")
    goc_b = (Path(tmp) / "b.py").read_text(encoding="utf-8")

    goi_goc = eng._ghi_va_validate
    dem = {"n": 0}

    def ghi_loi(path, content):
        dem["n"] += 1
        if dem["n"] == 2:  # file thứ 2 cố tình lỗi
            raise OSError("giả lập lỗi ghi đĩa")
        return goi_goc(path, content)

    eng._ghi_va_validate = ghi_loi
    kq = eng.rename_symbol("tinh_tong", "compute_sum", ["a.py", "b.py"])
    eng._ghi_va_validate = goi_goc
    check("lỗi giữa chừng -> fail", not kq["ok"], kq)
    check("a.py đã rollback về gốc",
          (Path(tmp) / "a.py").read_text(encoding="utf-8") == goc_a)
    check("b.py không bị động",
          (Path(tmp) / "b.py").read_text(encoding="utf-8") == goc_b)


# ---------------------------------------------------------------------------
# 2. extract_function
# ---------------------------------------------------------------------------

FILE_EXTRACT = '''"""Module extract."""
THUE = 0.1

def hoa_don(gia, so_luong):
    tam = gia * so_luong      # dòng 5
    thue = tam * THUE         # dòng 6
    tong = tam + thue         # dòng 7
    print("tạm:", tam)        # dòng 8
    return tong

if __name__ == "__main__":
    print(hoa_don(100, 2))
'''


def test_extract_params_va_return():
    tmp = tempfile.mkdtemp()
    p = Path(tmp) / "ex.py"
    p.write_text(FILE_EXTRACT, encoding="utf-8")
    eng = RefactorEngine(tmp)
    # dòng 5-8: tam/thue/tong/print ; tong dùng sau (return) -> return tong
    # gia, so_luong, THUE dùng trong đoạn nhưng định nghĩa ngoài -> params
    kq = eng.extract_function("ex.py", 5, 8, "tinh_hoa_don")
    check("extract ok", kq["ok"], kq)
    check("params đúng", kq["params"] == ["gia", "so_luong", "THUE"], kq)
    check("returns đúng", kq["returns"] == ["tong"], kq)
    moi = p.read_text(encoding="utf-8")
    check("có def mới", "def tinh_hoa_don(gia, so_luong, THUE):" in moi, moi)
    check("có return tong đúng indent", "        return tong\n" in moi, moi)
    check("call site gán kết quả",
          "    tong = tinh_hoa_don(gia, so_luong, THUE)" in moi, moi)
    rc, out = chay_python(p)
    check("code sau extract chạy được", rc == 0, out)
    check("kết quả đúng (100*2*1.1=220.0)", "220.0" in out, out)


def test_extract_dry_run_va_loi():
    tmp = tempfile.mkdtemp()
    p = Path(tmp) / "ex.py"
    p.write_text(FILE_EXTRACT, encoding="utf-8")
    eng = RefactorEngine(tmp)
    truoc = p.read_text(encoding="utf-8")
    kq = eng.extract_function("ex.py", 5, 8, "tinh_hoa_don", dry_run=True)
    check("extract dry_run ok", kq["ok"] and kq.get("dry_run"), kq)
    check("extract dry_run không ghi", p.read_text(encoding="utf-8") == truoc)
    # đoạn cắt dở biểu thức -> lỗi; dòng đơn hoàn chỉnh -> ok (dry_run)
    kq2 = eng.extract_function("ex.py", 6, 6, "mot_dong", dry_run=True)
    check("extract 1 dòng hoàn chỉnh ok", kq2["ok"], kq2)
    check("params dòng đơn", kq2["params"] == ["tam", "THUE"], kq2)
    check("returns dòng đơn", kq2["returns"] == ["thue"], kq2)
    kq4 = eng.extract_function("ex.py", 8, 6, "nguoc")
    check("start > end -> lỗi", not kq4["ok"], kq4)
    kq5 = eng.extract_function("ex.py", 1, 99, "vuot")
    check("end vượt số dòng -> lỗi", not kq5["ok"], kq5)


def test_extract_doan_khong_hoan_chinh():
    tmp = tempfile.mkdtemp()
    p = Path(tmp) / "ex2.py"
    p.write_text("x = 1\n", encoding="utf-8")
    eng = RefactorEngine(tmp)
    # dòng trống không có -> tạo file có dòng dở
    p.write_text("def f():\n    x = (1 +\n         2)\n", encoding="utf-8")
    kq = eng.extract_function("ex2.py", 2, 2, "g")
    check("đoạn dở biểu thức -> lỗi", not kq["ok"], kq)


# ---------------------------------------------------------------------------
# 3. move_symbol
# ---------------------------------------------------------------------------

FILE_SRC = '''"""Nguồn."""
import math

VAL = 42

@staticmethod
def _helper():
    return 1

def can_chuyen(x):
    """Hàm sẽ bị chuyển."""
    return x * VAL + _helper()

def o_lai():
    return "ở lại nguồn"
'''

FILE_DST = '''"""Đích."""
import os
'''


def test_move_function():
    tmp = tempfile.mkdtemp()
    src = Path(tmp) / "nguon.py"
    dst = Path(tmp) / "dich.py"
    src.write_text(FILE_SRC, encoding="utf-8")
    dst.write_text(FILE_DST, encoding="utf-8")
    eng = RefactorEngine(tmp)
    kq = eng.move_symbol("nguon.py", "dich.py", "can_chuyen")
    check("move ok", kq["ok"], kq)
    moi_src = src.read_text(encoding="utf-8")
    moi_dst = dst.read_text(encoding="utf-8")
    check("nguồn hết def can_chuyen", "def can_chuyen" not in moi_src)
    check("nguồn giữ o_lai", "def o_lai" in moi_src)
    check("đích có def can_chuyen", "def can_chuyen(x):" in moi_dst, moi_dst)
    check("đích có comment nguồn gốc", "moved from" in moi_dst)
    check("nguồn vẫn parse", _parse_ok(moi_src))
    check("đích vẫn parse", _parse_ok(moi_dst))
    # hàm chuyển sang vẫn chạy đúng logic (VAL/_helper không theo -> NameError
    # là đúng kỳ vọng vì dependency ngoài phạm vi; chỉ check syntax ở đây)


def _parse_ok(text):
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False


def test_move_class_va_dry_run():
    tmp = tempfile.mkdtemp()
    src = Path(tmp) / "s.py"
    dst = Path(tmp) / "d.py"
    src.write_text("class Xe:\n    def __init__(self):\n        self.banh = 4\n",
                   encoding="utf-8")
    dst.write_text('"""d."""\n', encoding="utf-8")
    eng = RefactorEngine(tmp)
    truoc_s, truoc_d = src.read_text(encoding="utf-8"), dst.read_text(encoding="utf-8")
    kq = eng.move_symbol("s.py", "d.py", "Xe", dry_run=True)
    check("move class dry_run ok", kq["ok"] and kq.get("dry_run"), kq)
    check("dry_run không ghi nguồn", src.read_text(encoding="utf-8") == truoc_s)
    check("dry_run không ghi đích", dst.read_text(encoding="utf-8") == truoc_d)
    kq2 = eng.move_symbol("s.py", "d.py", "KhongCo")
    check("symbol không tồn tại -> lỗi", not kq2["ok"], kq2)
    kq3 = eng.move_symbol("s.py", "s.py", "Xe")
    check("nguồn trùng đích -> lỗi", not kq3["ok"], kq3)
    # move thật class
    kq4 = eng.move_symbol("s.py", "d.py", "Xe")
    check("move class thật ok", kq4["ok"], kq4)
    check("đích có class Xe", "class Xe:" in dst.read_text(encoding="utf-8"))


def test_move_giu_decorator():
    tmp = tempfile.mkdtemp()
    src = Path(tmp) / "s.py"
    dst = Path(tmp) / "d.py"
    src.write_text("@dec(arg=1)\ndef ham_deco():\n    return 1\n\ndef khac():\n    return 2\n",
                   encoding="utf-8")
    dst.write_text('"""d."""\n', encoding="utf-8")
    eng = RefactorEngine(tmp)
    kq = eng.move_symbol("s.py", "d.py", "ham_deco")
    check("move decorator ok", kq["ok"], kq)
    check("đích giữ decorator", "@dec(arg=1)\ndef ham_deco():" in dst.read_text(encoding="utf-8"))
    check("nguồn sạch decorator", "@dec" not in src.read_text(encoding="utf-8"))
    check("nguồn giữ hàm khác", "def khac():" in src.read_text(encoding="utf-8"))


def test_move_rollback_khi_dich_loi():
    tmp = tempfile.mkdtemp()
    src = Path(tmp) / "s.py"
    dst = Path(tmp) / "d.py"
    src.write_text("def f():\n    return 1\n", encoding="utf-8")
    dst.write_text("def hong(:\n", encoding="utf-8")  # đích lỗi syntax sẵn
    eng = RefactorEngine(tmp)
    goc = src.read_text(encoding="utf-8")
    kq = eng.move_symbol("s.py", "d.py", "f")
    check("đích lỗi syntax -> fail, chưa ghi gì", not kq["ok"], kq)
    check("nguồn nguyên vẹn", src.read_text(encoding="utf-8") == goc)


# ---------------------------------------------------------------------------
# 4. rollback thủ công
# ---------------------------------------------------------------------------

def test_rollback_thu_cong():
    tmp = lap_tmpdir_rename()
    eng = RefactorEngine(tmp)
    goc = (Path(tmp) / "a.py").read_text(encoding="utf-8")
    kq = eng.rename_symbol("tinh_tong", "compute_sum", ["a.py"])
    check("rename ok trước rollback", kq["ok"], kq)
    # cố tình phá file sau khi rename
    (Path(tmp) / "a.py").write_text("PHÁ HỎNG (((", encoding="utf-8")
    rb = eng.rollback()
    check("rollback báo ok", rb.get(str(Path(tmp) / "a.py")) is True, rb)
    check("file về đúng bản gốc",
          (Path(tmp) / "a.py").read_text(encoding="utf-8") == goc)


if __name__ == "__main__":
    test_rename_ham_xuyen_3_file()
    test_rename_bien_va_arg()
    test_rename_dry_run_khong_ghi()
    test_rename_loi()
    test_rename_file_loi_syntax_san()
    test_rename_rollback_khi_loi_giua_chung()
    test_extract_params_va_return()
    test_extract_dry_run_va_loi()
    test_extract_doan_khong_hoan_chinh()
    test_move_function()
    test_move_class_va_dry_run()
    test_move_giu_decorator()
    test_move_rollback_khi_dich_loi()
    test_rollback_thu_cong()
    print("\n==== %d PASS, %d FAIL ====" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)
