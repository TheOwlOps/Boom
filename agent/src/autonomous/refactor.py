"""Refactor engine cho Boom: refactor đa file dùng AST thật, không regex mù.

Ba thao tác chính:
  - rename_symbol:    đổi tên biến/hàm/class xuyên nhiều file
  - extract_function: tách đoạn code thành hàm mới (tự suy params/return)
  - move_symbol:      chuyển 1 hàm/class top-level sang file khác

Quy ước an toàn (tái dùng pattern của agent/src/autonomous/surgery.py):
  - backup  : copy mỗi file thành "<path>.bak-<timestamp>" trước khi sửa
  - validate: ast.parse lại sau khi sửa; lỗi syntax -> rollback toàn bộ file
  - rollback: khôi phục nội dung từ file .bak

Mọi thao tác đều hỗ trợ dry_run=True để preview diff mà không ghi file.

Chỉ dùng stdlib: ast, builtins, difflib, keyword, shutil, textwrap, time, pathlib.
"""

import ast
import builtins
import difflib
import keyword
import shutil
import textwrap
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# helper nội bộ
# ---------------------------------------------------------------------------

def _la_ten_hop_le(name):
    """Tên Python hợp lệ và không phải keyword (chặn new_name bậy)."""
    return isinstance(name, str) and name.isidentifier() \
        and not keyword.iskeyword(name)


def _doc(path):
    """Đọc file, trả về (text, list_dong_giu_newline)."""
    text = Path(path).read_text(encoding="utf-8")
    return text, text.splitlines(keepends=True)


def _offset_dong(lines):
    """Map lineno (1-based) -> offset ký tự bắt đầu dòng trong text."""
    offsets = {}
    pos = 0
    for i, line in enumerate(lines, start=1):
        offsets[i] = pos
        pos += len(line)
    return offsets


def _unified_diff(path, before, after):
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile="a/" + str(path),
        tofile="b/" + str(path),
    ))


def _newline_cua(text):
    return "\r\n" if "\r\n" in text else "\n"


def _span_ten_def(lines, node, old_name):
    """Span chính xác của tên trong dòng 'def old_name'/'class old_name'.

    ast không cho sẵn col của tên def/class; dùng text thật của dòng tìm
    từ khóa def/class rồi tên ngay sau — đúng cả khi có decorator
    hoặc 'async def'.
    """
    line = lines[node.lineno - 1]
    for kw in ("def ", "class "):
        idx = line.find(kw)
        if idx != -1:
            start = idx + len(kw)
            if line[start:start + len(old_name)] == old_name:
                return (node.lineno, start, node.lineno, start + len(old_name))
    raise ValueError("không tìm thấy tên def/class trong dòng: %r" % line)


class _TimViTriDoiTen(ast.NodeVisitor):
    """Thu thập span (l1, c1, l2, c2) của mọi chỗ cần đổi tên.

    BỎ QUA Attribute: Attribute.attr không phải node Name nên
    obj.old_name tự động không bị đụng — an toàn nhất.
    """

    def __init__(self, old_name, lines):
        self.old = old_name
        self.lines = lines
        self.spans = []

    def _ghi(self, node):
        self.spans.append(
            (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)
        )

    def visit_Name(self, node):
        # Name gồm cả Load và Store.
        if node.id == self.old:
            self._ghi(node)
        self.generic_visit(node)

    def _ghi_def(self, node):
        if node.name == self.old:
            self.spans.append(_span_ten_def(self.lines, node, self.old))
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self._ghi_def(node)

    def visit_AsyncFunctionDef(self, node):
        self._ghi_def(node)

    def visit_ClassDef(self, node):
        self._ghi_def(node)

    def visit_arg(self, node):
        if node.arg == self.old:
            self._ghi(node)
        self.generic_visit(node)


