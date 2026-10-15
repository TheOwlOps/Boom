"""Do test coverage bang stdlib cho Boom.

Dung sys.monitoring (Python 3.12+) de bat LINE event voi overhead thap,
fallback ve sys.settrace cho ban Python cu. Chi track file nam trong
include_dirs de loai bo stdlib/site-packages.

API chinh:
    tracker = CoverageTracker(include_dirs=[...])
    tracker.start()
    ... chay test ...
    executed = tracker.stop()          # {file: set(dong da chay)}

    rep = measure(target_files, test_fn)  # chay test_fn trong tracker, tra report
    md = to_markdown(rep)                # bang tieng Viet
"""

import ast
import io
import os
import sys
import tokenize
from pathlib import Path
from typing import Callable, Dict, List, Set

# True neu co sys.monitoring (Python 3.12+)
_HAS_MONITORING = hasattr(sys, "monitoring")


def _norm(path: str) -> str:
    """Chuan hoa path de so sanh: absolute + resolve symlink."""
    return os.path.realpath(os.path.abspath(path))


def _docstring_lines(src: str) -> Set[int]:
    """Tap dong thuoc docstring cua module/class/function (de loai khoi total).

    File loi syntax -> tra ve set rong (khong raise).
    """
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return set()
    out: Set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        # docstring = Expr(Constant(str)) nam dau tien trong body
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            out.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return out


def _code_lines(file_path: str) -> Set[int]:
    """Tap dong code 'thuc' cua 1 file: co token that, bo comment/blank/docstring.

    Dung tokenize nen chi dong nao co token thuc moi duoc tinh — khop voi
    cac dong co the nhan LINE event tu interpreter. File khong doc duoc
    hoac loi syntax -> set rong.
    """
    try:
        src = Path(file_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    doc_lines = _docstring_lines(src)
    skip_types = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                  tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING,
                  tokenize.ENDMARKER}
    code: Set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in skip_types:
                continue
            code.add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return code - doc_lines


class CoverageTracker:
    """Track dong da chay cua cac file trong include_dirs.

    Tu chon sys.monitoring (3.12+) hoac sys.settrace (ban cu).
    Co the goi start()/stop() nhieu lan lien tiep.
    """

    def __init__(self, include_dirs=None):
        # include_dirs: list thu muc (str/Path); file ngoai danh sach bi bo qua
        self.include_dirs = [_norm(str(d)) for d in (include_dirs or [])]
        self._use_monitoring = _HAS_MONITORING
        self._executed: Dict[str, Set[int]] = {}
        self._active = False
        self._mode = None            # "monitoring" | "settrace"
        self._tool_id = None
        self._old_trace = None

    # -- loc file ------------------------------------------------------

    def _wanted(self, filename: str) -> bool:
        """True neu file nam trong include_dirs (bo stdlib/site-packages...)."""
        if not filename or filename.startswith("<"):
            # <frozen ...>, <string>... khong phai file that
            return False
        p = _norm(filename)
        for d in self.include_dirs:
            if p == d or p.startswith(d + os.sep):
                return True
        return False

    def _record(self, filename: str, lineno: int) -> None:
        """Ghi nhan 1 dong da chay."""
        key = _norm(filename)
        s = self._executed.get(key)
        if s is None:
            s = self._executed[key] = set()
        s.add(lineno)

    # -- sys.monitoring (3.12+) -----------------------------------------

    def _mon_line_cb(self, code, line_number):
        """Callback cho LINE event: chi ghi nhan file duoc track."""
        if self._wanted(code.co_filename):
            self._record(code.co_filename, line_number)
        return None  # giu event bat cho cac dong tiep theo

    def _start_monitoring(self) -> None:
        mon = sys.monitoring
        tool_id = mon.COVERAGE_ID
        mon.use_tool_id(tool_id, "boom_coverage")
        mon.register_callback(tool_id, mon.events.LINE, self._mon_line_cb)
        mon.set_events(tool_id, mon.events.LINE)
        self._tool_id = tool_id
        self._mode = "monitoring"

    def _stop_monitoring(self) -> None:
        mon = sys.monitoring
        try:
            mon.set_events(self._tool_id, 0)
            mon.register_callback(self._tool_id, mon.events.LINE, None)
        finally:
            mon.free_tool_id(self._tool_id)
        self._tool_id = None

    # -- sys.settrace (fallback ban cu) -----------------------------------

    def _global_trace(self, frame, event, arg):
        # Chi lap local tracer cho frame thuoc file duoc track
        if event == "call" and self._wanted(frame.f_code.co_filename):
            return self._local_trace
        return None

    def _local_trace(self, frame, event, arg):
        if event == "line":
            self._record(frame.f_code.co_filename, frame.f_lineno)
        return self._local_trace

    # -- public API -------------------------------------------------------

    def start(self) -> None:
        """Bat dau trace. Goi nhieu lan lien tiep thi lan sau bo qua."""
        if self._active:
            return
        self._executed = {}
        self._active = True
        if self._use_monitoring:
            try:
                self._start_monitoring()
                return
            except Exception:
                # tool id bi chiem hoac loi la -> roi ve settrace
                self._use_monitoring = False
        self._old_trace = sys.gettrace()
        sys.settrace(self._global_trace)
        self._mode = "settrace"

    def stop(self) -> Dict[str, Set[int]]:
        """Dung trace, tra ve {file: set(dong da chay)}. Chua start -> {}."""
        if not self._active:
            return {}
        self._active = False
        if self._mode == "monitoring":
            self._stop_monitoring()
        else:
            sys.settrace(self._old_trace)
            self._old_trace = None
        result = self._executed
        self._executed = {}
        self._mode = None
        return result


def measure(target_files: List[str],
            test_fn: Callable[[], None]) -> Dict[str, Dict]:
    """Chay test_fn() trong tracker, tra ve report coverage.

    target_files: list file can do. include_dirs tu suy ra tu parent cua
    cac file nay. test_fn raise -> tracker van duoc stop sach, exception
    duoc nem tiep (khong tra report).
    """
    files = [_norm(str(f)) for f in target_files]
    dirs = sorted({_norm(str(Path(f).parent)) for f in files})
    tracker = CoverageTracker(include_dirs=dirs)
    tracker.start()
    try:
        test_fn()
    finally:
        executed = tracker.stop()
    return report(files, executed)


def report(target_files: List[str],
           executed: Dict[str, Set[int]]) -> Dict[str, Dict]:
    """Dung report tu executed map.

    Moi file: {total_lines, covered_lines, pct, uncovered: [dong chua chay]}.
    total_lines = dong code thuc (bo comment/blank/docstring).
    File khong ton tai / khong co code -> total 0, pct 100.0.
    """
    out: Dict[str, Dict] = {}
    for f in target_files:
        key = _norm(str(f))
        code = _code_lines(key)
        covered = executed.get(key, set()) & code  # giao de loai event la
        total = len(code)
        n_covered = len(covered)
        pct = round(n_covered / total * 100, 1) if total else 100.0
        out[key] = {
            "total_lines": total,
            "covered_lines": n_covered,
            "pct": pct,
            "uncovered": sorted(code - covered),
        }
    return out


def to_markdown(rep: Dict[str, Dict]) -> str:
    """Render report thanh bang markdown tieng Viet."""
    lines = [
        "## Bao cao coverage",
        "",
        "| File | Tong dong | Da chay | Ty le | Dong chua chay |",
        "|---|---|---|---|---|",
    ]
    for f, r in rep.items():
        name = Path(f).name
        unc = ", ".join(str(n) for n in r["uncovered"]) or "-"
        lines.append(
            f"| `{name}` | {r['total_lines']} | {r['covered_lines']} "
            f"| {r['pct']:.1f}% | {unc} |"
        )
    total = sum(r["total_lines"] for r in rep.values())
    covered = sum(r["covered_lines"] for r in rep.values())
    pct = round(covered / total * 100, 1) if total else 100.0
    lines += ["", f"_Tong: {covered}/{total} dong ({pct:.1f}%)_"]
    return "\n".join(lines) + "\n"
