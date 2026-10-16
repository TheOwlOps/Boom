"""Splits a goal into TaskSteps via 1 LLM turn. Parse failure → 1 fallback step, no fabrication."""
import json
from typing import Callable, List, Optional
from tasks.models import TaskStep, StepStatus

_PROMPT_TEMPLATE = """You are a planner for an AI agent. The user's goal is:

{goal}

Split the goal into concrete, ordered execution steps; each step should be independently executable by the agent.

Reply with ONLY a JSON array, NO explanations, NO markdown, NO code fences. Each array element looks like:
{{"id": "1", "desc": "Description of the step to do", "depends_on": ["2"]}}

Where:
- "id": the step's unique identifier string (starting from "1").
- "desc": a short, clear description of the work in this step.
- "depends_on": ids of steps that MUST complete before this step runs; empty [] when independent.

Valid example reply:
[{{"id": "1", "desc": "Find good pho restaurants in District 1", "depends_on": []}}, {{"id": "2", "desc": "Read reviews of each place", "depends_on": ["1"]}}, {{"id": "3", "desc": "Pick the 3 best and send addresses", "depends_on": ["1", "2"]}}]
"""


def _fallback(goal: str) -> List[TaskStep]:
    """Honest fallback: exactly 1 step containing the raw goal, no invented steps."""
    return [TaskStep(id="1", desc=goal.strip(), status=StepStatus.PENDING)]


def _extract_json_array(text: str) -> Optional[list]:
    """Extracts a JSON array from text: from the first '[' to the last ']' then json.loads."""
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
    """Validates each item: dict with non-empty id and desc; duplicate ids keep the first;
    depends_on must be a list. Then filters depends_on to existing ids other than itself."""
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
            continue  # duplicate id: keep the first, drop later ones
        depends_on = item.get("depends_on", [])
        if not isinstance(depends_on, list):
            depends_on = []
        depends_on = [str(d).strip() for d in depends_on if str(d).strip()]
        seen[step_id] = TaskStep(
            id=step_id, desc=desc, status=StepStatus.PENDING, depends_on=depends_on
        )
    # Filter depends_on: keeps only ids present in steps and not the step itself (keeps the DAG valid)
    ids = set(seen.keys())
    for step in seen.values():
        step.depends_on = [d for d in step.depends_on if d in ids and d != step.id]
    return list(seen.values())


def plan_goal(goal: str, llm: Callable[[str], str]) -> List[TaskStep]:
    """Splits a goal into TaskSteps via 1 LLM turn.

    - Empty/blank goal → ValueError.
    - llm raising or returning unparsable text → 1-step fallback.
    """
    if not goal or not goal.strip():
        raise ValueError("goal must not be empty")
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
