#!/usr/bin/env python3
"""Verify Boom gateway API docs: openapi.json + API.md against gateway/api.py.

Checks:
  1. openapi.json parse được và valid OpenAPI 3.0 (có openapi, info, paths).
  2. Mọi endpoint trong api.py đều có trong spec (paths + methods).
  3. API.md đề cập đủ các endpoints.
  4. Cross-check: mọi path trong spec đều có trong API.md (không doc thừa/thiếu).

Usage: python3 test_openapi.py
Exit code 0 = tất cả PASS, != 0 = có FAIL.
"""

import ast
import json
import re
import sys
from pathlib import Path

REPO = Path("/tmp/boom_git")
API_PY = REPO / "gateway" / "api.py"
OPENAPI_JSON = REPO / "gateway" / "openapi.json"
API_MD = REPO / "docs" / "API.md"

failures: list[str] = []
passes: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        passes.append(name)
        print(f"  [PASS] {name}")
    else:
        failures.append(name)
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def extract_endpoints_from_code(path: Path) -> set[tuple[str, str]]:
    """AST: tìm các so sánh `path == "/api/..."` trong do_GET/do_POST/do_OPTIONS."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    eps: set[tuple[str, str]] = set()
    method_of = {"do_GET": "GET", "do_POST": "POST", "do_OPTIONS": "OPTIONS",
                 "do_PUT": "PUT", "do_DELETE": "DELETE", "do_PATCH": "PATCH"}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in method_of:
            http_method = method_of[node.name]
            for sub in ast.walk(node):
                if isinstance(sub, ast.Compare):
                    for op, comp in zip(sub.ops, sub.comparators):
                        if isinstance(op, ast.Eq) and isinstance(comp, ast.Constant):
                            val = comp.value
                            if isinstance(val, str) and val.startswith("/api/"):
                                eps.add((http_method, val))
    return eps


def main() -> int:
    print("== 1. openapi.json parse + valid OpenAPI 3.0 ==")
    try:
        spec = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))
        check("openapi.json parses as JSON", True)
    except Exception as e:
        check("openapi.json parses as JSON", False, str(e))
        print(f"\nRESULT: {len(passes)} PASS / {len(failures)} FAIL")
        return 1

    check("has 'openapi' (3.0.x)", isinstance(spec.get("openapi"), str)
          and spec["openapi"].startswith("3.0"))
    check("has 'info' (title + version)",
          isinstance(spec.get("info"), dict)
          and spec["info"].get("title") and spec["info"].get("version"))
    check("has 'paths' (non-empty)", isinstance(spec.get("paths"), dict)
          and len(spec["paths"]) > 0)
    check("every path item has an operation",
          all(isinstance(op, dict) and op.get("responses")
              for path_item in spec["paths"].values()
              for m, op in path_item.items()
              if m in ("get", "post", "put", "delete", "patch", "options")))

    print("\n== 2. Endpoints trong api.py đều có trong spec ==")
    code_eps = extract_endpoints_from_code(API_PY)
    # OPTIONS là preflight chung, không phải endpoint /api/* riêng
    code_eps = {e for e in code_eps if e[0] != "OPTIONS"}
    print(f"  endpoints trong api.py: {sorted(code_eps)}")
    check("tìm được endpoints trong code", len(code_eps) > 0,
          f"AST chỉ tìm được {len(code_eps)}")

    spec_eps = {(m.upper(), p) for p, item in spec["paths"].items()
                for m in item if m in ("get", "post", "put", "delete", "patch")}
    missing = code_eps - spec_eps
    check("mọi endpoint api.py đều có trong spec", not missing,
          f"thiếu: {sorted(missing)}")

    print("\n== 3. API.md đề cập đủ endpoints ==")
    md = API_MD.read_text(encoding="utf-8")
    md_missing = []
    for method, path in sorted(code_eps):
        # tìm "METHOD /api/..." trong markdown
        pattern = re.compile(rf"\b{method}\b[^\n]*`{re.escape(path)}`|{re.escape(path)}")
        if not re.search(pattern, md):
            md_missing.append(f"{method} {path}")
    check("API.md đề cập đủ endpoints", not md_missing,
          f"thiếu: {md_missing}")

    print("\n== 4. Spec không có path thừa (cross-check API.md) ==")
    spec_paths = set(spec["paths"].keys())
    extra = [p for p in spec_paths if p not in md]
    check("mọi path trong spec đều có trong API.md", not extra,
          f"thừa: {extra}")

    print(f"\nRESULT: {len(passes)} PASS / {len(failures)} FAIL "
          f"({len(code_eps)} endpoints documented)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
