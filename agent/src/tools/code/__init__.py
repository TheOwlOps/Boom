"""Tools nhóm code: review / phân tích phụ thuộc / AST / patch mờ.

Wrap các module có sẵn trong repo (không copy logic):
  - ``tools/ast_tool.py``              -> ast_parse
  - ``tools/fuzzy_patch.py``           -> fuzzy_patch
  - ``agent/src/autonomous/codereview.py`` -> code_review (CodeReviewer)
  - ``agent/src/autonomous/depgraph.py``   -> dep_cycles (build_graph+find_cycles)

Import theo kiểu lazy + try/except: nếu repo thiếu module nào, tool tương
ứng trả về {"error"} rõ ràng thay vì crash import cả package.
"""

from typing import Any, Dict, List, Optional

__all__ = [
    "ast_parse",
    "fuzzy_patch",
    "code_review",
    "dep_cycles",
    "build_tools",
]

# --- wrap module repo (giữ nguyên logic gốc, chỉ thêm lớp tool) ------------
try:
    from tools.ast_tool import ASTInspector as _ASTInspector
except ImportError:  # chạy standalone ngoài repo
    _ASTInspector = None  # type: ignore[assignment]

try:
    from tools.fuzzy_patch import fuzzy_find_and_replace as _fuzzy_find_and_replace
except ImportError:
    _fuzzy_find_and_replace = None  # type: ignore[assignment]

try:
    from autonomous.codereview import CodeReviewer as _CodeReviewer
except ImportError:
    _CodeReviewer = None  # type: ignore[assignment]

try:
    from autonomous.depgraph import build_graph as _build_graph
    from autonomous.depgraph import find_cycles as _find_cycles
except ImportError:
    _build_graph = None  # type: ignore[assignment]
    _find_cycles = None  # type: ignore[assignment]


def _need(mod, name: str):
    if mod is None:
        raise RuntimeError(
            f"module gốc cho tool {name!r} không khả dụng trong môi trường này"
        )
    return mod


# ------------------------------------------------------------------ tools
def ast_parse(file_path: str) -> Dict[str, Any]:
    """Parse 1 file Python bằng AST thật, trả về cấu trúc code.

    Kết quả: {file, classes: [{name, lineno, methods}], functions, imports}.
    File không tồn tại / parse lỗi -> {"error"} (từ ASTInspector gốc).
    """
    inspector = _need(_ASTInspector, "ast_parse")
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("file_path phải là string không rỗng")
    return inspector.inspect_file(file_path.strip())


def fuzzy_patch(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> Dict[str, Any]:
    """Find-and-replace chịu lỗi (fuzzy) cho agent sửa file.

    Thử lần lượt: exact -> chuẩn hóa CRLF -> chuẩn hóa Unicode -> trim
    whitespace từng dòng -> fuzzy similarity >= 0.85. Trả về
    {new_content, match_count, strategy, error}.
    """
    fn = _need(_fuzzy_find_and_replace, "fuzzy_patch")
    if not isinstance(content, str) or not isinstance(old_string, str) \
            or not isinstance(new_string, str):
        raise ValueError("content/old_string/new_string phải là string")
    new_content, count, strategy, error = fn(
        content, old_string, new_string, bool(replace_all)
    )
    return {
        "new_content": new_content,
        "match_count": count,
        "strategy": strategy,
        "error": error,
    }


def code_review(path: str) -> Dict[str, Any]:
    """Review tĩnh 1 file Python bằng AST thật (CodeReviewer của repo).

    Rules: complexity>10, hàm >50 dòng, file >500 dòng, dead code, bare
    except, TODO/FIXME, print(). Trả về {path, issues: [{line, severity,
    rule, message}], summary: {error, warning, info}}.
    """
    reviewer_cls = _need(_CodeReviewer, "code_review")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path phải là string không rỗng")
    issues: List[Dict[str, Any]] = reviewer_cls().review_file(path.strip())
    summary = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        sev = issue.get("severity")
        if sev in summary:
            summary[sev] += 1
    return {"path": path.strip(), "issues": issues, "summary": summary}


def dep_cycles(root_dir: str) -> Dict[str, Any]:
    """Dựng dependency graph Python rồi tìm circular imports.

    Dùng build_graph + find_cycles của autonomous/depgraph.py.
    Trả về {root_dir, modules: <số module>, cycles: [[m1, m2, ...], ...]}.
    cycles rỗng = không có vòng tròn.
    """
    build_graph = _need(_build_graph, "dep_cycles")
    find_cycles = _need(_find_cycles, "dep_cycles")
    if not isinstance(root_dir, str) or not root_dir.strip():
        raise ValueError("root_dir phải là string không rỗng")
    graph = build_graph(root_dir.strip())
    cycles = find_cycles(graph)
    return {
        "root_dir": root_dir.strip(),
        "modules": len(graph),
        "cycles": cycles,
    }


# ------------------------------------------------------------- registry
def _guard(fn):
    try:
        return fn()
    except Exception as exc:  # tool không bao giờ raise vào agent loop
        return {"error": f"{type(exc).__name__}: {exc}"}


def _schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {},
            "required": required or []}


