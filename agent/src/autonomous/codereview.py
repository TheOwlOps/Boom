"""CodeReviewer cho Boom: review tĩnh file Python bằng AST thật, không regex mù.

Review rules:
  - complexity   : cyclomatic complexity > 10 -> warning (AST: if/for/while/
                   except/and/or, mỗi điểm rẽ +1)
  - long_function: hàm > 50 dòng -> warning
  - long_file    : file > 500 dòng -> info
  - dead_code    : biến gán nhưng không dùng -> warning (phân tích AST theo scope)
  - bare_except  : `except:` trần -> error
  - todo         : comment TODO/FIXME -> info
  - print_stmt   : dùng print() thay vì logging -> info

API:
  - review_file(path)      -> list issues [{line, severity, rule, message}]
  - review_diff(diff_text) -> chỉ check các dòng thêm mới trong git diff
                            (rules theo dòng: todo, print_stmt, bare_except)
  - to_markdown(issues)    -> báo cáo tiếng Việt

Chỉ dùng stdlib: ast, io, re, tokenize, pathlib.
"""

import ast
import io
import re
import tokenize
from pathlib import Path


# Ngưỡng mặc định cho từng rule
NGUONG_COMPLEXITY = 10
NGUONG_LONG_FUNCTION = 50
NGUONG_LONG_FILE = 500

# Regex tìm TODO/FIXME trong comment
_RE_TODO = re.compile(r"\b(TODO|FIXME)\b", re.IGNORECASE)
# Regex cho print() và except trần trong review_diff (check theo dòng)
_RE_PRINT = re.compile(r"(?<![\w.])print\s*\(")
_RE_BARE_EXCEPT = re.compile(r"^\s*except\s*:\s*(#.*)?$")


def _issue(line, severity, rule, message):
    """Tạo 1 issue chuẩn."""
    return {"line": line, "severity": severity, "rule": rule,
            "message": message}


# ---------------------------------------------------------------------------
# rule: complexity
# ---------------------------------------------------------------------------

class _DemDoPhucTap(ast.NodeVisitor):
    """Đếm điểm rẽ cho cyclomatic complexity của 1 hàm.

    Mỗi điểm rẽ +1 vào độ phức tạp (gốc 1):
      if / for / while / except / with? (không) / and-or (BoolOp: n giá trị -> +n-1)
      IfExp (ternary), comprehension (For trong comprehension),
      Assert (rẽ khi fail), MatchCase (mỗi case -> +1).
    """

    def __init__(self):
        self.diem = 0

    def _cong(self, node):
        self.diem += 1
        self.generic_visit(node)

    # rẽ nhánh cơ bản
    def visit_If(self, node):
        self._cong(node)

    def visit_For(self, node):
        self._cong(node)

    def visit_AsyncFor(self, node):
        self._cong(node)

    def visit_While(self, node):
        self._cong(node)

    def visit_ExceptHandler(self, node):
        # chỉ tính except có type; except: trần đã có rule riêng
        if node.type is not None:
            self.diem += 1
        self.generic_visit(node)

    def visit_IfExp(self, node):
        self._cong(node)

    def visit_BoolOp(self, node):
        # a and b and c -> 3 giá trị -> +2 điểm rẽ
        self.diem += len(node.values) - 1
        self.generic_visit(node)

    def visit_comprehension(self, node):
        self.diem += 1  # mỗi vòng for trong comprehension
        self.generic_visit(node)

    def visit_Assert(self, node):
        self._cong(node)

    def visit_MatchCase(self, node):
        self._cong(node)

    # không đi vào hàm/class lồng nhau: độ phức tạp của scope riêng
    def visit_FunctionDef(self, node):
        pass

    def visit_AsyncFunctionDef(self, node):
        pass

    def visit_ClassDef(self, node):
        pass


def _do_phuc_tap(node):
    dem = _DemDoPhucTap()
    for stmt in node.body:
        dem.visit(stmt)
    return 1 + dem.diem


# ---------------------------------------------------------------------------
# rule: dead_code (biến gán nhưng không dùng)
# ---------------------------------------------------------------------------

class _ThuThapGanDung(ast.NodeVisitor):
    """Thu thập biến gán (Store) và biến dùng (Load) trong 1 scope.

    - Bỏ qua tên hàm/class/import (không phải biến).
    - Bỏ qua biến lặp for và biến `_`.
    - Gán nhưng sau đó có Load cùng tên trong scope -> coi là đã dùng.
    """

    def __init__(self):
        self.gan = {}   # name -> (lineno, col) của lần gán đầu tiên
        self.dung = set()
        self._trong_ham_long = False

    def visit_Name(self, node):
        if node.id == "_" or node.id.startswith("__"):
            return
        if isinstance(node.ctx, ast.Load):
            self.dung.add(node.id)
        elif isinstance(node.ctx, (ast.Store, ast.Del)):
            # Del coi như dùng (không phải dead code)
            if isinstance(node.ctx, ast.Del):
                self.dung.add(node.id)
            elif node.id not in self.gan:
                self.gan[node.id] = (node.lineno, node.col_offset)
        self.generic_visit(node)

    def visit_arg(self, node):
        # tham số hàm: coi là đã dùng mặc định (tránh false positive
        # cho API public); chỉ đánh dấu đã gán để không báo
        if node.arg not in self.gan:
            self.gan[node.arg] = (node.lineno, node.col_offset)
        self.dung.add(node.arg)

    def _ham_long(self, node):
        # hàm lồng: chỉ ghi tên + decorator/args, không đi vào thân
        # (biến trong thân thuộc scope riêng)
        self.gan.setdefault(node.name, (node.lineno, node.col_offset))
        self.dung.add(node.name)  # tên hàm định nghĩa coi như dùng nội bộ
        for dec in node.decorator_list:
            self.visit(dec)
        for a in node.args.args:
            self.visit_arg(a)

    def visit_FunctionDef(self, node):
        self._ham_long(node)

    def visit_AsyncFunctionDef(self, node):
        self._ham_long(node)

    def visit_ClassDef(self, node):
        self.gan.setdefault(node.name, (node.lineno, node.col_offset))
        self.dung.add(node.name)
        for dec in node.decorator_list:
            self.visit(dec)
        for base in node.bases:
            self.visit(base)

    def visit_Import(self, node):
        for a in node.names:
            ten = (a.asname or a.name).split(".")[0]
            self.gan.setdefault(ten, (node.lineno, node.col_offset))
            self.dung.add(ten)  # import coi như dùng (không báo dead import ở đây)

    def visit_ImportFrom(self, node):
        for a in node.names:
            if a.name == "*":
                continue
            ten = a.asname or a.name
            self.gan.setdefault(ten, (node.lineno, node.col_offset))
            self.dung.add(ten)

    def visit_Global(self, node):
        for ten in node.names:
            self.dung.add(ten)

    def visit_Nonlocal(self, node):
        for ten in node.names:
            self.dung.add(ten)


def _dead_code_trong_scope(thu):
    """Trả về [(lineno, name)] các biến gán nhưng không dùng."""
    return sorted(
        (lineno, ten) for ten, (lineno, _col) in thu.gan.items()
        if ten not in thu.dung
    )


# ---------------------------------------------------------------------------
# CodeReviewer
# ---------------------------------------------------------------------------

