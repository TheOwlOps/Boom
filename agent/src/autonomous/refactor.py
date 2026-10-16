"""Refactor engine for Boom: multi-file AST-based refactoring, no blind regex.

Three main operations:
  - rename_symbol:    rename a variable/function/class across many files
  - extract_function: extract a code block into a new function (params/return inferred)
  - move_symbol:      move one top-level function/class to another file

Safety conventions (reusing the pattern from agent/src/autonomous/surgery.py):
  - backup  : copy each file to "<path>.bak-<timestamp>" before editing
  - validate: re-parse with ast.parse after editing; syntax error -> roll back the whole file
  - rollback: restore content from the .bak file

Every operation supports dry_run=True to preview the diff without writing files.

Stdlib only: ast, builtins, difflib, keyword, shutil, textwrap, time, pathlib.
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
# internal helpers
# ---------------------------------------------------------------------------

def _la_ten_hop_le(name):
    """A valid Python name that is not a keyword (guards against junk new_name)."""
    return isinstance(name, str) and name.isidentifier() \
        and not keyword.iskeyword(name)


def _doc(path):
    """Read a file, return (text, list_of_lines_with_newlines)."""
    text = Path(path).read_text(encoding="utf-8")
    return text, text.splitlines(keepends=True)


def _offset_dong(lines):
    """Map lineno (1-based) -> character offset where the line starts in text."""
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
    """Exact span of the name on a 'def old_name'/'class old_name' line.

    ast doesn't provide the col of a def/class name; search the actual line
    text for the def/class keyword and the name right after it — works with
    decorators and 'async def' too.
    """
    line = lines[node.lineno - 1]
    for kw in ("def ", "class "):
        idx = line.find(kw)
        if idx != -1:
            start = idx + len(kw)
            if line[start:start + len(old_name)] == old_name:
                return (node.lineno, start, node.lineno, start + len(old_name))
    raise ValueError("def/class name not found in line: %r" % line)


class _TimViTriDoiTen(ast.NodeVisitor):
    """Collects (l1, c1, l2, c2) spans of every place to rename.

    SKIPS Attribute: Attribute.attr is not a Name node, so
    obj.old_name is automatically left alone — the safest behavior.
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
        # Name covers both Load and Store.
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
    """Smallest (FunctionDef/ClassDef) scope fully containing the span; module if none."""
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
    """Collects loaded/stored names in a code block (dedented + parsed separately).

    Nested functions: only the function name is taken; its body is not visited
    (separate scope).
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
        # body not visited: names inside belong to the nested function's own scope

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
    """AST-based refactor engine. root_dir: base directory for path resolution."""

    def __init__(self, root_dir="."):
        self.root = Path(root_dir).resolve()
        # backups of the most recent operation: {path_str: backup_path_str}
        self._backups = {}

    # -- backup / rollback (surgery.py pattern) -----------------------------

    def _backup(self, path):
        """Copy the file to '<path>.bak-<timestamp>'. Returns the backup path."""
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
        """Restore every file backed up by the most recent operation."""
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
        """Write the file then validate syntax; error -> raise so caller rolls back."""
        Path(path).write_text(content_moi, encoding="utf-8")
        try:
            ast.parse(content_moi, filename=str(path))
        except SyntaxError as exc:
            raise SyntaxError(
                "syntax error after editing %s line %s: %s"
                % (path, exc.lineno, exc.msg)
            )

    def _resolve(self, f):
        p = Path(f)
        return str(p if p.is_absolute() else self.root / f)

    # -- rename_symbol -----------------------------------------------------

    def rename_symbol(self, old_name, new_name, files, dry_run=False):
        """Rename a variable/function/class across many files using AST.

        - Finds: Name(id=old), FunctionDef/AsyncFunctionDef/ClassDef(name=old),
          arg(arg=old). Attribute is NOT renamed (safely skipped).
        - Rewrites by token position, from end of file backwards (no offset drift).
        - Validates: re-parses after editing, errors -> roll back every file.
        - Returns {"ok", "renamed_files", "occurrences", "diffs", ...}.
        """
        if not _la_ten_hop_le(old_name) or not _la_ten_hop_le(new_name):
            return {"ok": False, "error": "invalid name"}
        if old_name == new_name:
            return {"ok": False, "error": "old and new names are identical"}

        ke_hoach = {}  # path -> (new_content, count, diff)
        for f in files:
            path = self._resolve(f)
            try:
                text, lines = _doc(path)
            except OSError as exc:
                return {"ok": False,
                        "error": "could not read %s: %s" % (f, exc)}
            try:
                tree = ast.parse(text, filename=path)
            except SyntaxError as exc:
                return {"ok": False,
                        "error": "pre-existing syntax error in %s line %s: %s"
                                 % (f, exc.lineno, exc.msg)}

            visitor = _TimViTriDoiTen(old_name, lines)
            visitor.visit(tree)
            spans = sorted(set(visitor.spans), key=lambda s: (s[0], s[1]))
            if not spans:
                continue  # this file has no occurrences

            offsets = _offset_dong(lines)
            # The text at every span must be exactly old_name (safety check)
            abs_spans = []
            for (l1, c1, l2, c2) in spans:
                doan = text[offsets[l1] + c1: offsets[l2] + c2]
                if doan != old_name:
                    return {"ok": False,
                            "error": "span name mismatch at %s:%d" % (f, l1)}
                abs_spans.append((offsets[l1] + c1, offsets[l2] + c2))

            # Replace from end of file backwards so offsets don't shift
            moi = text
            for bat_dau, ket_thuc in sorted(abs_spans, reverse=True):
                moi = moi[:bat_dau] + new_name + moi[ket_thuc:]

            # Validate before writing (checked even for dry_run)
            try:
                ast.parse(moi, filename=path)
            except SyntaxError as exc:
                return {"ok": False,
                        "error": "rename broke syntax in %s line %s"
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
            # the syntax check above already passed for every file in the plan
            ket_qua["syntax_ok"] = True
            return ket_qua

        # Real write: back up each file first; any error -> roll everything back
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
                    "error": "write error, rolled back: %s" % exc,
                    "rolled_back": da_ghi}
        ket_qua["backups"] = dict(self._backups)
        return ket_qua

    # -- extract_function --------------------------------------------------

    def extract_function(self, source_path, start_line, end_line, new_name,
                         dry_run=False):
        """Extract lines [start_line, end_line] into a new function new_name.

        - Variables used in the block but defined outside -> parameters
          (in order of appearance, excluding builtins).
        - Variables assigned in the block and still used after it -> return.
        - Inserts 'def new_name(...)' right before start_line, replacing the
          old block with a call (assigning the result if there is a return).
        """
        if not _la_ten_hop_le(new_name):
            return {"ok": False, "error": "invalid function name"}
        if start_line < 1 or end_line < start_line:
            return {"ok": False, "error": "invalid line range"}

        path = self._resolve(source_path)
        try:
            text, lines = _doc(path)
        except OSError as exc:
            return {"ok": False, "error": "could not read file: %s" % exc}
        if end_line > len(lines):
            return {"ok": False, "error": "end_line exceeds file line count"}

        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError as exc:
            return {"ok": False,
                    "error": "pre-existing syntax error at line %s: %s" % (exc.lineno, exc.msg)}

        doan_lines = lines[start_line - 1:end_line]
        if not any(dl.strip() for dl in doan_lines):
            return {"ok": False, "error": "selected block is all blank lines"}
        # The block must be complete code -> dedent and try parsing
        doan_text = textwrap.dedent("".join(doan_lines))
        try:
            doan_tree = ast.parse(doan_text)
        except SyntaxError as exc:
            return {"ok": False,
                    "error": "selected block is not complete code: %s" % exc.msg}

        thu = _ThuThapDoan()
        thu.visit(doan_tree)
        ten_builtin = set(dir(builtins))

        # Free variables in order of appearance -> parameters
        params = []
        for node in ast.walk(doan_tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                    and node.id not in thu.stored \
                    and node.id not in ten_builtin \
                    and node.id not in params:
                params.append(node.id)

        # Variables assigned in the block and still used AFTER it (in the
        # enclosing scope) -> return values
        scope_bao = _tim_scope_bao(tree, start_line, end_line)
        dung_sau = set()
        for node in ast.walk(scope_bao):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                    and getattr(node, "lineno", 0) > end_line:
                dung_sau.add(node.id)
        can_return = sorted((thu.stored & dung_sau) - set(params))

        # Build the new function, indenting to the block's first line
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

        # The call replacing the old block
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
                    "error": "extract broke syntax at line %s: %s"
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
            return {"ok": False, "error": "write error, rolled back: %s" % exc}
        return {"ok": True, "diff": diff, "params": params,
                "returns": can_return, "backups": dict(self._backups)}

    # -- move_symbol -------------------------------------------------------

    def move_symbol(self, source_path, dest_path, symbol_name, dry_run=False):
        """Move one top-level function/class to another file.

        - Cuts the source segment with ast.get_source_segment (keeps decorators).
        - Removes it from the source file, appends to the destination with an
          origin comment.
        - Validates both files; error -> roll back both.
        """
        if not _la_ten_hop_le(symbol_name):
            return {"ok": False, "error": "invalid symbol name"}

        src = self._resolve(source_path)
        dst = self._resolve(dest_path)
        if src == dst:
            return {"ok": False, "error": "source and destination are the same file"}
        try:
            text_src, lines_src = _doc(src)
            text_dst, _lines_dst = _doc(dst)
        except OSError as exc:
            return {"ok": False, "error": "could not read file: %s" % exc}
        try:
            tree_src = ast.parse(text_src, filename=src)
        except SyntaxError as exc:
            return {"ok": False,
                    "error": "pre-existing syntax error in source at line %s" % exc.lineno}
        try:
            ast.parse(text_dst, filename=dst)
        except SyntaxError as exc:
            return {"ok": False,
                    "error": "pre-existing syntax error in destination at line %s" % exc.lineno}

        # Find the top-level def/class with the matching name. Note:
        # FunctionDef.lineno points at the 'def' line, NOT including
        # decorators -> compute the start line manually.
        node = None
        for item in tree_src.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)) and item.name == symbol_name:
                node = item
                break
        if node is None:
            return {"ok": False,
                    "error": "top-level function/class '%s' not found"
                             % symbol_name}

        dong_bat_dau = node.lineno
        if node.decorator_list:
            dong_bat_dau = min(d.lineno for d in node.decorator_list)
        segment = "".join(lines_src[dong_bat_dau - 1:node.end_lineno])
        if not segment.strip():
            return {"ok": False, "error": "could not capture source segment"}

        # Remove from source by line range [dong_bat_dau, end_lineno],
        # cleaning up leftover blank lines
        text_src_moi = "".join(lines_src[:dong_bat_dau - 1]
                               + lines_src[node.end_lineno:])
        while "\n\n\n" in text_src_moi:
            text_src_moi = text_src_moi.replace("\n\n\n", "\n\n")

        # Append to the destination with an origin comment (a minimal stand-in
        # for automatic imports — full import handling needs dependency
        # analysis, out of scope)
        nl = _newline_cua(text_dst)
        comment = "# moved from %s by RefactorEngine%s" % (src, nl)
        text_dst_moi = text_dst.rstrip("\n") + nl + nl + comment \
            + segment.rstrip("\n") + nl

        for label, p, t in (("source", src, text_src_moi),
                            ("destination", dst, text_dst_moi)):
            try:
                ast.parse(t, filename=p)
            except SyntaxError as exc:
                return {"ok": False,
                        "error": "move broke syntax in %s file at line %s"
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
            return {"ok": False, "error": "write error, rolled back: %s" % exc}
        return {"ok": True, "diff_source": diff_src, "diff_dest": diff_dst,
                "backups": dict(self._backups)}
