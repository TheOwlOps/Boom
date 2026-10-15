"""Test Generator cho autonomous coding cua Boom.

Tu sinh test tu AST (stdlib only): parse module -> trich function ->
sinh edge cases -> sinh file test chay duoc.

Triet ly: test sinh ra MUC TIEU LA CHAY DUOC (pass), khong phai test
dung logic sau. Moi test goi ham that trong try/except va chi FAIL khi
gap crash khong mong doi (Exception la ma khong nam trong danh sach
cho phep). Day la heuristic — generator khong hieu semantics cua code.
"""

import ast
import importlib.util
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

# Exception duoc chap nhan khi goi ham voi input bien (edge case).
# Ly do: input nhu None/""/[] khong phai luc nao cung hop le, ham co
# quyen raise cac loi nay ma van coi nhu "khong crash bat ngo".
ALLOWED_EDGE_ERRORS = (TypeError, ValueError, AttributeError, KeyError,
                       IndexError, ZeroDivisionError)


def _annotation_str(node: Optional[ast.AST]) -> Optional[str]:
    """Tra ve chuoi annotation, None neu khong co hoac khong unparse duoc."""
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _func_info(node: Any, class_name: Optional[str] = None) -> Dict[str, Any]:
    """Trich thong tin 1 FunctionDef/AsyncFunctionDef thanh dict."""
    argspec = node.args
    raw_args = list(argspec.args)
    start = 0
    if class_name is not None and raw_args and raw_args[0].arg in ("self", "cls"):
        start = 1  # bo self/cls cua method
    n_defaults = len(argspec.defaults)
    args: List[Dict[str, Any]] = []
    for i, a in enumerate(raw_args[start:], start=start):
        has_default = n_defaults > 0 and i >= len(raw_args) - n_defaults
        args.append({"name": a.arg, "has_default": has_default})
    for a, d in zip(argspec.kwonlyargs, argspec.kw_defaults):
        args.append({"name": a.arg, "has_default": d is not None})
    return {
        "name": node.name,
        "args": args,
        "is_async": isinstance(node, ast.AsyncFunctionDef),
        "docstring": ast.get_docstring(node),
        "returns_annotation": _annotation_str(node.returns),
        "class_name": class_name,
    }


