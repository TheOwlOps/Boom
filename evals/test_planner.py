"""Test cho autonomous/planner.py — chay bang: python3 test_planner.py (stdlib only)."""

import sys
from pathlib import Path
import os

# Import root is agent/src (repo convention); the modules under test live
# in subpackages there (they previously lived next to this file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from autonomous.planner import (
    TaskPlanner,
    detect_task_type,
    estimate_complexity,
    plan_task,
)

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  PASS  %s" % name)
    else:
        _failed += 1
        print("  FAIL  %s" % name)


def _no_cycle(steps):
    """Kiem tra dependencies khong co chu trinh (topological check don gian)."""
    ids = {s["id"] for s in steps}
    # moi dependency phai tro toi step da ton tai va co so thu tu nho hon
    order = {s["id"]: n for n, s in enumerate(steps)}
    for s in steps:
        for d in s["dependencies"]:
            if d not in ids or order[d] >= order[s["id"]]:
                return False
    return True


def test_detect_vietnamese():
    print("[detect_task_type - tieng Viet]")
    check("them chuc nang -> feature",
          detect_task_type("Thêm chức năng tìm kiếm vào module tools") == "feature")
    check("sua loi -> bugfix",
          detect_task_type("Sửa lỗi crash khi gửi tin nhắn rỗng") == "bugfix")
    check("loi khong chay -> bugfix",
          detect_task_type("Lỗi: gateway không chạy sau khi pull") == "bugfix")
    check("tach file -> refactor",
          detect_task_type("Tách file channels.py thành module riêng từng kênh") == "refactor")
    check("don dep -> refactor",
          detect_task_type("Dọn dẹp code thừa trong agent") == "refactor")


def test_detect_english():
    print("[detect_task_type - tieng Anh]")
    check("add feature -> feature",
          detect_task_type("Add a new sticker sending feature") == "feature")
    check("implement -> feature",
          detect_task_type("Implement retry logic for webhooks") == "feature")
    check("fix bug -> bugfix",
          detect_task_type("Fix bug where cron jobs never fire") == "bugfix")
    check("crash -> bugfix",
          detect_task_type("App crashes on empty message") == "bugfix")
    check("refactor -> refactor",
          detect_task_type("Refactor the channel adapters into separate modules") == "refactor")
    check("mac dinh -> feature",
          detect_task_type("làm cái gì đó hay hay") == "feature")


def test_plan_feature():
    print("[plan - feature]")
    p = TaskPlanner().plan("Thêm chức năng gửi sticker cho kênh Telegram")
    check("task_type = feature", p["task_type"] == "feature")
    check("dung 5 steps", len(p["steps"]) == 5 and p["estimated_steps"] == 5)
    titles = [s["title"] for s in p["steps"]]
    check("template feature dung thu tu",
          titles == ["Khảo sát codebase", "Thiết kế giải pháp",
                     "Triển khai tính năng", "Viết test", "Review và hoàn thiện"])
    check("id tang dan", [s["id"] for s in p["steps"]] ==
          ["step_1", "step_2", "step_3", "step_4", "step_5"])
    check("dependencies hop le, khong circular", _no_cycle(p["steps"]))
    check("step_1 khong phu thuoc ai", p["steps"][0]["dependencies"] == [])
    check("step sau phu thuoc step truoc",
          all(p["steps"][i]["dependencies"] == ["step_%d" % i]
              for i in range(1, 5)))
    check("moi step co complexity hop le",
          all(s["complexity"] in ("low", "medium", "high") for s in p["steps"]))
    check("step implement la high (multi-file)",
          p["steps"][2]["complexity"] == "high")
    check("step test la medium",
          p["steps"][3]["complexity"] == "medium")


def test_plan_bugfix():
    print("[plan - bugfix]")
    p = TaskPlanner().plan("Fix bug: cron job không chạy đúng giờ")
    check("task_type = bugfix", p["task_type"] == "bugfix")
    titles = [s["title"] for s in p["steps"]]
    check("template bugfix dung thu tu",
          titles == ["Tái hiện lỗi", "Xác định nguyên nhân gốc", "Sửa lỗi",
                     "Xác minh đã hết lỗi", "Test hồi quy"])
    check("dependencies hop le, khong circular", _no_cycle(p["steps"]))
    check("step reproduce la medium",
          p["steps"][0]["complexity"] == "medium")
    check("goi y file cron", any("cron" in f for f in p["steps"][0]["files"]))


def test_plan_refactor():
    print("[plan - refactor]")
    p = TaskPlanner().plan("Refactor gateway channels into separate modules")
    check("task_type = refactor", p["task_type"] == "refactor")
    titles = [s["title"] for s in p["steps"]]
    check("template refactor dung thu tu",
          titles == ["Phân tích hiện trạng", "Lập kế hoạch thay đổi",
                     "Áp dụng theo từng file", "Chạy test đầy đủ",
                     "Xác minh không đổi hành vi"])
    check("dependencies hop le, khong circular", _no_cycle(p["steps"]))
    check("step apply per file la high (refactor+multi-file)",
          p["steps"][2]["complexity"] == "high")


def test_estimate_complexity():
    print("[estimate_complexity]")
    check("multi-file -> high",
          estimate_complexity({"actions": ["implement", "multi-file"],
                               "files": [], "title": "x"}) == "high")
    check(">2 files -> high",
          estimate_complexity({"actions": ["read"], "files": ["a", "b", "c"],
                               "title": "x"}) == "high")
    check("refactor -> high",
          estimate_complexity({"actions": ["refactor"], "files": [],
                               "title": "x"}) == "high")
    check("test -> medium",
          estimate_complexity({"actions": ["test"], "files": [],
                               "title": "Viet test"}) == "medium")
    check("verify -> medium",
          estimate_complexity({"actions": ["verify"], "files": [],
                               "title": "x"}) == "medium")
    check("don gian -> low",
          estimate_complexity({"actions": ["read"], "files": [],
                               "title": "Doc code"}) == "low")


def test_to_markdown():
    print("[to_markdown]")
    p = TaskPlanner().plan("Sửa lỗi tin nhắn bị trùng khi retry")
    md = TaskPlanner().to_markdown(p)
    check("co tieu de", "# Kế hoạch thực hiện" in md)
    check("co loai Sửa lỗi", "**Loại:** Sửa lỗi" in md)
    check("co step tai hien loi", "Tái hiện lỗi" in md)
    check("co complexity trong step", "`medium`" in md or "`high`" in md)
    check("co phu thuoc", "Phụ thuộc" in md)
    check("khong loi khi task trong",
          isinstance(TaskPlanner().to_markdown(TaskPlanner().plan("")), str))


def test_plan_task_helper():
    print("[plan_task helper]")
    p = plan_task("Create a new memory search tool")
    check("tra ve dict co task_type", p["task_type"] == "feature")
    check("gi nguyen task goc", p["task"] == "Create a new memory search tool")


if __name__ == "__main__":
    test_detect_vietnamese()
    test_detect_english()
    test_plan_feature()
    test_plan_bugfix()
    test_plan_refactor()
    test_estimate_complexity()
    test_to_markdown()
    test_plan_task_helper()
    print("\n%d passed, %d failed" % (_passed, _failed))
    sys.exit(1 if _failed else 0)
