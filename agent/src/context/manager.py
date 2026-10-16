"""Context window management for Boom: token counting, pruning, compression.

This module works at conversation level (the whole chat history), unlike
loop/context_compressor.py which only micro-compacts each turn's output.

Strategy:
  - add_message(): adds a message, estimating tokens at insertion time
    (~4 chars/token for EN, ~2.5 for accented VI).
  - get_context(max_tokens): takes messages fitting the budget, preferring
    the system prompt + the newest messages.
  - prune(): removes the least important messages first (old tool results
    first, never the system, keeps user/assistant to the end).
  - compress(): past the threshold, summarizes old messages with
    HeuristicSummarizer into 1 message, keeping system + N newest.
"""
from __future__ import annotations

import math
import re
import time
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .summarizer import HeuristicSummarizer

# Each message costs ~4 extra tokens for role/format (OpenAI's accounting).
MSG_OVERHEAD = 4

# Accented Vietnamese chars → higher token density than EN (~2.5 chars/token).
# NOTE: functional detection regex — kept as-is.
_VI_DIACRITICS = re.compile(
    "[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡ"
    "ùúụủũưừứựửữỳýỵỷỹđ"
    "ÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠ"
    "ÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ]"
)


def estimate_tokens(text: str) -> int:
    """Estimates a text's token count.

    Heuristic: ~4 chars/token for English, ~2.5 chars/token for Vietnamese
    (detected via accented chars — Vietnamese tokenizes less efficiently).
    """
    if not text:
        return 0
    if _VI_DIACRITICS.search(text):
        return max(1, math.ceil(len(text) / 2.5))
    return max(1, math.ceil(len(text) / 4))


class Message(BaseModel):
    """One conversation message, with its estimated token count."""

    role: str = Field(pattern=r"^(system|user|assistant|tool)$")
    content: str = ""
    tokens: int = Field(default=0, ge=0)
    created_at: float = Field(default_factory=time.time)


class ContextManager:
    """Manages a whole conversation session's context."""

    # Priority by role: higher = kept longer during pruning.
    ROLE_PRIORITY = {"system": 1000, "user": 60, "assistant": 40, "tool": 15}

    def __init__(
        self,
        max_tokens: int = 16000,
        compress_threshold: float = 0.8,
        keep_recent: int = 10,
    ):
        # max_tokens: the context ceiling; past compress_threshold * max_tokens
        # you should call compress(). keep_recent: newest messages always kept.
        self.max_tokens = max_tokens
        self.compress_threshold = compress_threshold
        self.keep_recent = keep_recent
        self.summarizer = HeuristicSummarizer()
        self._messages: List[Message] = []
        self._compressed_count = 0  # total messages compressed into summaries

    # -- add / stats ----------------------------------------------------------
    def add_message(self, role: str, content: str) -> Message:
        """Adds a message, auto-estimating tokens (incl. role/format overhead)."""
        msg = Message(
            role=role,
            content=content or "",
            tokens=estimate_tokens(content or "") + MSG_OVERHEAD,
        )
        self._messages.append(msg)
        return msg

    @property
    def messages(self) -> List[Message]:
        """All messages (a copy — the original list can't be mutated)."""
        return list(self._messages)

    def total_tokens(self) -> int:
        """Current total tokens of the whole history."""
        return sum(m.tokens for m in self._messages)

    def should_compress(self) -> bool:
        """True past the compression threshold (default 80% of max_tokens)."""
        return self.total_tokens() >= self.max_tokens * self.compress_threshold

    def stats(self) -> Dict:
        """Quick stats: message counts by role, tokens, compressed count."""
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

    # -- fit messages into a budget -------------------------------------------
    def _fit_or_trim(self, msg: Message, budget: int) -> Optional[Message]:
        """Returns msg if it fits the budget; otherwise a shortened copy
        (keeps head + tail). Returns None when even the trimmed copy won't fit."""
        if budget <= MSG_OVERHEAD + 1:
            return None
        if msg.tokens <= budget:
            return msg
        chars_per_token = 2.5 if _VI_DIACRITICS.search(msg.content) else 4.0
        marker = "\n[...content trimmed to fit context...]\n"
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
        """Returns messages fitting the budget, preferring system + newest.

        Order: system prompt first, then the newest messages.
        Guarantees total tokens <= max_tokens (trimming messages as needed).
        """
        budget = max_tokens if max_tokens is not None else self.max_tokens
        system = [m for m in self._messages if m.role == "system"]
        rest = [m for m in self._messages if m.role != "system"]

        out: List[Message] = []
        used = 0
        # 1) Always keep the system prompt (trim it if it alone exceeds budget).
        for m in system:
            fitted = self._fit_or_trim(m, budget - used)
            if fitted is None:
                break
            out.append(fitted)
            used += fitted.tokens

        # 2) Take newest-first until the budget runs out.
        recent: List[Message] = []
        for m in reversed(rest):
            if used >= budget:
                break
            fitted = self._fit_or_trim(m, budget - used)
            if fitted is None:
                continue  # this message is too long, try an older one
            recent.append(fitted)
            used += fitted.tokens
        recent.reverse()

        # 3) The newest message must always be present (trimmed if needed).
        if rest and not recent:
            fitted = self._fit_or_trim(rest[-1], budget - used)
            if fitted is not None:
                recent.append(fitted)

        return out + recent

    # -- prune: drop least-important messages ----------------------------------
    def _importance(self, index: int) -> float:
        """Importance score: higher role = kept, newer = kept."""
        m = self._messages[index]
        recency = (index / max(1, len(self._messages))) * 25.0
        return self.ROLE_PRIORITY.get(m.role, 0) + recency

    def prune(self, max_tokens: Optional[int] = None) -> int:
        """Drops the least important messages until the budget fits.

        Drop order: old tool results → old assistant → old user.
        Never drops the system prompt. Returns messages removed.
        """
        budget = max_tokens if max_tokens is not None else self.max_tokens
        removed = 0
        while self.total_tokens() > budget and len(self._messages) > 1:
            # Pick the lowest-scoring message among the droppable ones.
            candidates = [i for i, m in enumerate(self._messages) if m.role != "system"]
            if not candidates:
                break  # only the system left → stop, never drop the system
            victim = min(candidates, key=self._importance)
            del self._messages[victim]
            removed += 1
        return removed

    # -- compress: summarize old messages into 1 --------------------------------
    def compress(self, keep_recent: Optional[int] = None) -> Optional[Message]:
        """Compresses old messages into 1 summary message (heuristic, no LLM).

        Keeps: all system messages + the `keep_recent` newest messages.
        The rest is summarized by HeuristicSummarizer (keeps decisions,
        action items, entities). Returns the summary message, or None when
        there's nothing worth compressing.
        """
        keep = keep_recent if keep_recent is not None else self.keep_recent
        system = [m for m in self._messages if m.role == "system"]
        rest = [m for m in self._messages if m.role != "system"]
        if len(rest) <= keep:
            return None  # history still short, no need to compress

        old, recent = rest[:-keep], rest[-keep:]
        summary_text = self.summarizer.summarize(old)
        header = (
            f"[Context summary] {len(old)} old messages compressed "
            f"at {time.strftime('%Y-%m-%d %H:%M:%S')} — originals discarded."
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
