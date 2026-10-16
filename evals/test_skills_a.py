"""Test cho 4 skills LUONG 2A: code-review, test-writer, security-audit, perf-optimizer.

Chay: python3 test_skills_a.py
Khong can pytest — dung assert thuan + bo dem.
Moi skill >= 5 assertions voi code mau.
"""

import ast
import sys
import textwrap
from pathlib import Path

# -- nap 4 skill vao sys.path --
# perf-optimizer is archived; others are active.
ROOT = Path(__file__).resolve().parent.parent
for skill_dir in ("code-review", "test-writer", "security-audit"):
    sys.path.insert(0, str(ROOT / "skills" / skill_dir))
sys.path.insert(0, str(ROOT / "skills-archived" / "perf-optimizer"))

import review      # noqa: E402
import writer      # noqa: E402
import audit       # noqa: E402
import optimizer   # noqa: E402

PASS = 0
FAIL = 0
LOI: list = []


def check(dieu_kien, mo_ta):
    """1 assertion: dem PASS/FAIL, khong dung lai khi fail."""
    global PASS, FAIL
    if dieu_kien:
        PASS += 1
    else:
        FAIL += 1
        LOI.append(mo_ta)
        print("  FAIL: %s" % mo_ta)


# ---------------------------------------------------------------------------
# Skill 1: code-review (7 assertions)
# ---------------------------------------------------------------------------

def test_code_review():
    print("== skill code-review ==")
    code_mau = textwrap.dedent('''\
        import os

        def tinh_tong(data):
            # TODO: xu ly data None
            total = 0
            unused_var = 123
            for x in data:
                if x > 0:
                    total += x
            print("tong:", total)
            return total

        def nguy_hiem():
            try:
                x = 1 / 0
            except:
                pass
        ''')
    issues = review.review_source(code_mau, filename="mau.py")

    check(any(i.rule == "bare_except" and i.severity == "error"
              for i in issues),
          "phai phat hien bare_except severity=error")
    check(any(i.rule == "print_stmt" for i in issues),
          "phai phat hien print_stmt")
    check(any(i.rule == "todo" for i in issues),
          "phai phat hien TODO comment")
    check(any(i.rule == "dead_code" and "unused_var" in i.message
              for i in issues),
          "phai phat hien dead_code bien unused_var")
    # schema day du cho moi issue
    check(all(i.file == "mau.py" and i.line >= 1 and i.message
              and i.suggestion for i in issues),
          "moi issue phai co file/line/message/suggestion")
    check(all(i.severity in ("error", "warning", "info") for i in issues),
          "severity phai nam trong {error,warning,info}")
    # review_diff chi check dong them moi
    diff = ("diff --git a/mau.py b/mau.py\n"
            "--- a/mau.py\n+++ b/mau.py\n"
            "@@ -1,2 +1,3 @@\n x = 1\n+try:\n+    y = 2\n+except:\n+    pass\n")
    issues_diff = review.review_diff(diff)
    check(any(i.rule == "bare_except" for i in issues_diff),
          "review_diff phai bat bare_except o dong them moi")


# ---------------------------------------------------------------------------
# Skill 2: test-writer (8 assertions)
# ---------------------------------------------------------------------------

def test_test_writer():
    print("== skill test-writer ==")
    code_mau = textwrap.dedent('''\
        def add(a, b):
            """Cong hai so."""
            return a + b

        def is_adult(age: int) -> bool:
            return age >= 18

        def greet(name: str) -> str:
            return "Hello " + name
        ''')
    ket_qua = writer.generate_tests(code_mau, module_name="mau_mod")

    check("import pytest" in ket_qua.test_code,
          "test phai import pytest")
    check("def test_add_happy_path" in ket_qua.test_code,
          "phai sinh test happy path cho add")
    check("def test_is_adult_happy_path" in ket_qua.test_code,
          "phai sinh test happy path cho is_adult")
    check("assert add(1, 2) == 3" in ket_qua.test_code,
          "ham don gian phai tu tinh ket qua: assert add(1, 2) == 3")
    check("None" in ket_qua.test_code and "none_input" in ket_qua.test_code,
          "phai sinh edge case None")
    check("boundary_zero" in ket_qua.test_code
          or "boundary_negative" in ket_qua.test_code,
          "phai sinh edge case bien (0/-1) cho tham so so")
    check('""' in ket_qua.test_code or "empty_string" in ket_qua.test_code,
          "phai sinh edge case chuoi rong cho tham so str")
    # test sinh ra phai la Python hop le
    try:
        ast.parse(ket_qua.test_code)
        hop_le = True
    except SyntaxError:
        hop_le = False
    check(hop_le, "code test sinh ra phai parse duoc bang ast")


