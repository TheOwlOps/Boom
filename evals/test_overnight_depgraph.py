"""Test cho depgraph.py — chạy: python3 test_depgraph.py

Dựng 8 file mẫu trong thư mục tạm:
  - a -> b -> c -> a : 1 cycle (3 module)
  - d : không import nội bộ (có import os/sys để test lọc import ngoài)
  - e -> d : acyclic
  - pkg/__init__.py, pkg/m1.py -> pkg.m2 (from . import m2), pkg/m2.py : acyclic
Kiểm tra: find_cycles bắt đúng cycle, topological_order raise khi có cycle,
impact_analysis lan đúng trên reverse graph, to_mermaid xuất đúng format.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_sys_src = os.path.join(_REPO, "agent", "src")
if _sys_src not in sys.path:
    sys.path.insert(0, _sys_src)


from autonomous.depgraph import (  # noqa: E402
    build_graph, find_cycles, topological_order,
    dependents, impact_analysis, to_mermaid,
)

PASS = 0


def check(cond, msg):
    """1 assertion: fail thì raise AssertionError kèm message."""
    global PASS
    assert cond, "FAIL: " + msg
    PASS += 1
    print("ok -", msg)


def expect_value_error(fn, msg):
    """1 assertion: fn() phải raise ValueError."""
    global PASS
    try:
        fn()
    except ValueError:
        PASS += 1
        print("ok -", msg)
        return
    raise AssertionError("FAIL (không raise ValueError): " + msg)


tmp = tempfile.mkdtemp(prefix="depgraph_test_")
try:
    # ---- Dựng file mẫu ----
    sample_files = {
        "a.py": "import b\n",
        "b.py": "import c\n",
        "c.py": "import a\n",
        "d.py": "import os\nimport sys\nX = 1\n",
        "e.py": "import d\n",
        os.path.join("pkg", "__init__.py"): "",
        os.path.join("pkg", "m1.py"): "from . import m2\n",
        os.path.join("pkg", "m2.py"): "Y = 2\n",
    }
    for rel, content in sample_files.items():
        path = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    graph = build_graph(tmp)

    # ---- build_graph: 8 assertions ----
    check(len(graph) == 8, "build_graph quét đủ 8 module")
    check(graph["a"] == ["b"], "a import b")
    check(graph["b"] == ["c"], "b import c")
    check(graph["c"] == ["a"], "c import a (khép cycle)")
    check(graph["d"] == [], "d không có import nội bộ (os/sys bị lọc)")
    check(graph["e"] == ["d"], "e import d")
    check(graph["pkg.m1"] == ["pkg.m2"],
          "relative import 'from . import m2' giải đúng thành pkg.m2")
    check(graph["pkg.m2"] == [], "pkg.m2 không import ai")

    # ---- find_cycles: 4 assertions ----
    cycles = find_cycles(graph)
    check(len(cycles) == 1, "find_cycles bắt đúng 1 cycle")
    check(len(cycles[0]) == 3, "cycle dài đúng 3 module")
    check(set(cycles[0]) == {"a", "b", "c"},
          "cycle gồm đúng {a, b, c}")
    check(find_cycles({}) == [], "graph rỗng -> không có cycle")

    # ---- topological_order: 4 assertions ----
    expect_value_error(lambda: topological_order(graph),
                       "topological_order raise ValueError khi có cycle")
    dag = {"d": [], "e": ["d"], "x": []}
    order = topological_order(dag)
    check(set(order) == {"d", "e", "x"},
          "topological_order giữ đủ module của DAG")
    check(order.index("d") < order.index("e"),
          "topological_order: dependency d đứng trước e")
    check(topological_order({}) == [], "graph rỗng -> order rỗng")

    # ---- dependents: 4 assertions ----
    check(dependents(graph, "b") == ["a"], "dependents(b) == [a]")
    check(dependents(graph, "a") == ["c"], "dependents(a) == [c]")
    check(dependents(graph, "d") == ["e"], "dependents(d) == [e]")
    check(dependents(graph, "khong_ton_tai") == [],
          "dependents(module lạ) == []")

    # ---- impact_analysis: 5 assertions ----
    check(impact_analysis(graph, ["d"]) == ["d", "e"],
          "sửa d ảnh hưởng {d, e} (e import d)")
    check(impact_analysis(graph, ["e"]) == ["e"],
          "sửa e chỉ ảnh hưởng chính e")
    check(set(impact_analysis(graph, ["a"])) == {"a", "b", "c"},
          "sửa a lan khắp cycle {a, b, c}")
    check(impact_analysis(graph, ["e.py"]) == ["e"],
          "impact_analysis chấp nhận đường dẫn file 'e.py'")
    check(impact_analysis(graph, ["pkg/m2.py"]) == ["pkg.m1", "pkg.m2"],
          "impact_analysis chấp nhận path 'pkg/m2.py' -> lan tới pkg.m1")

    # ---- to_mermaid: 4 assertions ----
    mm = to_mermaid(graph)
    check("graph TD" in mm, "to_mermaid bắt đầu bằng 'graph TD'")
    check("a --> b" in mm, "to_mermaid có cạnh a --> b")
    check("pkg_m1 --> pkg_m2" in mm,
          "to_mermaid sanitize tên dotted thành pkg_m1 --> pkg_m2")
    check('d["d"]' in mm,
          "to_mermaid vẫn liệt kê node d dù không có cạnh")

    print("\nTẤT CẢ %d ASSERTIONS PASS" % PASS)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
