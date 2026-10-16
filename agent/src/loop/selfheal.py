"""Self-healing for the Boom agent loop.

This module wraps every execution step (LLM call, tool call) in an automatic
recovery layer: classify the error -> decide retry / compress context /
skip / abort immediately.

Stdlib + pydantic only, no dependency on the rest of Boom, so it can be
tested standalone.
"""
from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional, Protocol

from pydantic import BaseModel, Field

log = logging.getLogger("boom.selfheal")


# ---------------------------------------------------------------------------
# Exception markers: let Boom code actively label errors when raising.
# ---------------------------------------------------------------------------
class FatalError(Exception):
    """Critical error (auth/config): no retry, abort immediately."""


class TransientError(Exception):
    """Temporary error (network/timeout/rate limit): retry with backoff."""


class ToolError(Exception):
    """Tool execution error: retry once then skip, never crash the loop."""


class ContextOverflowError(Exception):
    """Context overflow: needs context compression before retrying."""


class SelfHealingFailed(Exception):
    """Wraps the original error after all recovery options are exhausted."""

    def __init__(self, message: str, *, category: str, attempts: int,
                 original: BaseException):
        super().__init__(message)
        self.category = category
        self.attempts = attempts
        self.original = original


class Skipped(Exception):
    """Marks a tool step skipped after failed retries (never raised)."""


# ---------------------------------------------------------------------------
# Models (pydantic)
# ---------------------------------------------------------------------------
class ErrorClassification(BaseModel):
    """The result of classifying one error."""

    category: str = Field(pattern=r"^(transient|tool_error|fatal|context_overflow)$")
    retryable: bool
    backoff_sec: int  # suggested backoff for the first retry


