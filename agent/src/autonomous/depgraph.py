"""depgraph — phân tích dependency giữa các module Python trong một thư mục.

Nằm trên đường chạy: autonomous controller / surgery cần biết module nào
phụ thuộc module nào trước khi sửa code nhiều file (đánh giá impact, phát
hiện circular import).

v1: stdlib only (ast + os), KHÔNG cần cài thêm gì.

Cách dùng::

    from autonomous.depgraph import (
        build_graph, find_cycles, topological_order,
        dependents, impact_analysis, to_mermaid,
    )

    graph = build_graph("agent/src/autonomous")
    print(find_cycles(graph))          # [] nếu không có circular import
    print(topological_order(graph))    # thứ tự an toàn để load/sửa
    print(impact_analysis(graph, ["planner.py"]))  # sửa planner.py ảnh hưởng ai
    print(to_mermaid(graph))           # dán vào mermaid.live để vẽ sơ đồ

Quy ước tên module: đường dẫn tương đối so với root_dir, bỏ đuôi .py,
dấu "/" thành ".". Ví dụ: ``a.py`` -> ``"a"``,
``pkg/mod.py`` -> ``"pkg.mod"``, ``pkg/__init__.py`` -> ``"pkg"``.
Chỉ tính import NỘI BỘ (module tồn tại trong root_dir); import stdlib
hay thư viện ngoài (os, sys, httpx, ...) bị bỏ qua.
"""

import ast
import os
from collections import deque
from typing import Dict, List, Set


# ---------------------------------------------------------------------------
# Quét file và parse import
# ---------------------------------------------------------------------------

def _module_name(root_dir: str, file_path: str) -> str:
    """Đổi đường dẫn file thành tên module dạng dotted.

    Ví dụ với root_dir=/x: /x/a.py -> "a", /x/pkg/m.py -> "pkg.m",
    /x/pkg/__init__.py -> "pkg". Trả về "" nếu không xác định được.
    """
    rel = os.path.relpath(file_path, root_dir)
    if not rel.endswith(".py"):
        return ""
    name = rel[:-3].replace(os.sep, ".")
    if name == "__init__":
        # __init__.py ngay tại root: lấy tên thư mục làm tên module
        name = os.path.basename(os.path.abspath(root_dir)) or "__root__"
    elif name.endswith(".__init__"):
        name = name[: -len(".__init__")]
    return name


def _collect_modules(root_dir: str) -> Dict[str, str]:
    """Quét đệ quy root_dir, trả về {tên module: đường dẫn file}."""
    modules: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root_dir):
        # Bỏ thư mục ẩn (.git, .venv, ...) cho đỡ ồn
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            mod = _module_name(root_dir, path)
            if mod:
                modules[mod] = path
    return modules


def _resolve_relative(current_module: str, is_package: bool,
                      level: int, modname: str):
    """Giải quyết import tương đối (from . / .. import ...) thành tên dotted.

    Trả về None nếu level vượt quá gốc package (import lỗi, bỏ qua).
    """
    # Package chứa module hiện tại: nếu là __init__.py thì chính nó,
    # ngược lại là phần trước dấu "." cuối cùng.
    if is_package:
        package = current_module
    else:
        package = current_module.rpartition(".")[0]
    parts = package.split(".") if package else []
    up = level - 1  # level=1 -> ở yên package hiện tại
    if up > len(parts):
        return None
    base = parts[: len(parts) - up]
    if modname:
        base = base + modname.split(".")
    return ".".join(base) if base else None


