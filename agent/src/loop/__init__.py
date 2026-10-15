"""Self-healing cho agent loop của Boom: phân loại lỗi, retry backoff, tự nén context."""
from .selfheal import (
    ContextManagerProtocol,
    ContextOverflowError,
    ErrorClassification,
    ErrorClassifier,
    FatalError,
    RecoveryAttempt,
    RetryConfig,
    RetryPolicy,
    SelfHealingFailed,
    SelfHealingLoop,
    Skipped,
    ToolError,
    TransientError,
)

__all__ = [
    "ContextManagerProtocol",
    "ContextOverflowError",
    "ErrorClassification",
    "ErrorClassifier",
    "FatalError",
    "RecoveryAttempt",
    "RetryConfig",
    "RetryPolicy",
    "SelfHealingFailed",
    "SelfHealingLoop",
    "Skipped",
    "ToolError",
    "TransientError",
]
