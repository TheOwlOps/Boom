"""Code tool group: review / dependency analysis / AST / fuzzy patch.

Wraps the repo's existing modules (no logic copied):
  - ``tools/ast_tool.py``              -> ast_parse
  - ``tools/fuzzy_patch.py``           -> fuzzy_patch
  - ``agent/src/autonomous/codereview.py`` -> code_review (CodeReviewer)
  - ``agent/src/autonomous/depgraph.py``   -> dep_cycles (build_graph+find_cycles)

Lazy + try/except imports: when the repo lacks a module, the corresponding
tool returns a clear {"error"} instead of crashing the whole package import.
"""

from typing import Any, Dict, List, Optional

__all__ = [
    "ast_parse",
    "fuzzy_patch",
    "code_review",
    "dep_cycles",
    "build_tools",
]

# --- wrap repo modules (keep original logic, add the tool layer) ----------
try:
    from tools.ast_tool import ASTInspector as _ASTInspector
except ImportError:  # running standalone outside the repo
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
            f"the source module for tool {name!r} is not available in this environment"
        )
    return mod


# ------------------------------------------------------------------ tools
def ast_parse(file_path: str) -> Dict[str, Any]:
    """Parses one Python file with a real AST, returns the code structure.

    Result: {file, classes: [{name, lineno, methods}], functions, imports}.
    Missing file / parse error -> {"error"} (from the original ASTInspector).
    """
    inspector = _need(_ASTInspector, "ast_parse")
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("file_path must be a non-empty string")
    return inspector.inspect_file(file_path.strip())


def fuzzy_patch(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> Dict[str, Any]:
    """Error-tolerant (fuzzy) find-and-replace for agent file edits.

    Tries in order: exact -> CRLF normalize -> Unicode normalize -> per-line
    whitespace trim -> fuzzy similarity >= 0.85. Returns
    {new_content, match_count, strategy, error}.
    """
    fn = _need(_fuzzy_find_and_replace, "fuzzy_patch")
    if not isinstance(content, str) or not isinstance(old_string, str) \
            or not isinstance(new_string, str):
        raise ValueError("content/old_string/new_string must be strings")
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
    """Static review of one Python file with a real AST (the repo's CodeReviewer).

    Rules: complexity>10, functions >50 lines, files >500 lines, dead code,
    bare except, TODO/FIXME, print(). Returns {path, issues: [{line, severity,
    rule, message}], summary: {error, warning, info}}.
    """
    reviewer_cls = _need(_CodeReviewer, "code_review")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")
    issues: List[Dict[str, Any]] = reviewer_cls().review_file(path.strip())
    summary = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        sev = issue.get("severity")
        if sev in summary:
            summary[sev] += 1
    return {"path": path.strip(), "issues": issues, "summary": summary}


def dep_cycles(root_dir: str) -> Dict[str, Any]:
    """Builds the Python dependency graph, then finds circular imports.

    Uses autonomous/depgraph.py's build_graph + find_cycles.
    Returns {root_dir, modules: <module count>, cycles: [[m1, m2, ...], ...]}.
    Empty cycles = no loops.
    """
    build_graph = _need(_build_graph, "dep_cycles")
    find_cycles = _need(_find_cycles, "dep_cycles")
    if not isinstance(root_dir, str) or not root_dir.strip():
        raise ValueError("root_dir must be a non-empty string")
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
    except Exception as exc:  # tools never raise into the agent loop
        return {"error": f"{type(exc).__name__}: {exc}"}


def _schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {},
            "required": required or []}


def build_tools(env=None) -> Dict[str, Dict[str, Any]]:
    """Registry-ready tool dicts for Boom's environment."""

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
                "Parses one Python file with AST: returns classes/functions/imports "
                "with line numbers. Parse error -> {\"error\"}."
            ),
            "schema": _schema(
                {"file_path": {"type": "string", "description": "Path to the .py file."}},
                ["file_path"],
            ),
        },
        "fuzzy_patch": {
            "name": "fuzzy_patch",
            "func": t_fuzzy_patch,
            "description": (
                "Error-tolerant find-and-replace editing: exact -> CRLF -> "
                "Unicode -> whitespace trim -> fuzzy >= 0.85. Returns "
                "{new_content, match_count, strategy, error}."
            ),
            "schema": _schema(
                {
                    "content": {"type": "string", "description": "Original content."},
                    "old_string": {"type": "string", "description": "Segment to replace."},
                    "new_string": {"type": "string", "description": "Replacement segment."},
                    "replace_all": {
                        "type": "boolean",
                        "description": "True to replace all occurrences. Default false (requires unique).",
                    },
                },
                ["content", "old_string", "new_string"],
            ),
        },
        "code_review": {
            "name": "code_review",
            "func": t_code_review,
            "description": (
                "Static review of a Python file: complexity, long functions, dead code, "
                "bare except, TODO, print(). Returns issues + severity summary."
            ),
            "schema": _schema(
                {"path": {"type": "string", "description": "Path to the .py file to review."}},
                ["path"],
            ),
        },
        "dep_cycles": {
            "name": "dep_cycles",
            "func": t_dep_cycles,
            "description": (
                "Scans a Python directory tree, builds the dependency graph and finds "
                "circular imports. Returns {modules, cycles}."
            ),
            "schema": _schema(
                {"root_dir": {"type": "string", "description": "Root dir containing Python code."}},
                ["root_dir"],
            ),
        },
    }
