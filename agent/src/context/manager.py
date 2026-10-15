"""Quản lý context window cho Boom: đếm token, prune, compress.

Phạm vi của module này là conversation-level (toàn bộ lịch sử hội thoại),
khác với loop/context_compressor.py chỉ micro-compact output từng turn.

Chiến lược:
  - add_message(): thêm message, ước lượng token ngay lúc thêm
    (~4 chars/token cho EN, ~2.5 cho VI có dấu).
  - get_context(max_tokens): lấy messages vừa khít budget, ưu tiên giữ
    system prompt + các message mới nhất.
  - prune(): xóa dần message ít quan trọng nhất (tool result cũ trước,
    không bao giờ xóa system, giữ user/assistant tới cùng).
  - compress(): khi vượt ngưỡng, tóm tắt messages cũ bằng HeuristicSummarizer
    thành 1 message duy nhất, giữ nguyên system + N message mới nhất.
"""
from __future__ import annotations

import math
import re
import time
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .summarizer import HeuristicSummarizer

# Mỗi message tốn thêm ~4 token cho role/format (theo cách tính của OpenAI).
MSG_OVERHEAD = 4

# Ký tự có dấu tiếng Việt → mật độ token cao hơn EN (~2.5 chars/token).
_VI_DIACRITICS = re.compile(
    "[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡ"
    "ùúụủũưừứựửữỳýỵỷỹđ"
    "ÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠ"
    "ÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ]"
)


def estimate_tokens(text: str) -> int:
    """Ước lượng số token của một đoạn text.

    Heuristic: ~4 ký tự/token cho tiếng Anh, ~2.5 ký tự/token cho tiếng Việt
    (nhận diện qua ký tự có dấu — tiếng Việt tokenize kém hiệu quả hơn).
    """
    if not text:
        return 0
    if _VI_DIACRITICS.search(text):
        return max(1, math.ceil(len(text) / 2.5))
    return max(1, math.ceil(len(text) / 4))


class Message(BaseModel):
    """Một message trong hội thoại, kèm số token đã ước lượng."""

    role: str = Field(pattern=r"^(system|user|assistant|tool)$")
    content: str = ""
    tokens: int = Field(default=0, ge=0)
    created_at: float = Field(default_factory=time.time)


