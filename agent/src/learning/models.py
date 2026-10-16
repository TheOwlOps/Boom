"""Models for Boom's self-learning system.

Stdlib only. JSON-serializable. Pattern reused from tasks/models.py:
str Enum + @dataclass + to_dict/from_dict (from_dict tolerates odd values).
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class LearningType(str, Enum):
    """Kind of knowledge the agent learned about the user."""

    PREFERENCE = "preference"   # user preferences / habits
    FACT = "fact"               # facts about the user
    DECISION = "decision"       # finalized decisions
    CORRECTION = "correction"   # times the user corrected the agent


def _clamp_confidence(value: Any) -> float:
    """Clamps confidence to [0.0, 1.0]; non-numbers → 0.0."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


@dataclass
class Learning:
    """One learned piece of knowledge.

    `source` only describes the origin (e.g. "conversation 2026-10-15") — it does
    NOT store the raw transcript.
    `superseded_by` points at the id of the newer Learning replacing this one.
    """

    id: str
    type: LearningType
    content: str
    confidence: float
    source: str
    created_at: str
    updated_at: str
    superseded_by: Optional[str] = None

    def __post_init__(self) -> None:
        self.confidence = _clamp_confidence(self.confidence)
        if isinstance(self.type, str) and not isinstance(self.type, LearningType):
            try:
                self.type = LearningType(self.type)
            except ValueError:
                self.type = LearningType.PREFERENCE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "content": self.content,
            "confidence": self.confidence,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "superseded_by": self.superseded_by,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Learning":
        raw = d.get("type", LearningType.PREFERENCE.value)
        try:
            ltype = LearningType(raw)
        except ValueError:
            ltype = LearningType.PREFERENCE
        return cls(
            id=d.get("id", ""),
            type=ltype,
            content=d.get("content", ""),
            confidence=_clamp_confidence(d.get("confidence", 0.0)),
            source=d.get("source", ""),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            superseded_by=d.get("superseded_by"),
        )
