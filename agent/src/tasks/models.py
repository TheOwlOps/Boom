"""Task models cho durable task engine. Chỉ stdlib, JSON-serializable."""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class TaskStep:
    id: str
    desc: str
    status: StepStatus = StepStatus.PENDING
    result: str = ""
    error: str = ""
    depends_on: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "desc": self.desc,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TaskStep":
        raw = d.get("status", StepStatus.PENDING.value)
        try:
            status = StepStatus(raw)
        except ValueError:
            status = StepStatus.PENDING
        return cls(
            id=d.get("id", ""),
            desc=d.get("desc", ""),
            status=status,
            result=d.get("result", ""),
            error=d.get("error", ""),
            depends_on=list(d.get("depends_on") or []),
        )


@dataclass
class Task:
    id: str
    title: str
    goal: str
    status: TaskStatus = TaskStatus.PENDING
    steps: List[TaskStep] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "status": self.status.value,
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Task":
        raw = d.get("status", TaskStatus.PENDING.value)
        try:
            status = TaskStatus(raw)
        except ValueError:
            status = TaskStatus.PENDING
        steps = [TaskStep.from_dict(s) for s in (d.get("steps") or [])]
        return cls(
            id=d.get("id", ""),
            title=d.get("title", ""),
            goal=d.get("goal", ""),
            status=status,
            steps=steps,
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )
