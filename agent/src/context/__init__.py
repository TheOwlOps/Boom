"""Context window management for Boom.

- ContextManager: adds messages, counts tokens (EN ~4 chars/token, VI ~2.5),
  get_context() fitting the budget (system + newest first), prune() dropping
  the least important messages, compress() summarizing old messages into 1.
- HeuristicSummarizer: extracts decisions / action items / entities (URL, file,
  proper names) without an LLM; output is a bullet list (Vietnamese, pinned).
- Message: pydantic model for one conversation message.
"""
from .manager import ContextManager, Message, estimate_tokens
from .summarizer import HeuristicSummarizer

__all__ = ["ContextManager", "HeuristicSummarizer", "Message", "estimate_tokens"]