class RetryConfig(BaseModel):
    """Retry policy configuration."""

    base_delay: float = Field(default=1.0, ge=0)
    max_delay: float = Field(default=60.0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    jitter: float = Field(default=0.5, ge=0)


class RecoveryAttempt(BaseModel):
    """One record in the recovery journal (every retry attempt is logged)."""

    timestamp: float = Field(default_factory=time.time)
    func_name: str
    category: str
    attempt: int  # retries already done before this run (0 = first run failed)
    delay_sec: float
    action: str  # retry | skip | raise | compress+retry
    error: str


class ContextManagerProtocol(Protocol):
    """Minimum ContextManager interface needed for auto-compress on overflow."""

    def compress(self, keep_recent: Optional[int] = None) -> Any: ...


# ---------------------------------------------------------------------------
# 1. ErrorClassifier
# ---------------------------------------------------------------------------
class ErrorClassifier:
    """Classifies errors into 4 groups to pick a recovery strategy."""

    # Message-based detection (priority: overflow > fatal > transient).
    _OVERFLOW_RE = re.compile(
        r"(context|token).*(length|limit|overflow|exceed|too (long|many))|"
        r"max(imum)?[_ ]?tokens|context_length_exceeded|"
        r"request too large|prompt is too long",
        re.IGNORECASE,
    )
    _FATAL_RE = re.compile(
        r"(api[_ ]?key|invalid[_ ]?(token|key|credential)|unauthori[sz]ed|"
        r"forbidden|access denied|permission denied|"
        r"\b401\b|\b403\b|"
        r"config(uration)? (error|missing|invalid)|missing config|bad config|"
        r"no such (file|module|package)|importerror|modulenotfound)",
        re.IGNORECASE,
    )
    _TRANSIENT_RE = re.compile(
        r"(timeout|timed out|temporar|econnreset|econnrefused|connection|"
        r"network|socket|dns|broken pipe|remote disconnected|"
        r"rate[_ ]?limit|too many requests|\b429\b|\b502\b|\b503\b|\b504\b|"
        r"service (unavailable|temporarily)|overloaded|try again)",
        re.IGNORECASE,
    )
    # Exception types that are always transient regardless of message.
    _TRANSIENT_TYPES = (TimeoutError, ConnectionError)

    def classify(self, error: BaseException) -> ErrorClassification:
        # 0) Marker exceptions actively raised by Boom code -> honor the label.
        if isinstance(error, ContextOverflowError):
            return ErrorClassification(category="context_overflow",
                                       retryable=False, backoff_sec=0)
        if isinstance(error, FatalError):
            return ErrorClassification(category="fatal",
                                       retryable=False, backoff_sec=0)
        if isinstance(error, TransientError):
            return ErrorClassification(category="transient",
                                       retryable=True, backoff_sec=2)
        if isinstance(error, ToolError):
            return ErrorClassification(category="tool_error",
                                       retryable=True, backoff_sec=1)

        msg = f"{type(error).__name__}: {error}"

        # 1) Context overflow: no blind retry, must compress first.
        if self._OVERFLOW_RE.search(msg):
            return ErrorClassification(category="context_overflow",
                                       retryable=False, backoff_sec=0)
        # 2) Fatal: broken auth/config -> retrying is pointless, abort now.
        if self._FATAL_RE.search(msg):
            return ErrorClassification(category="fatal",
                                       retryable=False, backoff_sec=0)
        # 3) Transient: network/timeout/rate-limit -> retry with backoff.
        if isinstance(error, self._TRANSIENT_TYPES) or self._TRANSIENT_RE.search(msg):
            return ErrorClassification(category="transient",
                                       retryable=True, backoff_sec=2)
        # 4) Default: treat as a tool failure -> retry once then skip.
        return ErrorClassification(category="tool_error",
                                   retryable=True, backoff_sec=1)


# ---------------------------------------------------------------------------
# 2. RetryPolicy: exponential backoff + jitter
# ---------------------------------------------------------------------------
class RetryPolicy:
    """delay = min(base * 2^attempt + random_jitter, max_delay)."""

    def __init__(self, config: Optional[RetryConfig] = None,
                 rng: Optional[random.Random] = None):
        self.config = config or RetryConfig()
        self._rng = rng or random.Random()

    def delay_for(self, attempt: int) -> float:
        """Wait time before retry number `attempt` (attempt=0 is the first retry)."""
        jitter = self._rng.uniform(0, self.config.jitter)
        return min(self.config.base_delay * (2 ** attempt) + jitter,
                   self.config.max_delay)

    def should_retry(self, category: str, attempt: int,
                     max_attempts: Optional[int] = None) -> bool:
        """attempt = retries already done. Returns True when another retry is allowed."""
        if category == "fatal":
            return False  # fatal: abort immediately, no retry
        if category == "tool_error":
            return attempt < 1  # exactly 1 retry, then skip
        if category == "context_overflow":
            return attempt < 1  # 1 retry after compressing
        if category == "transient":
            cap = max_attempts if max_attempts is not None else self.config.max_attempts
            return attempt < cap
        return False


# ---------------------------------------------------------------------------
# 3. SelfHealingLoop: runs a func with automatic recovery
# ---------------------------------------------------------------------------
class SelfHealingLoop:
    """Wraps any callable: error -> classify -> retry / compress / skip / raise."""

    def __init__(
        self,
        context_manager: Optional[ContextManagerProtocol] = None,
        retry_config: Optional[RetryConfig] = None,
        on_recovery: Optional[Callable[[RecoveryAttempt], None]] = None,
        sleep: Callable[[float], None] = time.sleep,
        skip_on_tool_error: bool = True,
        rng: Optional[random.Random] = None,
    ):
        self.classifier = ErrorClassifier()
        self.policy = RetryPolicy(retry_config, rng=rng)
        self.context_manager = context_manager
        self.on_recovery = on_recovery
        self.sleep = sleep  # injectable so tests don't really wait
        self.skip_on_tool_error = skip_on_tool_error
        self.recovery_log: List[RecoveryAttempt] = []

    # -- overridable hook ---------------------------------------------------
    def on_context_overflow(self, context_manager: ContextManagerProtocol) -> None:
        """Auto-compresses the context when overflow is detected."""
        context_manager.compress()

    # -- main API -------------------------------------------------------------
    def execute_with_recovery(
        self,
        func: Callable[..., Any],
        *args: Any,
        max_attempts: Optional[int] = None,
        context_manager: Optional[ContextManagerProtocol] = None,
        **kwargs: Any,
    ) -> Any:
        """Runs func; on error classifies it and retries / compresses / skips / raises.

        - transient: retries up to max_attempts with growing backoff.
        - context_overflow: compresses then retries once.
        - tool_error: retries once then skips (returns None) instead of
          crashing the loop.
        - fatal: aborts immediately, raises SelfHealingFailed.
        """
        cm = context_manager if context_manager is not None else self.context_manager
        attempt = 0  # retries done so far
        func_name = getattr(func, "__name__", type(func).__name__)

        while True:
            try:
                return func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - the classifier handles all errors
                cls = self.classifier.classify(exc)
                category = cls.category

                # Context overflow: compress before deciding to retry.
                if category == "context_overflow":
                    if cm is None:
                        self._log(func_name, category, attempt, 0.0, "raise", exc)
                        raise SelfHealingFailed(
                            f"Context overflow with no ContextManager to compress "
                            f"({func_name})",
                            category=category, attempts=attempt, original=exc,
                        ) from exc
                    self.on_context_overflow(cm)
                    if self.policy.should_retry(category, attempt, max_attempts):
                        self._log(func_name, category, attempt, 0.0,
                                  "compress+retry", exc)
                        attempt += 1
                        continue
                    self._log(func_name, category, attempt, 0.0, "raise", exc)
                    raise SelfHealingFailed(
                        f"Context still overflowing after compression ({func_name})",
                        category=category, attempts=attempt, original=exc,
                    ) from exc

                # Fatal: abort immediately, no retry.
                if category == "fatal":
                    self._log(func_name, category, attempt, 0.0, "raise", exc)
                    raise SelfHealingFailed(
                        f"Fatal error, aborting immediately ({func_name}): {exc}",
                        category=category, attempts=attempt, original=exc,
                    ) from exc

                # tool_error out of retries: skip instead of raising (loop survives).
                if (category == "tool_error" and self.skip_on_tool_error
                        and not self.policy.should_retry(category, attempt,
                                                         max_attempts)):
                    self._log(func_name, category, attempt, 0.0, "skip", exc)
                    log.warning("selfheal: skipping %s after %d attempt(s) (tool_error): %s",
                                func_name, attempt + 1, exc)
                    return None

                # Out of retries for the rest: escalate.
                if not self.policy.should_retry(category, attempt, max_attempts):
                    self._log(func_name, category, attempt, 0.0, "raise", exc)
                    raise SelfHealingFailed(
                        f"Out of retries ({attempt}) for {func_name}: {exc}",
                        category=category, attempts=attempt, original=exc,
                    ) from exc

                # Retries left: back off, then retry.
                delay = self.policy.delay_for(attempt)
                self._log(func_name, category, attempt, delay, "retry", exc)
                log.info("selfheal: retrying %s (attempt %d) after %.2fs (%s): %s",
                         func_name, attempt + 1, delay, category, exc)
                self.sleep(delay)
                attempt += 1

    # -- utilities ------------------------------------------------------------
    def _log(self, func_name: str, category: str, attempt: int, delay: float,
             action: str, exc: BaseException) -> None:
        entry = RecoveryAttempt(
            func_name=func_name,
            category=category,
            attempt=attempt,
            delay_sec=round(delay, 3),
            action=action,
            error=f"{type(exc).__name__}: {exc}",
        )
        self.recovery_log.append(entry)
        if self.on_recovery is not None:
            self.on_recovery(entry)

    def recovery_summary(self) -> Dict[str, Any]:
        """Quick stats of recorded recoveries."""
        by_action: Dict[str, int] = {}
        for e in self.recovery_log:
            by_action[e.action] = by_action.get(e.action, 0) + 1
        return {"total": len(self.recovery_log), "by_action": by_action}
