"""Test CodeReviewer: chạy python3 thật, PASS toàn bộ."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
_REPO = Path(__file__).resolve().parents[1]
_sys_src = str(_REPO / "agent" / "src")
if _sys_src not in sys.path:
    sys.path.insert(0, _sys_src)

from autonomous.codereview import CodeReviewer

# File mẫu cố tình chứa đủ 7 loại issue
MAU = '''"""Module mẫu để test review."""
import os

# TODO: viết docstring đầy đủ hơn


def ham_phuc_tap(x):
    """Hàm cố tình phức tạp: nhiều rẽ nhánh để complexity > 10."""
    if x > 0:
        if x > 1:
            for i in range(x):
                while i < 5:
                    if i % 2 == 0 and i % 3 == 0 or i == 7:
                        try:
                            y = i / (i - 1)
                        except ValueError:
                            y = 0
                        except TypeError:
                            y = 1
                    elif i == 3:
                        y = 3
                    else:
                        y = -1
                    i += 1
        else:
            y = -2
    else:
        y = -3
    return y if y else 0


def ham_dai_qua():
    """Hàm cố tình dài > 50 dòng."""
    a = 1
    b = 2
    c = 3
    d = 4
    e = 5
    f = 6
    g = 7
    h = 8
    j = 9
    k = 10
    l = 11
    m = 12
    n = 13
    o = 14
    p = 15
    q = 16
    r = 17
    s = 18
    t = 19
    u = 20
    v = 21
    w = 22
    x = 23
    y = 24
    z = 25
    aa = 26
    ab = 27
    ac = 28
    ad = 29
    ae = 30
    af = 31
    ag = 32
    ah = 33
    aj = 34
    ak = 35
    al = 36
    am = 37
    an = 38
    ao = 39
    ap = 40
    aq = 41
    ar = 42
    as_ = 43
    at = 44
    au = 45
    av = 46
    aw = 47
    ax = 48
    ay = 49
    az = 50
    total = a + az
    return total


def ham_dead_code():
    unused_var = 123
    dung_roi = 456
    return dung_roi * 2


def ham_bare_except():
    try:
        risky()
    except:
        pass


def ham_print():
    print("debug tạm")


def risky():
    return 1
# FIXME: hàm risky chỉ là stub
'''

rv = CodeReviewer()
passed = 0
failed = 0


def check(cond, ten):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", ten)


def bang_rule(issues, rule):
    return [i for i in issues if i["rule"] == rule]


with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp) / "mau.py"
    p.write_text(MAU, encoding="utf-8")
    issues = rv.review_file(str(p))

    c = bang_rule(issues, "complexity")
    check(len(c) >= 1, "complexity: bắt được hàm phức tạp")
    check(c[0]["severity"] == "warning", "complexity severity=warning")
    check("ham_phuc_tap" in c[0]["message"], "complexity nêu tên hàm")
    check(c[0]["line"] > 1, "complexity có số dòng hợp lệ")

    lf = bang_rule(issues, "long_function")
    check(len(lf) >= 1, "long_function: bắt được hàm dài")
    check(lf[0]["severity"] == "warning", "long_function severity=warning")
    check("ham_dai_qua" in lf[0]["message"], "long_function nêu tên hàm")

    dc = bang_rule(issues, "dead_code")
    check(any(i["message"].find("unused_var") != -1 for i in dc),
          "dead_code: bắt được unused_var")
    check(all(i["severity"] == "warning" for i in dc),
          "dead_code severity=warning")
    check(not any(i["message"].find("dung_roi") != -1 for i in dc),
          "dead_code: không báo biến đã dùng")

    be = bang_rule(issues, "bare_except")
    check(len(be) == 1, "bare_except: bắt đúng 1 chỗ")
    check(be[0]["severity"] == "error", "bare_except severity=error")

    td = bang_rule(issues, "todo")
    check(len(td) >= 2, "todo: bắt cả TODO và FIXME")
    check(all(i["severity"] == "info" for i in td), "todo severity=info")
    check(all(i["line"] > 0 for i in td), "todo có số dòng")

    ps = bang_rule(issues, "print_stmt")
    check(len(ps) == 1, "print_stmt: bắt đúng 1 chỗ")
    check(ps[0]["severity"] == "info", "print_stmt severity=info")

    # long_file: file mẫu ngắn nên không có; test riêng với file dài
    check(bang_rule(issues, "long_file") == [], "long_file: file ngắn không báo")
    p2 = Path(tmp) / "dai.py"
    p2.write_text("\n".join("x = %d" % i for i in range(600)), encoding="utf-8")
    iss2 = rv.review_file(str(p2))
    lfi = bang_rule(iss2, "long_file")
    check(len(lfi) == 1, "long_file: file 600 dòng báo 1 info")
    check(lfi[0]["severity"] == "info", "long_file severity=info")

    # syntax lỗi
    p3 = Path(tmp) / "loi.py"
    p3.write_text("def broken(:\n", encoding="utf-8")
    iss3 = rv.review_file(str(p3))
    check(len(iss3) == 1 and iss3[0]["rule"] == "syntax"
          and iss3[0]["severity"] == "error", "syntax lỗi trả về 1 issue error")

    # review_diff: chỉ check dòng thêm mới
    diff = """diff --git a/m.py b/m.py
index 111..222 100644
--- a/m.py
+++ b/m.py
@@ -1,3 +1,6 @@
 def ok():
     pass
+# TODO: nhớ xóa
+    print("mới")
+    try:
+        x = 1
+    except:
+        pass
"""
    diss = rv.review_diff(diff)
    check(len(bang_rule(diss, "todo")) == 1, "diff: bắt TODO dòng mới")
    check(len(bang_rule(diss, "print_stmt")) == 1, "diff: bắt print dòng mới")
    check(len(bang_rule(diss, "bare_except")) == 1, "diff: bắt bare except dòng mới")
    check(bang_rule(diss, "bare_except")[0]["severity"] == "error",
          "diff: bare_except severity=error")
    # diff không có dòng mới có issue -> rỗng
    diff_sach = """diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,2 +1,3 @@
 def ok():
     pass
+    return 1
"""
    check(rv.review_diff(diff_sach) == [], "diff sạch trả về rỗng")

    # to_markdown tiếng Việt
    md = rv.to_markdown(issues)
    check("Báo cáo code review" in md, "markdown có tiêu đề")
    check("Tổng:" in md and "🔴" in md, "markdown có dòng tổng")
    check("## 🔴 Lỗi" in md, "markdown có nhóm lỗi")
    check("## 🟡 Cảnh báo" in md, "markdown có nhóm cảnh báo")
    check("## 🔵 Thông tin" in md, "markdown có nhóm thông tin")
    check("bare_except" in md and "dead_code" in md, "markdown liệt kê rule")
    check(rv.to_markdown([]).find("Không phát hiện") != -1,
          "markdown rỗng báo không có vấn đề")

    # issue có đủ field chuẩn
    check(all(set(i) == {"line", "severity", "rule", "message"} for i in issues),
          "mọi issue đủ 4 field")

print("PASS: %d, FAIL: %d" % (passed, failed))
sys.exit(1 if failed else 0)
