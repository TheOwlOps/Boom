import hashlib
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

class ContextCompressor:
    """Manages token budgets, micro-compaction of tool results, and trajectory truncation."""

    def __init__(self, scratch_dir: Optional[Path] = None, max_turn_tokens: int = 16000):
        self.scratch_dir = Path(scratch_dir) if scratch_dir else Path.cwd() / "scratch"
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        self.max_turn_tokens = max_turn_tokens

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Rough token estimate: ~4 characters per token."""
        return max(1, len(text) // 4)

    def micro_compact(self, output: str, max_chars: int = 1500) -> str:
        """Truncate large output to head+tail and spill full output to disk."""
        if len(output) <= max_chars:
            return output

        # Spill full content to disk
        out_hash = hashlib.sha256(output.encode("utf-8")).hexdigest()[:12]
        file_name = f"spill_{int(time.time())}_{out_hash}.txt"
        spill_file = self.scratch_dir / file_name
        spill_file.write_text(output, encoding="utf-8")

        head = output[:600]
        tail = output[-400:]
        omitted = len(output) - (len(head) + len(tail))

        return (
            f"{head}\n\n"
            f"[... {omitted} characters omitted; full output saved to: {spill_file.as_posix()} ...]\n\n"
            f"{tail}"
        )

    def compact_messages(self, messages: List[Dict[str, Any]], max_tokens: int = None) -> List[Dict[str, Any]]:
        """Shrink a live transcript to fit the budget without breaking tool_call pairing.

        An assistant message carrying tool_calls must keep its tool replies adjacent or the
        next request is rejected, so messages are dropped in whole exchanges and oversized
        tool output is spilled to disk before anything is dropped at all.
        """
        budget = max_tokens or self.max_turn_tokens

        compacted = []
        for m in messages:
            if m.get("role") == "tool" and len(m.get("content") or "") > 1500:
                m = {**m, "content": self.micro_compact(m["content"])}
            compacted.append(m)

        if sum(self.estimate_tokens(m.get("content") or "") for m in compacted) <= budget:
            return compacted

        # Split into exchanges: each begins at a user message and runs to the next one.
        starts = [i for i, m in enumerate(compacted) if m.get("role") == "user"]
        if len(starts) < 2:
            return compacted

        def tokens_of(chunk: List[Dict[str, Any]]) -> int:
            return sum(self.estimate_tokens(m.get("content") or "") for m in chunk)

        head = compacted[: starts[0]]  # anything before the first user message: keep
        exchanges = [compacted[s:e] for s, e in zip(starts, starts[1:] + [len(compacted)])]

        kept: List[List[Dict[str, Any]]] = []
        running = tokens_of(head)
        for exchange in reversed(exchanges):
            cost = tokens_of(exchange)
            if kept and running + cost > budget:
                break
            running += cost
            kept.insert(0, exchange)

        # At least the most recent exchange survives: a budget this tight means the caller
        # needs the history compressed by other means, not emptied.
        if not kept:
            kept = [exchanges[-1]]

        return head + [m for exchange in kept for m in exchange]
        """Ensure message array stays within token limits by pruning older messages while preserving system prompts."""
        budget = max_tokens or self.max_turn_tokens
        current_tokens = sum(self.estimate_tokens(m.get("content", "")) for m in messages)

        if current_tokens <= budget or len(messages) <= 2:
            return messages

        # Preserve first message (usually system prompt) and recent messages
        system_msg = messages[0] if messages[0].get("role") == "system" else None
        non_system = messages[1:] if system_msg else messages[:]

        while len(non_system) > 2 and current_tokens > budget:
            removed = non_system.pop(0)
            current_tokens -= self.estimate_tokens(removed.get("content", ""))

        if system_msg:
            return [system_msg] + non_system
        return non_system