def build_tools(env=None) -> Dict[str, Dict[str, Any]]:
    """Registry-ready tool dicts cho environment của Boom."""

    def t_ast_parse(file_path: str):
        return _guard(lambda: ast_parse(file_path))

    def t_fuzzy_patch(content: str, old_string: str, new_string: str,
                      replace_all: bool = False):
        return _guard(lambda: fuzzy_patch(content, old_string, new_string,
                                          bool(replace_all)))

    def t_code_review(path: str):
        return _guard(lambda: code_review(path))

    def t_dep_cycles(root_dir: str):
        return _guard(lambda: dep_cycles(root_dir))

    return {
        "ast_parse": {
            "name": "ast_parse",
            "func": t_ast_parse,
            "description": (
                "Parse 1 file Python bằng AST: trả về classes/functions/imports "
                "kèm số dòng. Lỗi parse -> {\"error\"}."
            ),
            "schema": _schema(
                {"file_path": {"type": "string", "description": "Đường dẫn file .py."}},
                ["file_path"],
            ),
        },
        "fuzzy_patch": {
            "name": "fuzzy_patch",
            "func": t_fuzzy_patch,
            "description": (
                "Sửa text kiểu find-and-replace chịu lỗi: exact -> CRLF -> "
                "Unicode -> trim whitespace -> fuzzy >= 0.85. Trả về "
                "{new_content, match_count, strategy, error}."
            ),
            "schema": _schema(
                {
                    "content": {"type": "string", "description": "Nội dung gốc."},
                    "old_string": {"type": "string", "description": "Đoạn cần thay."},
                    "new_string": {"type": "string", "description": "Đoạn thay vào."},
                    "replace_all": {
                        "type": "boolean",
                        "description": "True để thay mọi vị trí. Mặc định false (đòi unique).",
                    },
                },
                ["content", "old_string", "new_string"],
            ),
        },
        "code_review": {
            "name": "code_review",
            "func": t_code_review,
            "description": (
                "Review tĩnh file Python: complexity, hàm dài, dead code, "
                "bare except, TODO, print(). Trả về issues + summary theo "
                "severity."
            ),
            "schema": _schema(
                {"path": {"type": "string", "description": "Đường dẫn file .py cần review."}},
                ["path"],
            ),
        },
        "dep_cycles": {
            "name": "dep_cycles",
            "func": t_dep_cycles,
            "description": (
                "Quét cây thư mục Python, dựng dependency graph và tìm "
                "circular imports. Trả về {modules, cycles}."
            ),
            "schema": _schema(
                {"root_dir": {"type": "string", "description": "Thư mục gốc chứa code Python."}},
                ["root_dir"],
            ),
        },
    }