# ---------------------------------------------------------------------------
# Skill 3: security-audit (8 assertions)
# ---------------------------------------------------------------------------

def test_security_audit():
    print("== skill security-audit ==")
    code_mau = textwrap.dedent('''\
        import os
        import subprocess

        api_key = "sk-test-1234567890abcdefghij"

        def chay_lenh(user_input):
            os.system("ls " + user_input)

        def tinh(expr):
            return eval(expr)

        def lay_user(user_id):
            cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")
        ''')
    findings = audit.audit_source(code_mau, filename="mau.py")

    check(any(f.category == "hardcoded_secret" and f.severity == "high"
              for f in findings),
          "phai phat hien hardcoded secret severity=high")
    check(any(f.category == "dangerous_function" and "eval" in f.issue
              for f in findings),
          "phai phat hien eval()")
    check(any(f.category == "dangerous_function" and "os.system" in f.issue
              for f in findings),
          "phai phat hien os.system()")
    check(any(f.category == "sql_injection" for f in findings),
          "phai phat hien SQL string concat (f-string)")
    check(any(f.category == "missing_validation" and "user_input" in f.issue
              for f in findings),
          "phai phat hien thieu validate input (user_input -> os.system)")
    check(all(f.file == "mau.py" and f.line >= 1 and f.issue and f.fix
              for f in findings),
          "moi finding phai co file/line/issue/fix")
    check(all(f.severity in ("high", "medium", "low") for f in findings),
          "severity phai nam trong {high,medium,low}")
    # whitelist: khong bao loi khi doc tu env
    sach = audit.audit_source('key = os.environ.get("API_KEY")\n',
                              filename="sach.py")
    check(not any(f.category == "hardcoded_secret" for f in sach),
          "khong bao secret khi doc tu os.environ.get")


# ---------------------------------------------------------------------------
# Skill 4: perf-optimizer (7 assertions)
# ---------------------------------------------------------------------------

def test_perf_optimizer():
    print("== skill perf-optimizer ==")
    code_mau = textwrap.dedent('''\
        def xu_ly(users, orders, ids):
            ket_qua = ""
            for user in users:
                for order in orders:
                    if order.user_id == user.id:
                        ket_qua += order.name + ";"
                f = open("/tmp/log.txt", "a")
                f.write(user.name)
                f.close()
            for i in ids:
                cursor.execute("SELECT * FROM t WHERE id = ?", (i,))
            return ket_qua
        ''')
    suggestions = optimizer.optimize_source(code_mau, filename="mau.py")
    patterns = {s.pattern for s in suggestions}

    check("nested_loop" in patterns,
          "phai phat hien nested_loop O(n^2)")
    check("io_in_loop" in patterns,
          "phai phat hien open() trong vong lap")
    check("string_concat" in patterns,
          "phai phat hien noi chuoi += trong vong lap")
    check("n_plus_one" in patterns,
          "phai phat hien N+1 query (execute trong loop)")
    check(all(s.file == "mau.py" and s.line >= 1 and s.problem
              and s.optimized_code for s in suggestions),
          "moi suggestion phai co file/line/problem/optimized_code")
    check(all(s.severity in ("high", "medium", "low") for s in suggestions),
          "severity phai nam trong {high,medium,low}")
    # goi y nested loop phai de xuat set/dict
    nl = [s for s in suggestions if s.pattern == "nested_loop"]
    check(any("set(" in s.optimized_code for s in nl),
          "goi y nested_loop phai de xuat dung set/dict")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    test_code_review()
    test_test_writer()
    test_security_audit()
    test_perf_optimizer()
    print()
    print("Ket qua: %d PASS, %d FAIL" % (PASS, FAIL))
    if LOI:
        print("Cac loi:")
        for loi in LOI:
            print("  - %s" % loi)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
