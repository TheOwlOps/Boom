"""TaskPlanner — phan tich task code thanh steps chi tiet truoc khi thuc thi.

Nam tren duong chay: controller.run_goal(goi brain.plan) -> goi TaskPlanner.plan
de co duoc ke hoach chi tiet (steps co dependencies DAG) truoc khi vao vong lap
execute -> verify -> debug.

v1: heuristic keyword + template, KHONG dung LLM — du de phan tich nhanh cac
task thong thuong, khong phu thuoc key hay mang.

Cach dung::

    from autonomous.planner import TaskPlanner
    planner = TaskPlanner()
    plan = planner.plan("Them chuc nang tim kiem vao module tools")
    print(planner.to_markdown(plan))

Plan dict co dang:
    {
        "task": str,            # mo ta task goc
        "task_type": "feature" | "bugfix" | "refactor",
        "steps": [
            {
                "id": "step_1",
                "title": str,
                "description": str,
                "files": [str],           # file du kien cham toi (co the rong)
                "actions": [str],         # hanh dong ky thuat ngan gon
                "dependencies": [str],    # id cac step phai xong truoc (DAG)
                "complexity": "low" | "medium" | "high",
            }, ...
        ],
        "estimated_steps": int,
    }
"""

from typing import Dict, List


# ---------------------------------------------------------------------------
# Keyword cho detect_task_type (Viet + Anh, so sanh tren chu thuong)
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
# Template steps cho tung loai task
# ---------------------------------------------------------------------------

# Moi template: list cac step, moi step la dict co:
#   title, description, files (goi y), actions.
# Dependencies tu dong xay theo thu tu (step n phu thuoc step n-1) de thanh DAG.
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

# Goi y file dua tren tu khoa xuat hien trong mo ta task.
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
    """Phan loai task bang keyword (Viet + Anh).

    Uu tien: bugfix > refactor > feature. Mac dinh tra ve "feature".
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
    """Goi y file co the lien quan dua tren tu khoa trong mo ta."""
    text = (task_description or "").lower()
    hints = []
    for kw, files in _FILE_HINTS:
        if kw in text and files[0] not in hints:
            hints.extend(files)
    return hints


def estimate_complexity(step: Dict) -> str:
    """Heuristic do phuc tap cua mot step.

    - "high":   cham nhieu file (multi-file / >2 file) hoac refactor
    - "medium": co test/verify/reproduce, hoac >= 3 actions
    - "low":    con lai
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
    """Phan tich mo ta task thanh plan steps chi tiet (heuristic, khong LLM)."""

    def plan(self, task_description: str) -> Dict:
        """Tra ve plan dict {task, task_type, steps, estimated_steps}.

        Steps duoc danh id step_1..step_N, dependencies xay theo thu tu
        logic (step sau phu thuoc step ngay truoc) tao thanh DAG tuyen tinh.
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
        """Render plan thanh markdown tieng Viet de hien thi/xac nhan."""
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
    """Ham tien ich: plan nhanh 1 task ma khong can tao TaskPlanner."""
    return TaskPlanner().plan(task_description)