def _imports_of_file(path: str, current_module: str, is_package: bool,
                     internal: Set[str]) -> List[str]:
    """Parse AST của 1 file, trả về list module NỘI BỘ mà nó import.

    File lỗi cú pháp / không đọc được -> trả về [] (không crash cả graph).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []

    candidates: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # import a, import pkg.mod
            for alias in node.names:
                candidates.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            mod = node.module or ""
            if level == 0:
                base = mod
            else:
                base = _resolve_relative(current_module, is_package,
                                         level, mod)
                if base is None:
                    continue
            for alias in node.names:
                if alias.name == "*":
                    if base:
                        candidates.append(base)
                else:
                    # from pkg import mod: ưu tiên submodule "pkg.mod" nếu nó
                    # tồn tại nội bộ, ngược lại fallback về "pkg".
                    full = base + "." + alias.name if base else alias.name
                    if full in internal:
                        candidates.append(full)
                    elif base and base in internal:
                        candidates.append(base)
                    else:
                        candidates.append(full)  # import ngoài: bị lọc sau

    # Chỉ giữ import nội bộ, bỏ self-import, giữ thứ tự xuất hiện, bỏ trùng
    result: List[str] = []
    seen: Set[str] = set()
    for cand in candidates:
        if cand in internal and cand != current_module and cand not in seen:
            seen.add(cand)
            result.append(cand)
    return result


def build_graph(root_dir: str) -> Dict[str, List[str]]:
    """Quét root_dir, trả về {module: [module mà nó import trực tiếp]}.

    Key bao gồm MỌI module .py tìm thấy (kể cả module không import ai ->
    value là []). Chỉ tính import nội bộ trong root_dir.
    """
    root_dir = os.path.abspath(root_dir)
    modules = _collect_modules(root_dir)
    internal = set(modules)
    graph: Dict[str, List[str]] = {}
    for mod, path in modules.items():
        is_package = os.path.basename(path) == "__init__.py"
        graph[mod] = _imports_of_file(path, mod, is_package, internal)
    return graph


# ---------------------------------------------------------------------------
# Phát hiện circular import
# ---------------------------------------------------------------------------

def _canonical_cycle(cycle: List[str]):
    """Chuẩn hoá cycle: xoay để phần tử nhỏ nhất đứng đầu, trả về tuple.

    Để cùng 1 vòng tròn phát hiện từ nhiều điểm xuất phát chỉ tính 1 lần.
    """
    i = cycle.index(min(cycle))
    return tuple(cycle[i:] + cycle[:i])


def find_cycles(graph: Dict[str, List[str]]) -> List[List[str]]:
    """Tìm circular imports bằng DFS tô màu (trắng/xám/đen).

    Trả về list các cycle, mỗi cycle là list module theo thứ tự import
    (a -> b -> c -> a được biểu diễn ["a", "b", "c"]). Không có cycle -> [].
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {m: WHITE for m in graph}
    stack: List[str] = []
    cycles: List[List[str]] = []
    seen: Set[tuple] = set()

    def dfs(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for dep in graph.get(node, []):
            if dep not in color:
                continue  # module import nhưng không có trong graph: bỏ qua
            if color[dep] == GRAY:
                # Gặp node đang dở dang -> lôi cycle từ stack ra
                idx = stack.index(dep)
                canon = _canonical_cycle(stack[idx:])
                if canon not in seen:
                    seen.add(canon)
                    cycles.append(list(canon))
            elif color[dep] == WHITE:
                dfs(dep)
        stack.pop()
        color[node] = BLACK

    for module in graph:
        if color[module] == WHITE:
            dfs(module)
    return cycles


# ---------------------------------------------------------------------------
# Sắp xếp topo, reverse lookup, impact analysis
# ---------------------------------------------------------------------------

def topological_order(graph: Dict[str, List[str]]) -> List[str]:
    """Sắp xếp module theo thứ tự: dependency đứng trước dependent (Kahn).

    Raise ValueError nếu graph có cycle (kèm danh sách module kẹt lại).
    """
    in_degree = {m: 0 for m in graph}      # số dependency (nội bộ) của mỗi node
    rdeps: Dict[str, List[str]] = {m: [] for m in graph}  # dep -> ai import nó
    for module, deps in graph.items():
        for dep in deps:
            if dep in in_degree:
                in_degree[module] += 1
                rdeps[dep].append(module)

    queue = deque(sorted(m for m, deg in in_degree.items() if deg == 0))
    order: List[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for dependent in sorted(rdeps[node]):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if len(order) != len(graph):
        stuck = sorted(m for m, deg in in_degree.items() if deg > 0)
        raise ValueError(
            "Phát hiện circular import, không thể sắp xếp topo. "
            "Module kẹt lại: %s" % ", ".join(stuck))
    return order


def dependents(graph: Dict[str, List[str]], module: str) -> List[str]:
    """Ai import trực tiếp module này? (reverse lookup, sắp xếp A-Z)."""
    return sorted(m for m, deps in graph.items() if module in deps)


def _normalize_module_name(name: str) -> str:
    """Chấp nhận cả tên module ("pkg.m") lẫn đường dẫn file ("pkg/m.py")."""
    name = name.strip().replace(os.sep, "/").replace("\\", "/")
    if name.endswith(".py"):
        name = name[:-3]
    name = name.replace("/", ".")
    if name.endswith(".__init__"):
        name = name[: -len(".__init__")]
    return name


def impact_analysis(graph: Dict[str, List[str]],
                    changed_files) -> List[str]:
    """Sửa các file/module này thì những module nào bị ảnh hưởng?

    BFS trên reverse graph: từ module bị sửa, lan ra tất cả module
    (trực tiếp/gián tiếp) import nó. Trả về list sắp xếp A-Z, bao gồm
    chính các module bị sửa (nếu có trong graph).
    """
    changed = {_normalize_module_name(str(f)) for f in (changed_files or [])}
    affected: Set[str] = set()
    queue = deque(m for m in changed if m in graph)
    affected.update(queue)
    while queue:
        current = queue.popleft()
        for dependent in dependents(graph, current):
            if dependent not in affected:
                affected.add(dependent)
                queue.append(dependent)
    return sorted(affected)


# ---------------------------------------------------------------------------
# Xuất Mermaid
# ---------------------------------------------------------------------------

def _mermaid_id(module: str) -> str:
    """ID node Mermaid: không dấu chấm/gạch ngang cho an toàn."""
    return module.replace(".", "_").replace("-", "_")


def to_mermaid(graph: Dict[str, List[str]], direction: str = "TD") -> str:
    """Xuất graph thành Mermaid flowchart (dán vào mermaid.live để vẽ).

    direction: "TD" (trên-xuống) hoặc "LR" (trái-phải).
    """
    lines = ["graph %s" % direction]
    for module in sorted(graph):
        lines.append('    %s["%s"]' % (_mermaid_id(module), module))
    for module in sorted(graph):
        for dep in sorted(set(graph[module])):
            lines.append("    %s --> %s" % (_mermaid_id(module),
                                            _mermaid_id(dep)))
    return "\n".join(lines) + "\n"