def _tim_scope_bao(tree, start_line, end_line):
    """Scope (FunctionDef/ClassDef) nhỏ nhất chứa trọn đoạn; không có -> module."""
    ung_vien = None

    class _Tim(ast.NodeVisitor):
        def _xet(self, node):
            nonlocal ung_vien
            bat_dau = node.lineno
            ket_thuc = getattr(node, "end_lineno", bat_dau) or bat_dau
            if bat_dau <= start_line and ket_thuc >= end_line:
                if ung_vien is None or bat_dau >= ung_vien.lineno:
                    ung_vien = node

        def visit_FunctionDef(self, node):
            self._xet(node)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node):
            self._xet(node)
            self.generic_visit(node)

        def visit_ClassDef(self, node):
            self._xet(node)
            self.generic_visit(node)

    _Tim().visit(tree)
    return ung_vien if ung_vien is not None else tree


class _ThuThapDoan(ast.NodeVisitor):
    """Thu thập tên load/store trong đoạn code (đã dedent + parse riêng).

    Hàm lồng bên trong: chỉ lấy tên hàm, không đi vào thân (scope riêng).
    """

    def __init__(self):
        self.loaded = set()
        self.stored = set()

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.loaded.add(node.id)
        elif isinstance(node.ctx, (ast.Store, ast.Del)):
            self.stored.add(node.id)
        self.generic_visit(node)

    def visit_arg(self, node):
        self.stored.add(node.arg)
        self.generic_visit(node)

    def _ham_long(self, node):
        self.stored.add(node.name)
        for dec in node.decorator_list:
            self.visit(dec)
        # không visit thân: tên bên trong thuộc scope riêng của hàm lồng

    def visit_FunctionDef(self, node):
        self._ham_long(node)

    def visit_AsyncFunctionDef(self, node):
        self._ham_long(node)

    def visit_ClassDef(self, node):
        self.stored.add(node.name)
        for dec in node.decorator_list:
            self.visit(dec)
        for base in node.bases:
            self.visit(base)

    def visit_Import(self, node):
        for a in node.names:
            self.stored.add((a.asname or a.name).split(".")[0])

    def visit_ImportFrom(self, node):
        for a in node.names:
            self.stored.add(a.asname or a.name)


# ---------------------------------------------------------------------------
# RefactorEngine
# ---------------------------------------------------------------------------

class RefactorEngine:
    """Engine refactor dùng AST. root_dir: thư mục gốc để resolve path."""

    def __init__(self, root_dir="."):
        self.root = Path(root_dir).resolve()
        # backup của thao tác gần nhất: {path_str: backup_path_str}
        self._backups = {}

    # -- backup / rollback (pattern của surgery.py) ------------------------

    def _backup(self, path):
        """Copy file thành '<path>.bak-<timestamp>'. Trả về đường dẫn backup."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError("file not found: %s" % path)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bak = Path(str(p) + ".bak-" + stamp)
        dem = 0
        while bak.exists():
            dem += 1
            bak = Path("%s.bak-%s-%d" % (p, stamp, dem))
        shutil.copy2(p, bak)
        self._backups[str(p)] = str(bak)
        return str(bak)

    def rollback(self):
        """Khôi phục toàn bộ file đã backup trong thao tác gần nhất."""
        ket_qua = {}
        for path, bak in self._backups.items():
            if Path(bak).is_file():
                shutil.copy2(bak, path)
                ket_qua[path] = True
            else:
                ket_qua[path] = False
        self._backups = {}
        return ket_qua

    def _ghi_va_validate(self, path, content_moi):
        """Ghi file rồi validate syntax; lỗi -> raise để caller rollback."""
        Path(path).write_text(content_moi, encoding="utf-8")
        try:
            ast.parse(content_moi, filename=str(path))
        except SyntaxError as exc:
            raise SyntaxError(
                "syntax lỗi sau khi sửa %s dòng %s: %s"
                % (path, exc.lineno, exc.msg)
            )

    def _resolve(self, f):
        p = Path(f)
        return str(p if p.is_absolute() else self.root / f)

    # -- rename_symbol -----------------------------------------------------

    def rename_symbol(self, old_name, new_name, files, dry_run=False):
        """Đổi tên biến/hàm/class xuyên nhiều file bằng AST.

        - Tìm: Name(id=old), FunctionDef/AsyncFunctionDef/ClassDef(name=old),
          arg(arg=old). Attribute KHÔNG đổi (bỏ qua an toàn).
        - Rewrite theo vị trí token, từ cuối file lên đầu (không lệch offset).
        - Validate: parse lại sau khi sửa, lỗi -> rollback toàn bộ file.
        - Trả về {"ok", "renamed_files", "occurrences", "diffs", ...}.
        """
        if not _la_ten_hop_le(old_name) or not _la_ten_hop_le(new_name):
            return {"ok": False, "error": "tên không hợp lệ"}
        if old_name == new_name:
            return {"ok": False, "error": "tên cũ và mới trùng nhau"}

        ke_hoach = {}  # path -> (content_moi, so_luong, diff)
        for f in files:
            path = self._resolve(f)
            try:
                text, lines = _doc(path)
            except OSError as exc:
                return {"ok": False,
                        "error": "không đọc được %s: %s" % (f, exc)}
            try:
                tree = ast.parse(text, filename=path)
            except SyntaxError as exc:
                return {"ok": False,
                        "error": "syntax lỗi sẵn ở %s dòng %s: %s"
                                 % (f, exc.lineno, exc.msg)}

            visitor = _TimViTriDoiTen(old_name, lines)
            visitor.visit(tree)
            spans = sorted(set(visitor.spans), key=lambda s: (s[0], s[1]))
            if not spans:
                continue  # file này không có occurrence nào

            offsets = _offset_dong(lines)
            # Kiểm tra text tại mỗi span phải đúng là old_name (an toàn)
            abs_spans = []
            for (l1, c1, l2, c2) in spans:
                doan = text[offsets[l1] + c1: offsets[l2] + c2]
                if doan != old_name:
                    return {"ok": False,
                            "error": "span không khớp tên ở %s:%d" % (f, l1)}
                abs_spans.append((offsets[l1] + c1, offsets[l2] + c2))

            # Thay từ cuối file lên đầu để không lệch offset
            moi = text
            for bat_dau, ket_thuc in sorted(abs_spans, reverse=True):
                moi = moi[:bat_dau] + new_name + moi[ket_thuc:]

            # Validate trước khi ghi (kể cả dry_run cũng check)
            try:
                ast.parse(moi, filename=path)
            except SyntaxError as exc:
                return {"ok": False,
                        "error": "rename làm hỏng syntax %s dòng %s"
                                 % (f, exc.lineno)}
            ke_hoach[path] = (moi, len(spans), _unified_diff(path, text, moi))

        ket_qua = {
            "ok": True,
            "renamed_files": sorted(ke_hoach.keys()),
            "occurrences": sum(v[1] for v in ke_hoach.values()),
            "diffs": {p: v[2] for p, v in ke_hoach.items()},
        }
        if dry_run:
            ket_qua["dry_run"] = True
            # check syntax ở trên đã pass cho mọi file trong kế hoạch
            ket_qua["syntax_ok"] = True
            return ket_qua

        # Ghi thật: backup từng file trước, lỗi ở file nào -> rollback hết
        self._backups = {}
        da_ghi = []
        try:
            for path, (moi, _n, _d) in ke_hoach.items():
                self._backup(path)
                self._ghi_va_validate(path, moi)
                da_ghi.append(path)
        except (OSError, SyntaxError) as exc:
            self.rollback()
            return {"ok": False,
                    "error": "lỗi khi ghi, đã rollback: %s" % exc,
                    "rolled_back": da_ghi}
        ket_qua["backups"] = dict(self._backups)
        return ket_qua

    # -- extract_function --------------------------------------------------

    def extract_function(self, source_path, start_line, end_line, new_name,
                         dry_run=False):
        """Tách dòng [start_line, end_line] thành hàm mới tên new_name.

        - Biến dùng trong đoạn nhưng định nghĩa ở ngoài -> parameters
          (theo thứ tự xuất hiện, bỏ builtin).
        - Biến gán trong đoạn và còn dùng sau đoạn -> return.
        - Chèn 'def new_name(...)' ngay trước start_line, thay đoạn cũ
          bằng lời gọi hàm (gán kết quả nếu có return).
        """
        if not _la_ten_hop_le(new_name):
            return {"ok": False, "error": "tên hàm không hợp lệ"}
        if start_line < 1 or end_line < start_line:
            return {"ok": False, "error": "khoảng dòng không hợp lệ"}

        path = self._resolve(source_path)
        try:
            text, lines = _doc(path)
        except OSError as exc:
            return {"ok": False, "error": "không đọc được file: %s" % exc}
        if end_line > len(lines):
            return {"ok": False, "error": "end_line vượt quá số dòng file"}

        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError as exc:
            return {"ok": False,
                    "error": "syntax lỗi sẵn dòng %s: %s" % (exc.lineno, exc.msg)}

        doan_lines = lines[start_line - 1:end_line]
        if not any(dl.strip() for dl in doan_lines):
            return {"ok": False, "error": "đoạn chọn toàn dòng trống"}
        # Đoạn phải là code hoàn chỉnh -> dedent rồi parse thử
        doan_text = textwrap.dedent("".join(doan_lines))
        try:
            doan_tree = ast.parse(doan_text)
        except SyntaxError as exc:
            return {"ok": False,
                    "error": "đoạn chọn không phải code hoàn chỉnh: %s" % exc.msg}

        thu = _ThuThapDoan()
        thu.visit(doan_tree)
        ten_builtin = set(dir(builtins))

        # Free variables theo thứ tự xuất hiện -> parameters
        params = []
        for node in ast.walk(doan_tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                    and node.id not in thu.stored \
                    and node.id not in ten_builtin \
                    and node.id not in params:
                params.append(node.id)

        # Biến gán trong đoạn mà còn dùng SAU đoạn (trong scope bao) -> return
        scope_bao = _tim_scope_bao(tree, start_line, end_line)
        dung_sau = set()
        for node in ast.walk(scope_bao):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                    and getattr(node, "lineno", 0) > end_line:
                dung_sau.add(node.id)
        can_return = sorted((thu.stored & dung_sau) - set(params))

        # Dựng hàm mới, indent theo dòng đầu của đoạn
        dong_dau = doan_lines[0]
        indent_goc = dong_dau[:len(dong_dau) - len(dong_dau.lstrip())]
        nl = _newline_cua(text)
        than_ham = "".join(
            ("    " + dl if dl.strip() else dl) for dl in doan_lines
        )
        if can_return:
            tra_ve = "%s    return %s%s" % (
                indent_goc, ", ".join(can_return), nl)
        else:
            tra_ve = ""
        ham_moi = "%sdef %s(%s):%s%s%s%s" % (
            indent_goc, new_name, ", ".join(params), nl, than_ham, tra_ve, nl)

        # Lời gọi thay thế đoạn cũ
        doi_so = ", ".join(params)
        if can_return:
            goi = "%s%s = %s(%s)%s" % (
                indent_goc, ", ".join(can_return), new_name, doi_so, nl)
        else:
            goi = "%s%s(%s)%s" % (indent_goc, new_name, doi_so, nl)

        lines_moi = (lines[:start_line - 1] + [ham_moi] + [goi]
                     + lines[end_line:])
        text_moi = "".join(lines_moi)
        try:
            ast.parse(text_moi, filename=path)
        except SyntaxError as exc:
            return {"ok": False,
                    "error": "extract làm hỏng syntax dòng %s: %s"
                             % (exc.lineno, exc.msg)}

        diff = _unified_diff(path, text, text_moi)
        if dry_run:
            return {"ok": True, "dry_run": True, "diff": diff,
                    "params": params, "returns": can_return,
                    "syntax_ok": True}

        self._backups = {}
        try:
            self._backup(path)
            self._ghi_va_validate(path, text_moi)
        except (OSError, SyntaxError) as exc:
            self.rollback()
            return {"ok": False, "error": "lỗi khi ghi, đã rollback: %s" % exc}
        return {"ok": True, "diff": diff, "params": params,
                "returns": can_return, "backups": dict(self._backups)}

    # -- move_symbol -------------------------------------------------------

    def move_symbol(self, source_path, dest_path, symbol_name, dry_run=False):
        """Chuyển 1 hàm/class top-level sang file khác.

        - Cắt source segment bằng ast.get_source_segment (giữ decorator).
        - Xóa khỏi file nguồn, append vào file đích kèm comment nguồn gốc.
        - Validate cả 2 file; lỗi -> rollback cả 2.
        """
        if not _la_ten_hop_le(symbol_name):
            return {"ok": False, "error": "tên symbol không hợp lệ"}

        src = self._resolve(source_path)
        dst = self._resolve(dest_path)
        if src == dst:
            return {"ok": False, "error": "file nguồn và đích trùng nhau"}
        try:
            text_src, lines_src = _doc(src)
            text_dst, _lines_dst = _doc(dst)
        except OSError as exc:
            return {"ok": False, "error": "không đọc được file: %s" % exc}
        try:
            tree_src = ast.parse(text_src, filename=src)
        except SyntaxError as exc:
            return {"ok": False,
                    "error": "syntax lỗi sẵn ở nguồn dòng %s" % exc.lineno}
        try:
            ast.parse(text_dst, filename=dst)
        except SyntaxError as exc:
            return {"ok": False,
                    "error": "syntax lỗi sẵn ở đích dòng %s" % exc.lineno}

        # Tìm def/class top-level trùng tên. Lưu ý: FunctionDef.lineno
        # trỏ tới dòng 'def', KHÔNG gồm decorator -> tự tính dòng bắt đầu.
        node = None
        for item in tree_src.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)) and item.name == symbol_name:
                node = item
                break
        if node is None:
            return {"ok": False,
                    "error": "không tìm thấy hàm/class top-level '%s'"
                             % symbol_name}

        dong_bat_dau = node.lineno
        if node.decorator_list:
            dong_bat_dau = min(d.lineno for d in node.decorator_list)
        segment = "".join(lines_src[dong_bat_dau - 1:node.end_lineno])
        if not segment.strip():
            return {"ok": False, "error": "không lấy được source segment"}

        # Xóa khỏi nguồn theo dòng [dong_bat_dau, end_lineno],
        # dọn dòng trống thừa
        text_src_moi = "".join(lines_src[:dong_bat_dau - 1]
                               + lines_src[node.end_lineno:])
        while "\n\n\n" in text_src_moi:
            text_src_moi = text_src_moi.replace("\n\n\n", "\n\n")

        # Append vào đích kèm comment nguồn gốc (tối thiểu thay cho import
        # tự động — import đầy đủ cần phân tích dependency, ngoài phạm vi)
        nl = _newline_cua(text_dst)
        comment = "# moved from %s by RefactorEngine%s" % (src, nl)
        text_dst_moi = text_dst.rstrip("\n") + nl + nl + comment \
            + segment.rstrip("\n") + nl

        for label, p, t in (("nguồn", src, text_src_moi),
                            ("đích", dst, text_dst_moi)):
            try:
                ast.parse(t, filename=p)
            except SyntaxError as exc:
                return {"ok": False,
                        "error": "move làm hỏng syntax file %s dòng %s"
                                 % (label, exc.lineno)}

        diff_src = _unified_diff(src, text_src, text_src_moi)
        diff_dst = _unified_diff(dst, text_dst, text_dst_moi)
        if dry_run:
            return {"ok": True, "dry_run": True,
                    "diff_source": diff_src, "diff_dest": diff_dst,
                    "syntax_ok": True}

        self._backups = {}
        try:
            self._backup(src)
            self._backup(dst)
            self._ghi_va_validate(src, text_src_moi)
            self._ghi_va_validate(dst, text_dst_moi)
        except (OSError, SyntaxError) as exc:
            self.rollback()
            return {"ok": False, "error": "lỗi khi ghi, đã rollback: %s" % exc}
        return {"ok": True, "diff_source": diff_src, "diff_dest": diff_dst,
                "backups": dict(self._backups)}
