"""TaskPlanner — breaks a coding task into detailed steps before execution.

On the execution path: controller.run_goal (calls brain.plan) -> calls
TaskPlanner.plan to get a detailed plan (steps with DAG dependencies) before
entering the execute -> verify -> debug loop.

v1: keyword heuristics + templates, NO LLM — fast enough for routine tasks,
no keys or network required.

Usage::

    from autonomous.planner import TaskPlanner
    planner = TaskPlanner()
    plan = planner.plan("Add a search feature to the tools module")
    print(planner.to_markdown(plan))

Plan dict shape:
    {
        "task": str,            # original task description
        "task_type": "feature" | "bugfix" | "refactor",
        "steps": [
            {
                "id": "step_1",
                "title": str,
                "description": str,
                "files": [str],           # files likely touched (may be empty)
                "actions": [str],         # short technical actions
                "dependencies": [str],    # ids of steps that must finish first (DAG)
                "complexity": "low" | "medium" | "high",
            }, ...
        ],
        "estimated_steps": int,
    }
"""

from typing import Dict, List


# ---------------------------------------------------------------------------
# Keywords for detect_task_type (Vietnamese + English, lowercased match).
# NOTE: these tuples are functional classification data — kept as-is.
# ---------------------------------------------------------------------------

_FEATURE_KEYWORDS = (
    "thêm", "them", "tạo mới", "tao moi", "tạo", "tao",
    "add", "create", "implement", "implementer", "triển khai", "trien khai",
    "phát triển", "phat trien", "xây", "xay", "build",
    "chức năng", "chuc nang", "feature", "tính năng", "tinh nang",
    "hỗ trợ", "ho tro", "support", "tích hợp", "tich hop", "integrate",
)

_BUGFIX_KEYWORDS = (
    "lỗi", "loi", "sửa", "sua", "sửa lỗi", "sua loi",
    "fix", "bug", "error", "crash", "sập", "sap",
    "không chạy", "khong chay", "không hoạt động", "khong hoat dong",
    "broken", "fail", "failing", "sai", "sai kết quả", "sai ket qua",
    "khắc phục", "khac phuc", "vấn đề", "van de", "issue",
)

_REFACTOR_KEYWORDS = (
    "refactor", "tách", "tach", "gộp", "gop", "merge", "split",
    "dọn", "don", "clean", "cleanup", "dọn dẹp", "don dep",
    "tổ chức lại", "to chuc lai", "restructure", "reorganize",
    "đổi tên", "doi ten", "rename", "di chuyển", "di chuyen", "move",
    "tối ưu", "toi uu", "optimize", "gọn", "gon", "simplify",
)

_TASK_TYPES = ("feature", "bugfix", "refactor")


# ---------------------------------------------------------------------------
# Step templates per task type.
# NOTE: template titles/descriptions and the to_markdown() labels are pinned
# by evals/test_planner.py — the plan report stays Vietnamese by design.
# ---------------------------------------------------------------------------

# Each template: a list of steps, each a dict with:
#   title, description, files (suggestions), actions.
# Dependencies are auto-built in order (step n depends on step n-1) forming a DAG.
_FEATURE_TEMPLATE = [
    {
        "title": "Khảo sát codebase",
        "description": "Đọc code hiện có để hiểu cấu trúc module, convention "
                       "và vị trí phù hợp để thêm tính năng mới.",
        "files": [],
        "actions": ["read", "explore"],
    },
    {
        "title": "Thiết kế giải pháp",
        "description": "Xác định API/tham số, vị trí đặt code, và các trường "
                       "hợp biên cần xử lý trước khi viết code.",
        "files": [],
        "actions": ["design"],
    },
    {
        "title": "Triển khai tính năng",
        "description": "Viết code theo thiết kế, chia nhỏ theo file, giữ "
                       "đúng convention của repo.",
        "files": [],
        "actions": ["implement", "multi-file"],
    },
    {
        "title": "Viết test",
        "description": "Bổ sung test kiểm tra hành vi đúng/sai và các "
                       "trường hợp biên của tính năng mới.",
        "files": [],
        "actions": ["test", "implement"],
    },
    {
        "title": "Review và hoàn thiện",
        "description": "Chạy toàn bộ test liên quan, rà soát lại code, "
                       "dọn code thừa rồi kết luận.",
        "files": [],
        "actions": ["test", "review"],
    },
]

_BUGFIX_TEMPLATE = [
    {
        "title": "Tái hiện lỗi",
        "description": "Viết script/câu lệnh tái hiện lỗi một cách ổn định "
                       "để có bằng chứng trước khi sửa.",
        "files": [],
        "actions": ["reproduce", "test"],
    },
    {
        "title": "Xác định nguyên nhân gốc",
        "description": "Đọc code đường chạy liên quan, khoanh vùng dòng code "
                       "gây lỗi và giải thích cơ chế.",
        "files": [],
        "actions": ["read", "analyze"],
    },
    {
        "title": "Sửa lỗi",
        "description": "Sửa đúng chỗ gây lỗi với thay đổi nhỏ nhất có thể, "
                       "không lan man sang phần khác.",
        "files": [],
        "actions": ["fix"],
    },
    {
        "title": "Xác minh đã hết lỗi",
        "description": "Chạy lại script tái hiện, đảm bảo lỗi không còn và "
                       "hành vi đúng như mong đợi.",
        "files": [],
        "actions": ["verify", "test"],
    },
    {
        "title": "Test hồi quy",
        "description": "Chạy test liên quan để chắc chắn bản sửa không làm "
                       "hỏng chức năng khác.",
        "files": [],
        "actions": ["test", "regression"],
    },
]

_REFACTOR_TEMPLATE = [
    {
        "title": "Phân tích hiện trạng",
        "description": "Đọc các file liên quan, liệt kê điểm trùng lặp, "
                       "file quá lớn, và ranh giới module nên có.",
        "files": [],
        "actions": ["read", "analyze"],
    },
    {
        "title": "Lập kế hoạch thay đổi",
        "description": "Xác định thứ tự tách/gộp/đổi tên theo từng file, "
                       "đảm bảo mỗi bước vẫn giữ code chạy được.",
        "files": [],
        "actions": ["design", "multi-file"],
    },
    {
        "title": "Áp dụng theo từng file",
        "description": "Thực hiện refactor từng file một, chạy test sau mỗi "
                       "bước để phát hiện hỏng sớm.",
        "files": [],
        "actions": ["refactor", "multi-file"],
    },
    {
        "title": "Chạy test đầy đủ",
        "description": "Chạy toàn bộ test suite liên quan sau khi refactor "
                       "xong để bắt lỗi.",
        "files": [],
        "actions": ["test"],
    },
    {
        "title": "Xác minh không đổi hành vi",
        "description": "So sánh hành vi trước/sau refactor, đảm bảo chỉ "
                       "thay đổi cấu trúc chứ không đổi tính năng.",
        "files": [],
        "actions": ["verify", "review"],
    },
]

_TEMPLATES = {
    "feature": _FEATURE_TEMPLATE,
    "bugfix": _BUGFIX_TEMPLATE,
    "refactor": _REFACTOR_TEMPLATE,
}

# File suggestions based on keywords in the task description.
_FILE_HINTS = (
    ("tool", ["agent/src/tools.py"]),
    ("channel", ["gateway/channels/"]),
    ("cron", ["agent/src/cron/"]),
    ("memory", ["agent/src/memory/"]),
    ("skill", ["agent/src/skills_hub.py"]),
    ("gateway", ["gateway/"]),
    ("web", ["agent/src/web_tools.py"]),
    ("autonomous", ["agent/src/autonomous/"]),
    ("test", ["evals/"]),
)


def detect_task_type(task_description: str) -> str:
    """Classifies a task by keyword (Vietnamese + English).

    Priority: bugfix > refactor > feature. Defaults to "feature".
    """
    text = (task_description or "").lower()
    for kw in _BUGFIX_KEYWORDS:
        if kw in text:
            return "bugfix"
    for kw in _REFACTOR_KEYWORDS:
        if kw in text:
            return "refactor"
    for kw in _FEATURE_KEYWORDS:
        if kw in text:
            return "feature"
    return "feature"


def _suggest_files(task_description: str) -> List[str]:
    """Suggests possibly-relevant files from keywords in the description."""
    text = (task_description or "").lower()
    hints = []
    for kw, files in _FILE_HINTS:
        if kw in text and files[0] not in hints:
            hints.extend(files)
    return hints


def estimate_complexity(step: Dict) -> str:
    """Heuristic step-complexity estimate.

    - "high":   touches many files (multi-file / >2 files) or is a refactor
    - "medium": involves test/verify/reproduce, or >= 3 actions
    - "low":    everything else
    """
    actions = [str(a).lower() for a in (step.get("actions") or [])]
    title = str(step.get("title") or "").lower()
    files = step.get("files") or []

    blob = " ".join(actions) + " " + title
    if "multi-file" in actions or "refactor" in actions or len(files) > 2:
        return "high"
    if ("test" in blob or "verify" in blob or "reproduce" in blob
            or len(actions) >= 3):
        return "medium"
    return "low"


class TaskPlanner:
    """Turns a task description into detailed plan steps (heuristic, no LLM)."""

    def plan(self, task_description: str) -> Dict:
        """Returns a plan dict {task, task_type, steps, estimated_steps}.

        Steps get ids step_1..step_N, dependencies built in logical order
        (each step depends on the previous one) forming a linear DAG.
        """
        task = (task_description or "").strip()
        task_type = detect_task_type(task)
        template = _TEMPLATES[task_type]
        file_hints = _suggest_files(task)

        steps: List[Dict] = []
        prev_id = None
        for i, tpl in enumerate(template, start=1):
            step_id = "step_%d" % i
            step = {
                "id": step_id,
                "title": tpl["title"],
                "description": tpl["description"],
                "files": list(file_hints) if i == 1 else list(tpl["files"]),
                "actions": list(tpl["actions"]),
                "dependencies": [prev_id] if prev_id else [],
            }
            step["complexity"] = estimate_complexity(step)
            steps.append(step)
            prev_id = step_id

        return {
            "task": task,
            "task_type": task_type,
            "steps": steps,
            "estimated_steps": len(steps),
        }

    def to_markdown(self, plan: Dict) -> str:
        """Renders the plan as Vietnamese markdown for display/confirmation.

        NOTE: the labels below are pinned by evals/test_planner.py — keep
        them Vietnamese.
        """
        lines = []
        lines.append("# Kế hoạch thực hiện")
        lines.append("")
        lines.append("**Task:** %s" % (plan.get("task") or "(trống)"))
        type_label = {"feature": "Tính năng mới",
                      "bugfix": "Sửa lỗi",
                      "refactor": "Refactor"}.get(plan.get("task_type"),
                                                  plan.get("task_type"))
        lines.append("**Loại:** %s" % type_label)
        lines.append("**Số bước dự kiến:** %d" % plan.get("estimated_steps", 0))
        lines.append("")
        lines.append("## Các bước")
        lines.append("")
        for step in plan.get("steps", []):
            lines.append("### %s. %s `%s`" % (
                step["id"].replace("step_", ""), step["title"],
                step["complexity"]))
            lines.append("")
            lines.append(step["description"])
            if step.get("files"):
                lines.append("")
                lines.append("- **File liên quan:** %s"
                             % ", ".join("`%s`" % f for f in step["files"]))
            if step.get("actions"):
                lines.append("- **Hành động:** %s" % ", ".join(step["actions"]))
            if step.get("dependencies"):
                lines.append("- **Phụ thuộc:** %s"
                             % ", ".join(step["dependencies"]))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def plan_task(task_description: str) -> Dict:
    """Convenience: quickly plan one task without creating a TaskPlanner."""
    return TaskPlanner().plan(task_description)