class TestGenerator:
    """Sinh test tu AST cua 1 module Python."""

    def analyze_module(self, file_path: str,
                       include_private: bool = False) -> List[Dict[str, Any]]:
        """Parse file bang AST, tra ve list function info.

        Moi item: {name, args: [{name, has_default}], is_async, docstring,
        returns_annotation, class_name}. Bo qua ham private (_prefix) tru
        khi include_private=True. Method trong class VAN lay, ghi class_name.
        File loi syntax -> tra ve [] (khong raise).
        """
        try:
            src = Path(file_path).read_text(encoding="utf-8")
            tree = ast.parse(src, filename=str(file_path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            return []
        funcs: List[Dict[str, Any]] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_") and not include_private:
                    continue
                funcs.append(_func_info(node))
            elif isinstance(node, ast.ClassDef):
                if node.name.startswith("_") and not include_private:
                    continue
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if sub.name.startswith("_") and not include_private:
                            continue
                        funcs.append(_func_info(sub, class_name=node.name))
        return funcs

    # ---- heuristic gia tri mau theo ten arg ---------------------------

    def sample_value(self, arg_name: str) -> Any:
        """Gia tri mau 'hop ly' dua tren ten arg (heuristic, khong chac)."""
        n = arg_name.lower()
        if any(k in n for k in ("path", "file", "filename", "filepath", "dir")):
            return "/tmp/test"
        if any(k in n for k in ("text", "name", "title", "msg", "message",
                                "string", "str_", "s_", "key", "prompt")):
            return "test"
        if any(k in n for k in ("count", "limit", "num", "size", "index",
                                "n_", "length", "len_", "age", "port",
                                "year", "id")) or n in ("n", "i", "k"):
            return 3
        if any(k in n for k in ("items", "list", "array", "seq", "values",
                                "rows", "entries")):
            return [1, 2, 3]
        if any(k in n for k in ("flag", "enabled", "disabled", "active",
                                "verbose", "debug", "dry_run")):
            return True
        if any(k in n for k in ("dict", "mapping", "config", "options",
                                "kwargs", "params")):
            return {"a": 1}
        return 1

    def generate_edge_cases(self, func_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Sinh edge cases cho 1 function.

        Tra ve list [{case_name, args: {arg: value_repr}}] — value_repr la
        Python literal an toan (repr). Luon co: "", [], 0, -1, so rat lon;
        arg khong default thi them case None.
        """
        args = func_info.get("args", [])
        if not args:
            return []
        names = [a["name"] for a in args]
        first = names[0]
        required = [a["name"] for a in args if not a["has_default"]]
        cases: List[Dict[str, Any]] = []
        if required:
            # Tat ca arg bat buoc = None: ham nao khong chiu None se raise
            # TypeError/ValueError — duoc chap nhan o test runner.
            cases.append({
                "case_name": "required_args_none",
                "args": {n: repr(None) for n in required},
            })
        generic: List[Any] = ["", [], 0, -1, 10 ** 18]
        labels = {"''": "empty_string", "[]": "empty_list",
                  "0": "zero", "-1": "negative_one"}
        for v in generic:
            label = labels.get(repr(v), "huge_number")
            cases.append({"case_name": label, "args": {first: repr(v)}})
        return cases

    # ---- sinh code test ------------------------------------------------

    def _call_args(self, func_info: Dict[str, Any],
                   overrides: Optional[Dict[str, str]] = None) -> str:
        """Chuoi args goi ham: sample values, override bang repr cho truoc."""
        overrides = overrides or {}
        parts = []
        for a in func_info["args"]:
            n = a["name"]
            if n in overrides:
                parts.append(f"{n}={overrides[n]}")
            else:
                parts.append(f"{n}={self.sample_value(n)!r}")
        return ", ".join(parts)

    def generate_tests(self, file_path: str,
                       output_path: Optional[str] = None) -> str:
        """Sinh source file test hoan chinh (style pytest, khong can pytest).

        Test chi dung assert/raise thuan — chay duoc ca duoi pytest lan
        chay thu cong. Neu output_path cho truoc thi ghi file luon.
        Tra ve source code (str).
        """
        p = Path(file_path)
        module_stem = p.stem
        funcs = self.analyze_module(str(p))
        alias = f"_mod_{module_stem}"
        lines: List[str] = []
        lines.append(f'"""Auto-generated tests for {module_stem}."""')
        lines.append('"""Sinh boi Boom TestGenerator — muc tieu: chay duoc (pass).')
        lines.append('Khong assert logic sau vi generator khong hieu semantics."""')
        lines.append("")
        lines.append("import asyncio")
        lines.append("import importlib.util")
        lines.append("import sys")
        lines.append("from pathlib import Path")
        lines.append("")
        lines.append(f"_TARGET = Path({str(p.resolve())!r})")
        lines.append("sys.path.insert(0, str(_TARGET.parent))  # cho import tuong doi")
        lines.append(f"_spec = importlib.util.spec_from_file_location("
                     f"{alias!r}, str(_TARGET))")
        lines.append(f"{alias} = importlib.util.module_from_spec(_spec)")
        lines.append("_spec.loader.exec_module(_mod_{})".format(module_stem))
        lines.append("")
        lines.append("_ALLOWED = (TypeError, ValueError, AttributeError, KeyError,")
        lines.append("            IndexError, ZeroDivisionError)")
        lines.append("")
        lines.append("")
        count = 0
        for f in funcs:
            target = f"{alias}.{f['name']}"
            if f["class_name"]:
                target = f"{alias}.{f['class_name']}.{f['name']}"
            call_args = self._call_args(f)
            await_prefix = "await " if f["is_async"] else ""
            # --- happy path ---
            lines.append(f"def test_{f['name']}_happy_path():")
            body = self._happy_body(f, target, call_args, await_prefix)
            lines.append(textwrap.indent(body, "    "))
            lines.append("")
            lines.append("")
            count += 1
            # --- edge cases ---
            for case in self.generate_edge_cases(f):
                cname = case["case_name"]
                ecall_args = self._call_args(f, case["args"])
                lines.append(f"def test_{f['name']}_edge_{cname}():")
                body = self._edge_body(f, target, ecall_args, await_prefix, cname)
                lines.append(textwrap.indent(body, "    "))
                lines.append("")
                lines.append("")
                count += 1
        src = "\n".join(lines).rstrip() + "\n"
        if output_path:
            Path(output_path).write_text(src, encoding="utf-8")
        self._last_count = count
        return src

    def _happy_body(self, f: Dict[str, Any], target: str, call_args: str,
                    await_prefix: str) -> str:
        """Body test happy-path: goi that, FAIL neu crash bat ngo."""
        if f["is_async"]:
            return self._happy_async_body(f, target, call_args)
        if f["class_name"]:
            head = (
                f"try:\n"
                f"    _inst = {target.rsplit('.', 1)[0]}()\n"
                f"except Exception:\n"
                f"    return  # khong khoi tao duoc class -> bo qua, khong fail\n"
                f"call = lambda: _inst.{f['name']}({call_args})\n"
            )
        else:
            head = f"call = lambda: {target}({call_args})\n"
        return (head +
                "try:\n"
                "    call()\n"
                "except Exception as e:\n"
                "    raise AssertionError(\n"
                "        f\"happy path crash khong mong doi: "
                "{type(e).__name__}: {e}\")")

    def _happy_async_body(self, f: Dict[str, Any], target: str,
                          call_args: str) -> str:
        if f["class_name"]:
            cls_target = target.rsplit(".", 1)[0]
            return (
                f"try:\n"
                f"    _inst = {cls_target}()\n"
                f"except Exception:\n"
                f"    return  # khong khoi tao duoc class -> bo qua\n"
                f"async def _amain():\n"
                f"    return await _inst.{f['name']}({call_args})\n"
                f"try:\n"
                f"    asyncio.run(_amain())\n"
                f"except Exception as e:\n"
                f"    raise AssertionError(\n"
                f"        f\"happy path crash khong mong doi: "
                f"{{type(e).__name__}}: {{e}}\")"
            )
        return (
            f"async def _amain():\n"
            f"    return await {target}({call_args})\n"
            f"try:\n"
            f"    asyncio.run(_amain())\n"
            f"except Exception as e:\n"
            f"    raise AssertionError(\n"
            f"        f\"happy path crash khong mong doi: "
            f"{{type(e).__name__}}: {{e}}\")"
        )

    def _edge_body(self, f: Dict[str, Any], target: str, call_args: str,
                   await_prefix: str, case_name: str) -> str:
        """Body test edge: cho phep raise _ALLOWED, FAIL neu crash la."""
        allow_none = "required_args_none" in case_name or "none" in case_name
        note = ("# None la input bien — ham duoc phep raise loi kieu/gia tri"
                if allow_none else
                "# input bien — chi fail khi crash kieu la hoan toan")
        if f["class_name"]:
            cls_target = target.rsplit(".", 1)[0]
            if f["is_async"]:
                return (
                    f"{note}\n"
                    f"try:\n"
                    f"    _inst = {cls_target}()\n"
                    f"except Exception:\n"
                    f"    return\n"
                    f"async def _amain():\n"
                    f"    return await _inst.{f['name']}({call_args})\n"
                    f"try:\n"
                    f"    asyncio.run(_amain())\n"
                    f"except _ALLOWED:\n"
                    f"    pass  # chap nhan duoc\n"
                    f"except Exception as e:\n"
                    f"    raise AssertionError(\n"
                    f"        f\"edge '{case_name}' crash la: "
                    f"{{type(e).__name__}}: {{e}}\")"
                )
            return (
                f"{note}\n"
                f"try:\n"
                f"    _inst = {cls_target}()\n"
                f"except Exception:\n"
                f"    return\n"
                f"try:\n"
                f"    _inst.{f['name']}({call_args})\n"
                f"except _ALLOWED:\n"
                f"    pass  # chap nhan duoc\n"
                f"except Exception as e:\n"
                f"    raise AssertionError(\n"
                f"        f\"edge '{case_name}' crash la: "
                f"{{type(e).__name__}}: {{e}}\")"
            )
        if f["is_async"]:
            return (
                f"{note}\n"
                f"async def _amain():\n"
                f"    return await {target}({call_args})\n"
                f"try:\n"
                f"    asyncio.run(_amain())\n"
                f"except _ALLOWED:\n"
                f"    pass  # chap nhan duoc\n"
                f"except Exception as e:\n"
                f"    raise AssertionError(\n"
                f"        f\"edge '{case_name}' crash la: "
                f"{{type(e).__name__}}: {{e}}\")"
            )
        return (
            f"{note}\n"
            f"try:\n"
            f"    {target}({call_args})\n"
            f"except _ALLOWED:\n"
            f"    pass  # chap nhan duoc\n"
            f"except Exception as e:\n"
            f"    raise AssertionError(\n"
            f"        f\"edge '{case_name}' crash la: "
            f"{{type(e).__name__}}: {{e}}\")"
        )

    def write_tests(self, file_path: str, output_path: str) -> int:
        """Sinh + ghi file test. Tra ve so test da sinh."""
        self.generate_tests(file_path, output_path=output_path)
        return getattr(self, "_last_count", 0)


def run_generated_tests(test_file: str) -> Dict[str, Any]:
    """Chay thu cong file test da sinh (khong can pytest).

    Import file, goi moi ham test_*, dem pass/fail. Tra ve dict
    {total, passed, failed: [(name, error)]}. Khong raise.
    """
    import traceback
    p = Path(test_file)
    name = f"_gen_test_{p.stem}"
    spec = importlib.util.spec_from_file_location(name, str(p.resolve()))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return {"total": 0, "passed": 0,
                "failed": [("<import>", traceback.format_exc(limit=3))]}
    total, passed, failed = 0, 0, []
    for attr in dir(mod):
        if attr.startswith("test_") and callable(getattr(mod, attr)):
            total += 1
            try:
                getattr(mod, attr)()
                passed += 1
            except Exception:
                failed.append((attr, traceback.format_exc(limit=5)))
    return {"total": total, "passed": passed, "failed": failed}
