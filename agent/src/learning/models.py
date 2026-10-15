"""Models cho hệ thống tự học (learning) của Boom.

Chỉ stdlib. JSON-serializable. Pattern tái dùng từ tasks/models.py:
str Enum + @dataclass + to_dict/from_dict (from_dict chịu được giá trị lạ).
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class LearningType(str, Enum):
    """Loại kiến thức agent học được về user."""

    PREFERENCE = "preference"   # sở thích / thói quen của user
    FACT = "fact"               # sự thật về user
    DECISION = "decision"       # quyết định đã chốt
    CORRECTION = "correction"   # lần user sửa mình


def _clamp_confidence(value: Any) -> float:
    """Ép confidence về [0.0, 1.0]; giá trị không phải số → 0.0."""
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
    """Một mẩu kiến thức đã học.

    `source` chỉ mô tả nguồn (vd "conversation 2026-10-15") — KHÔNG lưu
    transcript nguyên văn.
    `superseded_by` trỏ tới id của Learning mới thay thế mẩu này.
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
