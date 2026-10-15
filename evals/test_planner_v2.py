"""Test cho planner_v2.RepoAwarePlanner — chạy THẬT trên repo /tmp/boom_git.

Yêu cầu: python3 test_planner_v2.py (không cần cài thêm gì ngoài pydantic).
"""

import json
import os
import sys

# Nạp planner_v2 từ thư mục output của worker
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/tmp/boom_git/agent/src")

from autonomous.planner_v2 import RepoAwarePlanner

REPO = "/tmp/boom_git"

# Build graph 1 lần cho tất cả test (quét ~671 file .py trong repo)
planner = RepoAwarePlanner(REPO)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    """Ghi nhận 1 assertion: PASS/FAIL kèm chi tiết."""
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS: %s" % name)
    else:
        FAIL += 1
        print("  FAIL: %s %s" % (name, detail))


print("== RepoAwarePlanner trên %s ==" % REPO)
print("   graph: %d module" % len(planner.graph))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_dependents(plan):
    """Plan cho endpoint có chứa ít nhất 1 dependent của gateway/api.py?"""
    deps = planner._depgraph.dependents(planner.graph, "gateway.api")
    dep_files = {planner.mod_to_path[m] for m in deps if m in planner.mod_to_path}
    return bool(dep_files & set(plan["files_affected"]))


def _edit_steps_in_topo_order(pl, plan):
    """Các step edit có đi theo thứ tự topo không (index không giảm)?"""
    idx = []
    for s in plan["steps"]:
        if s["action"] == "edit" and s["files"]:
            mod = s["files"][0][:-3].replace("/", ".")
            idx.append(pl._topo_index.get(mod, 10 ** 9))
    return idx == sorted(idx) and len(idx) > 0


def _impact_matches_depgraph(pl, plan):
    """files_affected có chứa đúng tập impact_analysis tính trực tiếp?"""
    dg = pl._depgraph
    primaries = [s["files"][0] for s in plan["steps"]
                 if s["action"] == "edit" and s["files"]]
    mods = [p[:-3].replace("/", ".") for p in primaries
            if p[:-3].replace("/", ".") in pl.graph]
    expected = {pl.mod_to_path[m] for m in dg.impact_analysis(pl.graph, mods)
                if m in pl.mod_to_path}
    return expected <= set(plan["files_affected"])


# ---------------------------------------------------------------- 1-3: endpoint
p_api = planner.plan("thêm endpoint /api/users")

check("1. plan('thêm endpoint /api/users') tìm đúng gateway/api.py",
      any(f == "gateway/api.py" for f in p_api["files_affected"]),
      "files_affected không chứa gateway/api.py: %s" % p_api["files_affected"][:8])

check("2. files_affected chứa dependents của gateway/api.py từ depgraph",
      _has_dependents(p_api),
      "không thấy dependent nào")

check("3. steps có action/edit/test và mỗi step đều có reason giải thích",
      all(s.get("action") and s.get("reason", "").strip() for s in p_api["steps"])
      and {s["action"] for s in p_api["steps"]} >= {"survey", "edit", "test"},
      "steps=%s" % [s["action"] for s in p_api["steps"]])

# ---------------------------------------------------------------- 4-6: tools
p_tools = planner.plan("sửa bug tools")

check("4. plan('sửa bug tools') tìm đúng agent/src/tools/",
      any(f.startswith("agent/src/tools/") for f in p_tools["files_affected"]),
      "files_affected=%s" % p_tools["files_affected"][:8])

check("5. task_type của 'sửa bug tools' là bugfix",
      p_tools["task_type"] == "bugfix",
      "got %r" % p_tools["task_type"])

check("6. estimated_complexity nằm trong {low, medium, high}",
      p_tools["estimated_complexity"] in ("low", "medium", "high"),
      "got %r" % p_tools["estimated_complexity"])

# ------------------------------------------------- 7-9: không còn byte-identical
p_dark = planner.plan("Thêm dark mode cho web UI")

def _fingerprint(p):
    return json.dumps(p, sort_keys=True, ensure_ascii=False)

check("7. 2 task khác nhau -> plan KHÁC nhau (không byte-identical)",
      _fingerprint(p_api) != _fingerprint(p_tools),
      "plan endpoint và plan tools giống hệt nhau!")

check("8. 'dark mode web UI' vs 'endpoint /api/users' -> plan khác nhau",
      _fingerprint(p_dark) != _fingerprint(p_api),
      "2 plan giống hệt nhau!")

check("9. files của plan endpoint khác files của plan tools",
      set(p_api["files_affected"]) != set(p_tools["files_affected"]),
      "files_affected trùng nhau")

# ------------------------------------------------- 10-12: thứ tự dependency
check("10. steps edit được sắp theo thứ tự dependency (gốc trước, ngọn sau)",
      _edit_steps_in_topo_order(planner, p_api),
      "thứ tự edit steps sai")

check("11. files_affected bao gồm cả file khớp keyword lẫn dependents",
      any(f == "gateway/api.py" for f in p_api["files_affected"])
      and len(p_api["files_affected"]) > 1,
      "files_affected=%s" % p_api["files_affected"])

check("12. reason của step edit nhắc tới keyword/task (không phải template mù)",
      any("keyword" in s.get("reason", "") or "depgraph" in s.get("reason", "")
          for s in p_api["steps"] if s["action"] == "edit"),
      "reason không giải thích được tại sao")

# ------------------------------------------------- 13-15: robustness
p_empty = planner.plan("")
check("13. task rỗng vẫn trả về plan hợp lệ (không crash)",
      isinstance(p_empty["steps"], list) and len(p_empty["steps"]) > 0
      and p_empty["estimated_complexity"] in ("low", "medium", "high"))

p_ref = planner.plan("refactor module autonomous cho gọn")
check("14. 'refactor module autonomous' -> task_type refactor + tìm đúng thư mục",
      p_ref["task_type"] == "refactor"
      and any(f.startswith("agent/src/autonomous/") for f in p_ref["files_affected"]),
      "type=%r files=%s" % (p_ref["task_type"], p_ref["files_affected"][:5]))

check("15. plan() trả về đúng schema {steps, files_affected, estimated_complexity}",
      all(k in p_api for k in ("steps", "files_affected", "estimated_complexity"))
      and all(set(s) >= {"action", "files", "reason"} for s in p_api["steps"]))

# ------------------------------------------------- 16: dependents thật từ depgraph
check("16. dependents trong plan khớp depgraph.impact_analysis trực tiếp",
      _impact_matches_depgraph(planner, p_tools),
      "mismatch với impact_analysis")


print("\n== KẾT QUẢ: %d PASS, %d FAIL ==" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
