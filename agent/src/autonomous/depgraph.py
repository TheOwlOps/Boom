"""depgraph — dependency analysis between Python modules in a directory.

On the execution path: the autonomous controller / surgery needs to know
which module depends on which before editing code across files (impact
assessment, circular import detection).

v1: stdlib only (ast + os), nothing to install.

Usage::

    from autonomous.depgraph import (
        build_graph, find_cycles, topological_order,
        dependents, impact_analysis, to_mermaid,
    )

    graph = build_graph("agent/src/autonomous")
    print(find_cycles(graph))          # [] when no circular imports
    print(topological_order(graph))    # safe order to load/edit
    print(impact_analysis(graph, ["planner.py"]))  # editing planner.py affects whom
    print(to_mermaid(graph))           # paste into mermaid.live to draw

Module naming: path relative to root_dir, minus the .py suffix,
"/" becomes ".". Example: ``a.py`` -> ``"a"``,
``pkg/mod.py`` -> ``"pkg.mod"``, ``pkg/__init__.py`` -> ``"pkg"``.
Only INTERNAL imports count (modules existing in root_dir); stdlib or
third-party imports (os, sys, httpx, ...) are ignored.
"""

import ast
import os
from collections import deque
from typing import Dict, List, Set


# ---------------------------------------------------------------------------
# File scanning and import parsing
# ---------------------------------------------------------------------------

def _module_name(root_dir: str, file_path: str) -> str:
    """Converts a file path to a dotted module name.

    Example with root_dir=/x: /x/a.py -> "a", /x/pkg/m.py -> "pkg.m",
    /x/pkg/__init__.py -> "pkg". Returns "" when undeterminable.
    """
    rel = os.path.relpath(file_path, root_dir)
    if not rel.endswith(".py"):
        return ""
    name = rel[:-3].replace(os.sep, ".")
    if name == "__init__":
        # __init__.py right at the root: use the directory name as module name
        name = os.path.basename(os.path.abspath(root_dir)) or "__root__"
    elif name.endswith(".__init__"):
        name = name[: -len(".__init__")]
    return name


def _collect_modules(root_dir: str) -> Dict[str, str]:
    """Recursively scans root_dir, returns {module name: file path}."""
    modules: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root_dir):
        # Skip hidden dirs (.git, .venv, ...) to reduce noise
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
    """Resolves a relative import (from . / .. import ...) to a dotted name.

    Returns None when level goes above the package root (broken import, skipped).
    """
    # The package containing the current module: itself when __init__.py,
    # otherwise the part before the last ".".
    if is_package:
        package = current_module
    else:
        package = current_module.rpartition(".")[0]
    parts = package.split(".") if package else []
    up = level - 1  # level=1 -> stay in the current package
    if up > len(parts):
        return None
    base = parts[: len(parts) - up]
    if modname:
        base = base + modname.split(".")
    return ".".join(base) if base else None


def _imports_of_file(path: str, current_module: str, is_package: bool,
                     internal: Set[str]) -> List[str]:
    """Parses one file's AST, returns the INTERNAL modules it imports.

    Unreadable/syntax-broken files -> [] (never crashes the whole graph).
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
                    # from pkg import mod: prefer submodule "pkg.mod" when it
                    # exists internally, otherwise fall back to "pkg".
                    full = base + "." + alias.name if base else alias.name
                    if full in internal:
                        candidates.append(full)
                    elif base and base in internal:
                        candidates.append(base)
                    else:
                        candidates.append(full)  # external import: filtered later

    # Keep internal imports only, drop self-imports, keep appearance order, dedupe
    result: List[str] = []
    seen: Set[str] = set()
    for cand in candidates:
        if cand in internal and cand != current_module and cand not in seen:
            seen.add(cand)
            result.append(cand)
    return result


def build_graph(root_dir: str) -> Dict[str, List[str]]:
    """Scans root_dir, returns {module: [modules it imports directly]}.

    Keys include EVERY .py module found (even ones importing nothing ->
    empty value). Only internal imports within root_dir count.
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
# Circular import detection
# ---------------------------------------------------------------------------

def _canonical_cycle(cycle: List[str]):
    """Normalizes a cycle: rotates so the smallest element leads, returns a tuple.

    So the same loop discovered from different start points counts once.
    """
    i = cycle.index(min(cycle))
    return tuple(cycle[i:] + cycle[:i])


def find_cycles(graph: Dict[str, List[str]]) -> List[List[str]]:
    """Finds circular imports with colored DFS (white/gray/black).

    Returns a list of cycles, each a module list in import order
    (a -> b -> c -> a is represented as ["a", "b", "c"]). No cycles -> [].
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
                continue  # imported module not in the graph: skip
            if color[dep] == GRAY:
                # Hit an in-progress node -> pull the cycle out of the stack
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
# Topological order, reverse lookup, impact analysis
# ---------------------------------------------------------------------------

def topological_order(graph: Dict[str, List[str]]) -> List[str]:
    """Orders modules so dependencies come before dependents (Kahn).

    Raises ValueError when the graph has a cycle (lists the stuck modules).
    """
    in_degree = {m: 0 for m in graph}      # each node's (internal) dependency count
    rdeps: Dict[str, List[str]] = {m: [] for m in graph}  # dep -> who imports it
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
            "Circular import detected, cannot sort topologically. "
            "Stuck modules: %s" % ", ".join(stuck))
    return order


def dependents(graph: Dict[str, List[str]], module: str) -> List[str]:
    """Who directly imports this module? (reverse lookup, sorted A-Z)."""
    return sorted(m for m, deps in graph.items() if module in deps)


def _normalize_module_name(name: str) -> str:
    """Accepts module names ("pkg.m") and file paths ("pkg/m.py") alike."""
    name = name.strip().replace(os.sep, "/").replace("\\", "/")
    if name.endswith(".py"):
        name = name[:-3]
    name = name.replace("/", ".")
    if name.endswith(".__init__"):
        name = name[: -len(".__init__")]
    return name


def impact_analysis(graph: Dict[str, List[str]],
                    changed_files) -> List[str]:
    """Editing these files/modules affects which modules?

    BFS over the reverse graph: from each edited module, spreads to every
    module (direct/indirect) importing it. Returns a sorted A-Z list,
    including the edited modules themselves (when in the graph).
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
# Mermaid export
# ---------------------------------------------------------------------------

def _mermaid_id(module: str) -> str:
    """Mermaid node ID: no dots/dashes, to be safe."""
    return module.replace(".", "_").replace("-", "_")


def to_mermaid(graph: Dict[str, List[str]], direction: str = "TD") -> str:
    """Exports the graph as a Mermaid flowchart (paste into mermaid.live).

    direction: "TD" (top-down) or "LR" (left-right).
    """
    lines = ["graph %s" % direction]
    for module in sorted(graph):
        lines.append('    %s["%s"]' % (_mermaid_id(module), module))
    for module in sorted(graph):
        for dep in sorted(set(graph[module])):
            lines.append("    %s --> %s" % (_mermaid_id(module),
                                            _mermaid_id(dep)))
    return "\n".join(lines) + "\n"
