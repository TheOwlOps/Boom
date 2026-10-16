"""Self-healing for the Boom agent loop: error classification, backoff retry, context compression."""
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