class ContextManager:
    """Quản lý toàn bộ context của một phiên hội thoại."""

    # Điểm ưu tiên theo role: càng cao càng được giữ lại khi prune.
    ROLE_PRIORITY = {"system": 1000, "user": 60, "assistant": 40, "tool": 15}

    def __init__(
        self,
        max_tokens: int = 16000,
        compress_threshold: float = 0.8,
        keep_recent: int = 10,
    ):
        # max_tokens: trần context; vượt compress_threshold * max_tokens
        # thì nên gọi compress(). keep_recent: số message mới nhất luôn giữ.
        self.max_tokens = max_tokens
        self.compress_threshold = compress_threshold
        self.keep_recent = keep_recent
        self.summarizer = HeuristicSummarizer()
        self._messages: List[Message] = []
        self._compressed_count = 0  # tổng số message đã bị nén thành summary

    # -- thêm / thống kê ------------------------------------------------------
    def add_message(self, role: str, content: str) -> Message:
        """Thêm một message, tự ước lượng token (kèm overhead role/format)."""
        msg = Message(
            role=role,
            content=content or "",
            tokens=estimate_tokens(content or "") + MSG_OVERHEAD,
        )
        self._messages.append(msg)
        return msg

    @property
    def messages(self) -> List[Message]:
        """Toàn bộ messages (bản copy, không sửa được list gốc)."""
        return list(self._messages)

    def total_tokens(self) -> int:
        """Tổng token hiện tại của toàn bộ lịch sử."""
        return sum(m.tokens for m in self._messages)

    def should_compress(self) -> bool:
        """True khi đã vượt ngưỡng cần nén (mặc định 80% max_tokens)."""
        return self.total_tokens() >= self.max_tokens * self.compress_threshold

    def stats(self) -> Dict:
        """Thống kê nhanh: số message theo role, token, số đã nén."""
        counts: Dict[str, int] = {}
        for m in self._messages:
            counts[m.role] = counts.get(m.role, 0) + 1
        return {
            "total_messages": len(self._messages),
            "by_role": counts,
            "total_tokens": self.total_tokens(),
            "max_tokens": self.max_tokens,
            "compressed_count": self._compressed_count,
        }

    # -- lấy context vừa budget ------------------------------------------------
    def _fit_or_trim(self, msg: Message, budget: int) -> Optional[Message]:
        """Trả về msg nếu vừa budget; nếu không, trả về bản cắt ngắn
        (giữ đầu + cuối). Trả về None khi budget không đủ cho cả bản cắt."""
        if budget <= MSG_OVERHEAD + 1:
            return None
        if msg.tokens <= budget:
            return msg
        chars_per_token = 2.5 if _VI_DIACRITICS.search(msg.content) else 4.0
        marker = "\n[...nội dung bị cắt bớt cho vừa context...]\n"
        max_chars = int((budget - MSG_OVERHEAD) * chars_per_token) - len(marker) - 8
        if max_chars <= 0:
            return None
        text = msg.content
        head = int(max_chars * 0.7)
        tail = max_chars - head
        trimmed = text[:head] + marker + (text[-tail:] if tail > 0 else "")
        return Message(
            role=msg.role,
            content=trimmed,
            tokens=estimate_tokens(trimmed) + MSG_OVERHEAD,
            created_at=msg.created_at,
        )

    def get_context(self, max_tokens: Optional[int] = None) -> List[Message]:
        """Trả về messages vừa khít budget, ưu tiên system + mới nhất.

        Thứ tự: system prompt trước, sau đó các message mới nhất.
        Đảm bảo tổng token <= max_tokens (cắt ngắn message khi cần).
        """
        budget = max_tokens if max_tokens is not None else self.max_tokens
        system = [m for m in self._messages if m.role == "system"]
        rest = [m for m in self._messages if m.role != "system"]

        out: List[Message] = []
        used = 0
        # 1) Luôn giữ system prompt (cắt ngắn nếu tự nó đã vượt budget).
        for m in system:
            fitted = self._fit_or_trim(m, budget - used)
            if fitted is None:
                break
            out.append(fitted)
            used += fitted.tokens

        # 2) Lấy từ message mới nhất trở về trước cho tới khi hết budget.
        recent: List[Message] = []
        for m in reversed(rest):
            if used >= budget:
                break
            fitted = self._fit_or_trim(m, budget - used)
            if fitted is None:
                continue  # message này quá dài, thử message cũ hơn
            recent.append(fitted)
            used += fitted.tokens
        recent.reverse()

        # 3) Đảm bảo message mới nhất luôn có mặt (cắt ngắn nếu cần).
        if rest and not recent:
            fitted = self._fit_or_trim(rest[-1], budget - used)
            if fitted is not None:
                recent.append(fitted)

        return out + recent

    # -- prune: xóa message ít quan trọng nhất ---------------------------------
    def _importance(self, index: int) -> float:
        """Điểm quan trọng: role càng cao càng giữ, message càng mới càng giữ."""
        m = self._messages[index]
        recency = (index / max(1, len(self._messages))) * 25.0
        return self.ROLE_PRIORITY.get(m.role, 0) + recency

    def prune(self, max_tokens: Optional[int] = None) -> int:
        """Xóa dần message ít quan trọng nhất cho tới khi vừa budget.

        Thứ tự xóa: tool result cũ → assistant cũ → user cũ.
        Không bao giờ xóa system. Trả về số message đã xóa.
        """
        budget = max_tokens if max_tokens is not None else self.max_tokens
        removed = 0
        while self.total_tokens() > budget and len(self._messages) > 1:
            # Chọn message có điểm thấp nhất trong số message được phép xóa.
            candidates = [i for i, m in enumerate(self._messages) if m.role != "system"]
            if not candidates:
                break  # chỉ còn system → dừng, không xóa system
            victim = min(candidates, key=self._importance)
            del self._messages[victim]
            removed += 1
        return removed

    # -- compress: tóm tắt messages cũ thành 1 summary --------------------------
    def compress(self, keep_recent: Optional[int] = None) -> Optional[Message]:
        """Nén messages cũ thành 1 message tóm tắt (heuristic, không cần LLM).

        Giữ nguyên: toàn bộ system messages + `keep_recent` message mới nhất.
        Phần còn lại được HeuristicSummarizer tóm tắt (giữ decisions,
        action items, entities). Trả về message tóm tắt, hoặc None nếu
        không có gì để nén.
        """
        keep = keep_recent if keep_recent is not None else self.keep_recent
        system = [m for m in self._messages if m.role == "system"]
        rest = [m for m in self._messages if m.role != "system"]
        if len(rest) <= keep:
            return None  # lịch sử còn ngắn, chưa cần nén

        old, recent = rest[:-keep], rest[-keep:]
        summary_text = self.summarizer.summarize(old)
        header = (
            f"[Tóm tắt ngữ cảnh] {len(old)} messages cũ đã được nén "
            f"lúc {time.strftime('%Y-%m-%d %H:%M:%S')} — chi tiết gốc đã loại bỏ."
        )
        content = header + "\n" + summary_text
        summary_msg = Message(
            role="assistant",
            content=content,
            tokens=estimate_tokens(content) + MSG_OVERHEAD,
        )
        self._compressed_count += len(old)
        self._messages = system + [summary_msg] + recent
        return summary_msg
