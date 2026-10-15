"""Chia goal thành TaskStep qua 1 LLM turn. Parse fail → 1 step fallback, không bịa."""
import json
from typing import Callable, List, Optional
from tasks.models import TaskStep, StepStatus

_PROMPT_TEMPLATE = """Bạn là bộ lập kế hoạch cho một AI agent. Mục tiêu của người dùng là:

{goal}

Hãy chia mục tiêu thành các bước thực hiện cụ thể, theo thứ tự, mỗi bước là một việc có thể thực hiện độc lập bởi agent.

Trả lời DUY NHẤT bằng một JSON array, KHÔNG thêm giải thích, KHÔNG thêm markdown, KHÔNG dùng code fence. Mỗi phần tử trong array có dạng:
{{"id": "1", "desc": "Mô tả bước cần làm", "depends_on": ["2"]}}

Trong đó:
- "id": chuỗi định danh duy nhất của bước (bắt đầu từ "1").
- "desc": mô tả ngắn gọn, rõ ràng việc phải làm trong bước đó (tiếng Việt).
- "depends_on": danh sách id các bước PHẢI hoàn thành trước khi bước này chạy; để trống [] nếu bước độc lập.

Ví dụ trả lời hợp lệ:
[{{"id": "1", "desc": "Tìm danh sách quán phở ngon ở Quận 1", "depends_on": []}}, {{"id": "2", "desc": "Đọc review từng quán", "depends_on": ["1"]}}, {{"id": "3", "desc": "Chốt 3 quán tốt nhất và gửi địa chỉ", "depends_on": ["1", "2"]}}]
"""


def _fallback(goal: str) -> List[TaskStep]:
    """Fallback trung thực: đúng 1 step chứa nguyên goal, không bịa thêm bước."""
    return [TaskStep(id="1", desc=goal.strip(), status=StepStatus.PENDING)]


def _extract_json_array(text: str) -> Optional[list]:
    """Trích JSON array từ text: lấy từ '[' đầu tiên đến ']' cuối cùng rồi json.loads."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def _build_steps(items: list) -> List[TaskStep]:
    """Validate từng item: dict có id và desc non-empty; id trùng giữ cái đầu;
    depends_on phải là list. Sau đó lọc depends_on chỉ giữ id tồn tại và khác chính nó."""
    seen: dict = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        raw_desc = item.get("desc")
        step_id = str(raw_id).strip() if raw_id is not None else ""
        desc = str(raw_desc).strip() if raw_desc is not None else ""
        if not step_id or not desc:
            continue
        if step_id in seen:
            continue  # id trùng: giữ cái đầu, bỏ cái sau
        depends_on = item.get("depends_on", [])
        if not isinstance(depends_on, list):
            depends_on = []
        depends_on = [str(d).strip() for d in depends_on if str(d).strip()]
        seen[step_id] = TaskStep(
            id=step_id, desc=desc, status=StepStatus.PENDING, depends_on=depends_on
        )
    # Lọc depends_on: chỉ giữ id tồn tại trong steps và khác chính nó (không để DAG gãy)
    ids = set(seen.keys())
    for step in seen.values():
        step.depends_on = [d for d in step.depends_on if d in ids and d != step.id]
    return list(seen.values())


def plan_goal(goal: str, llm: Callable[[str], str]) -> List[TaskStep]:
    """Chia goal thành các TaskStep qua 1 LLM turn.

    - goal rỗng/blank → ValueError.
    - llm raise exception hoặc text trả về không parse được → fallback 1 step.
    """
    if not goal or not goal.strip():
        raise ValueError("goal không được rỗng")
    prompt = _PROMPT_TEMPLATE.format(goal=goal.strip())
    try:
        text = llm(prompt)
    except Exception:
        return _fallback(goal)
    items = _extract_json_array(text or "")
    if items is None:
        return _fallback(goal)
    steps = _build_steps(items)
    if not steps:
        return _fallback(goal)
    return steps
