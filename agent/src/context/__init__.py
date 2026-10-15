"""Quản lý context window cho Boom.

- ContextManager: thêm message, đếm token (EN ~4 chars/token, VI ~2.5),
  get_context() vừa budget (ưu tiên system + mới nhất), prune() xóa message
  ít quan trọng nhất, compress() tóm tắt messages cũ thành 1 summary.
- HeuristicSummarizer: trích decisions / action items / entities (URL, file,
  tên riêng) không cần LLM, output bullet list tiếng Việt.
- Message: model pydantic cho một message trong hội thoại.
"""
from .manager import ContextManager, Message, estimate_tokens
from .summarizer import HeuristicSummarizer

__all__ = ["ContextManager", "HeuristicSummarizer", "Message", "estimate_tokens"]
