---
name: perf-optimizer
description: "Analyze performance bottlenecks with AST: nested O(n²) loops, I/O in loops, += string concatenation, N+1 queries, missing cache."
version: 1.0.0
author: Boom Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [performance, optimization, bottleneck, ast, profiling-static]
    related_skills: [code-review]
---

# Perf Optimizer

Statically analyze Python code for performance bottlenecks using AST (stdlib),
each finding accompanied by optimized sample code using the variable names from
the original code.

## When to use

- Code runs slowly, need to find the bottleneck before optimizing
- Performance review before merging code that handles large data
- When the user says "optimize this code", "find the bottleneck", "code is too slow"

**Not for:** runtime profiling (use `cProfile` for real numbers);
non-Python languages. The skill finds *bad code patterns*, it doesn't measure runtime.

## Input

- **Path:** 1 `.py` file or 1 directory (recursive scan)

## Output

List of suggestions, each with this schema:

```json
[
  {
    "file": "agent/src/process.py",
    "line": 30,
    "pattern": "nested_loop",
    "severity": "medium",
    "problem": "2-level nested loop — O(n^2) complexity...",
    "optimized_code": "lookup = set(ys)  # or dict if you need to map values\nfor x in xs:\n    if x in lookup:  # O(1) instead of a nested loop\n        ..."
  }
]
```

| Pattern | Severity | Meaning |
|---------|----------|---------|
| `nested_loop` | medium/high | Nested loops of O(n²) or worse → use set/dict for O(1) lookup |
| `io_in_loop` | high | Repeated I/O inside a loop (open, requests, socket) → batch/hoist |
| `string_concat` | medium | `+=` string concatenation in a loop → collect in a list then `"".join()` |
| `n_plus_one` | high | DB query inside a loop → 1 query with `IN` / `executemany` |
| `missing_cache` | medium | Expensive function called repeatedly in a loop → `@lru_cache` |
| `list_membership` | low | `x in list` inside a loop → switch to `set` |

## Example

### CLI

```bash
# Analyze 1 file, print markdown
python3 skills/perf-optimizer/optimizer.py agent/src/process.py

# Analyze a whole directory, print JSON
python3 skills/perf-optimizer/optimizer.py agent/src/ --json
```

### Python API

```python
from skills.perf_optimizer.optimizer import optimize_source, to_markdown

code = """
for user in users:
    for order in orders:
        if order.user_id == user.id:
            print(order)
"""
suggestions = optimize_source(code, filename="demo.py")
for s in suggestions:
    print(s.pattern, s.file, s.line)
    print(s.problem)
    print(s.optimized_code)
```

## Pitfalls

- Static heuristic: may report false positives (e.g. small nested loops, small data
  not worth optimizing) — consider the actual data size
- `missing_cache` only suggests `@lru_cache` for user-defined functions containing
  a loop; functions with side effects must NOT be cached — verify manually
- Always re-measure with `cProfile`/benchmarks after applying suggestions