class CodeReviewer:
    """Review tĩnh file Python."""

    def __init__(self, nguong_complexity=NGUONG_COMPLEXITY,
                 nguong_long_function=NGUONG_LONG_FUNCTION,
                 nguong_long_file=NGUONG_LONG_FILE):
        self.nguong_complexity = nguong_complexity
        self.nguong_long_function = nguong_long_function
        self.nguong_long_file = nguong_long_file

    # -- review_file -------------------------------------------------------

    def review_file(self, path):
        """Phân tích 1 file Python, trả về list issues.

        Syntax lỗi -> trả về 1 issue severity error rule 'syntax'.
        """
        text = Path(path).read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            return [_issue(exc.lineno or 1, "error", "syntax",
                           "lỗi cú pháp: %s" % exc.msg)]

        issues = []
        so_dong = len(text.splitlines())

        # long_file
        if so_dong > self.nguong_long_file:
            issues.append(_issue(
                1, "info", "long_file",
                "file dài %d dòng (ngưỡng %d), nên tách nhỏ"
                % (so_dong, self.nguong_long_file)))

        # todo: quét comment bằng tokenize (không bắt nhầm trong string)
        issues.extend(self._rule_todo(text))

        for node in ast.walk(tree):
            # complexity + long_function: chỉ áp cho def (không tính module)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                do_dai = (node.end_lineno or node.lineno) - node.lineno + 1
                if do_dai > self.nguong_long_function:
                    issues.append(_issue(
                        node.lineno, "warning", "long_function",
                        "hàm '%s' dài %d dòng (ngưỡng %d), nên tách nhỏ"
                        % (node.name, do_dai, self.nguong_long_function)))
                phuc_tap = _do_phuc_tap(node)
                if phuc_tap > self.nguong_complexity:
                    issues.append(_issue(
                        node.lineno, "warning", "complexity",
                        "hàm '%s' có cyclomatic complexity %d (ngưỡng %d), "
                        "nên tách nhánh"
                        % (node.name, phuc_tap, self.nguong_complexity)))

            # bare_except
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                issues.append(_issue(
                    node.lineno, "error", "bare_except",
                    "`except:` trần bắt mọi exception kể cả "
                    "KeyboardInterrupt/SystemExit, nên bắt cụ thể"))

            # print_stmt
            if isinstance(node, ast.Call) \
                    and isinstance(node.func, ast.Name) \
                    and node.func.id == "print":
                issues.append(_issue(
                    node.lineno, "info", "print_stmt",
                    "dùng print() thay vì logging, log sẽ khó "
                    "quản lý khi chạy thật"))

        # dead_code: chạy riêng từng scope (module + mỗi hàm)
        issues.extend(self._rule_dead_code(tree))

        issues.sort(key=lambda i: (i["line"], i["rule"]))
        return issues

    # -- từng rule chi tiết ------------------------------------------------

    def _rule_todo(self, text):
        """Quét comment TODO/FIXME bằng tokenize."""
        issues = []
        try:
            tokens = tokenize.generate_tokens(io.StringIO(text).readline)
            for tok in tokens:
                if tok.type == tokenize.COMMENT and _RE_TODO.search(tok.string):
                    khop = _RE_TODO.search(tok.string)
                    issues.append(_issue(
                        tok.start[0], "info", "todo",
                        "comment %s ở dòng %d: %s"
                        % (khop.group(1).upper(), tok.start[0],
                           tok.string.strip()[:80])))
        except (tokenize.TokenError, IndentationError):
            pass  # file lỗi token -> rule syntax đã bắt ở review_file
        return issues

    def _rule_dead_code(self, tree):
        """Dead code theo từng scope: module và mỗi hàm."""
        issues = []

        def xet_scope(scope_node, ten_scope):
            thu = _ThuThapGanDung()
            # với hàm: chỉ visit thân (args đã được visit_arg đánh dấu dùng)
            for stmt in scope_node.body:
                thu.visit(stmt)
            for lineno, ten in _dead_code_trong_scope(thu):
                issues.append(_issue(
                    lineno, "warning", "dead_code",
                    "biến '%s' được gán nhưng không dùng%s"
                    % (ten, " trong hàm '%s'" % ten_scope
                       if ten_scope else "")))

        # scope module
        xet_scope(tree, "")
        # scope từng hàm
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                xet_scope(node, node.name)
        return issues

    # -- review_diff -------------------------------------------------------

    def review_diff(self, diff_text):
        """Review từ git diff: chỉ check các dòng thêm mới (bắt đầu bằng '+').

        Rules theo dòng: todo, print_stmt, bare_except. Trả về issues với
        line là số dòng trong file MỚI.
        """
        issues = []
        file_moi = None
        dong_moi = 0
        for dong in diff_text.splitlines():
            if dong.startswith("+++"):
                # +++ b/<path>
                file_moi = dong[4:] if dong.startswith("+++ b/") else dong[4:]
                continue
            if dong.startswith("@@"):
                # @@ -a,b +c,d @@ -> lấy dòng bắt đầu của file mới (c)
                m = re.search(r"\+(\d+)", dong)
                dong_moi = int(m.group(1)) if m else 0
                continue
            if dong.startswith("+") and not dong.startswith("+++"):
                noi_dung = dong[1:]
                dong_moi += 1
                if _RE_TODO.search(noi_dung) and noi_dung.lstrip().startswith("#"):
                    khop = _RE_TODO.search(noi_dung)
                    issues.append(_issue(
                        dong_moi, "info", "todo",
                        "[diff %s] comment %s: %s"
                        % (file_moi, khop.group(1).upper(),
                           noi_dung.strip()[:80])))
                if _RE_PRINT.search(noi_dung):
                    issues.append(_issue(
                        dong_moi, "info", "print_stmt",
                        "[diff %s] dùng print() thay vì logging"
                        % file_moi))
                if _RE_BARE_EXCEPT.match(noi_dung):
                    issues.append(_issue(
                        dong_moi, "error", "bare_except",
                        "[diff %s] `except:` trần bắt mọi exception"
                        % file_moi))
            elif dong.startswith("-") and not dong.startswith("---"):
                pass  # dòng xóa: không check, không tăng số dòng mới
            elif not dong.startswith("\\"):
                dong_moi += 1  # dòng context: vẫn tăng số dòng file mới
        issues.sort(key=lambda i: (i["line"], i["rule"]))
        return issues

    # -- to_markdown -------------------------------------------------------

    def to_markdown(self, issues, tieu_de="Báo cáo code review"):
        """Format báo cáo tiếng Việt, nhóm theo severity."""
        thu_tu = {"error": 0, "warning": 1, "info": 2}
        nhan = {"error": "🔴 Lỗi", "warning": "🟡 Cảnh báo", "info": "🔵 Thông tin"}
        dem = {"error": 0, "warning": 0, "info": 0}
        for i in issues:
            dem[i["severity"]] = dem.get(i["severity"], 0) + 1

        dong = ["# %s" % tieu_de, "",
                "Tổng: %d vấn đề (🔴 %d lỗi, 🟡 %d cảnh báo, 🔵 %d thông tin)"
                % (len(issues), dem["error"], dem["warning"], dem["info"]), ""]

        sap_xep = sorted(issues,
                         key=lambda i: (thu_tu.get(i["severity"], 3), i["line"]))
        nhom_hien_tai = None
        for i in sap_xep:
            if i["severity"] != nhom_hien_tai:
                nhom_hien_tai = i["severity"]
                dong.append("## %s" % nhan[nhom_hien_tai])
                dong.append("")
            dong.append("- dòng %d [`%s`] %s"
                        % (i["line"], i["rule"], i["message"]))
        if not issues:
            dong.append("Không phát hiện vấn đề nào. 🎉")
        dong.append("")
        return "\n".join(dong)
