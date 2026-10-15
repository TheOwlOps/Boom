from typing import Any, Dict
from pydantic import BaseModel

class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

class TokenAccountant:
    """Tracks token consumption and calculates costs per session."""

    # Approximate price per 1M tokens ($ USD)
    PRICING_TABLE = {
        "gpt-4o": {"input": 2.50, "output": 10.00, "cache_read": 1.25},
        "claude-3-7-sonnet": {"input": 3.00, "output": 15.00, "cache_read": 0.30},
        "deepseek-chat": {"input": 0.14, "output": 0.28, "cache_read": 0.014},
        "default": {"input": 1.00, "output": 2.00, "cache_read": 0.50},
    }

    def __init__(self):
        self.session_usage: Dict[str, TokenUsage] = {}

    @classmethod
    def resolve_model_key(cls, model: str) -> str:
        """Map a provider-qualified model id onto a PRICING_TABLE key.

        Config carries ids like 'anthropic/claude-3.7-sonnet'; the table uses
        'claude-3-7-sonnet'. Unknown ids fall back to 'default' rates.
        """
        if not model:
            return "default"
        name = model.strip().rsplit("/", 1)[-1].lower()
        candidates = (name, name.replace(".", "-"), name.replace("-", "."))
        for candidate in candidates:
            if candidate in cls.PRICING_TABLE:
                return candidate
        for key in cls.PRICING_TABLE:
            if key != "default" and key in name:
                return key
        return "default"

    def record_usage(
        self,
        session_id: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0
    ):
        usage = self.session_usage.setdefault(session_id, TokenUsage())
        usage.input_tokens += input_tokens
        usage.output_tokens += output_tokens
        usage.cache_read_tokens += cache_read_tokens
        usage.cache_write_tokens += cache_write_tokens
        usage.reasoning_tokens += reasoning_tokens

    def record_response_usage(self, session_id: str, usage: Any) -> None:
        """Record usage from a providers.llm.LLMUsage (or any object with matching fields)."""
        if usage is None:
            return
        self.record_usage(
            session_id,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_write_tokens", 0) or 0,
            reasoning_tokens=getattr(usage, "reasoning_tokens", 0) or 0,
        )

    def get_usage(self, session_id: str) -> TokenUsage:
        return self.session_usage.get(session_id, TokenUsage())

    def calculate_cost(self, session_id: str, model: str = "default") -> float:
        usage = self.get_usage(session_id)
        rates = self.PRICING_TABLE[self.resolve_model_key(model)]

        # Reasoning tokens are billed as output tokens by every provider that reports them.
        billable_output = usage.output_tokens + usage.reasoning_tokens
        cost = (
            (usage.input_tokens / 1_000_000) * rates["input"] +
            (billable_output / 1_000_000) * rates["output"] +
            (usage.cache_read_tokens / 1_000_000) * rates.get("cache_read", 0.0)
        )
        return round(cost, 6)

    def get_total_cost(self, model: str = "default") -> float:
        if not self.session_usage:
            return 0.0
        return sum(self.calculate_cost(s, model) for s in self.session_usage)
