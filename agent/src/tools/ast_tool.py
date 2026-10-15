import ast
from pathlib import Path
from typing import Any, Dict, List

class ASTInspector:
    """AST-aware structural code inspector extracting functions, classes, and imports."""

    @staticmethod
    def inspect_file(file_path: str) -> Dict[str, Any]:
        p = Path(file_path)
        if not p.exists():
            return {"error": f"File '{file_path}' does not exist"}

        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception as e:
            return {"error": f"Failed to parse AST: {e}"}

        classes: List[Dict[str, Any]] = []
        functions: List[Dict[str, Any]] = []
        imports: List[str] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                methods = [m.name for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
                classes.append({"name": node.name, "lineno": node.lineno, "methods": methods})
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append({"name": node.name, "lineno": node.lineno})
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for alias in node.names:
                    imports.append(f"{mod}.{alias.name}")

        return {
            "file": str(p),
            "classes": classes,
            "functions": functions,
            "imports": imports
        }
