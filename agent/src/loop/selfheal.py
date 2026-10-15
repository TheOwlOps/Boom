"""Self-healing cho agent loop của Boom.

Module này bọc mọi bước chạy (LLM call, tool call) bằng một lớp phục hồi tự động:
phân loại lỗi -> quyết định retry / compress context / skip / dừng ngay.

Chỉ dùng stdlib + pydantic, không phụ thuộc phần còn lại của Boom nên có thể
test độc lập.
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
# Exception marker: cho phép code Boom chủ động gắn nhãn lỗi khi raise.
# ---------------------------------------------------------------------------
class FatalError(Exception):
    """Lỗi nghiêm trọng (auth/config): không retry, dừng ngay."""


class TransientError(Exception):
    """Lỗi tạm thời (network/timeout/rate limit): retry với backoff."""


class ToolError(Exception):
    """Tool chạy lỗi: retry 1 lần rồi skip, không làm sập loop."""


class ContextOverflowError(Exception):
    """Context tràn: cần nén context rồi thử lại."""


class SelfHealingFailed(Exception):
    """Gói lỗi gốc sau khi đã hết mọi phương án phục hồi."""

    def __init__(self, message: str, *, category: str, attempts: int,
                 original: BaseException):
        super().__init__(message)
        self.category = category
        self.attempts = attempts
        self.original = original


class Skipped(Exception):
    """Đánh dấu một bước tool bị skip sau khi retry thất bại (không raise)."""


# ---------------------------------------------------------------------------
# Models (pydantic)
# ---------------------------------------------------------------------------
class ErrorClassification(BaseModel):
    """Kết quả phân loại một lỗi."""

    category: str = Field(pattern=r"^(transient|tool_error|fatal|context_overflow)$")
    retryable: bool
    backoff_sec: int  # backoff gợi ý cho lần retry đầu tiên


class RetryConfig(BaseModel):
    """Cấu hình chính sách retry."""

    base_delay: float = Field(default=1.0, ge=0)
    max_delay: float = Field(default=60.0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    jitter: float = Field(default=0.5, ge=0)


class RecoveryAttempt(BaseModel):
    """Một bản ghi trong nhật ký phục hồi (mọi lần thử lại đều log)."""

    timestamp: float = Field(default_factory=time.time)
    func_name: str
    category: str
    attempt: int  # số retry đã thực hiện trước lần này (0 = lần chạy đầu lỗi)
    delay_sec: float
    action: str  # retry | skip | raise | compress+retry
    error: str


class ContextManagerProtocol(Protocol):
    """Interface tối thiểu cần của ContextManager để tự nén khi tràn."""

    def compress(self, keep_recent: Optional[int] = None) -> Any: ...


# ---------------------------------------------------------------------------
# 1. ErrorClassifier
# ---------------------------------------------------------------------------
class ErrorClassifier:
    """Phân loại lỗi thành 4 nhóm để quyết định chiến lược phục hồi."""

    # Pattern nhận diện theo message (thứ tự ưu tiên: overflow > fatal > transient).
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
    # Các exception type luôn là transient dù message không khớp pattern.
    _TRANSIENT_TYPES = (TimeoutError, ConnectionError)

    def classify(self, error: BaseException) -> ErrorClassification:
        # 0) Marker exception do code Boom chủ động raise -> tôn trọng nhãn.
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

        # 1) Tràn context: không retry "chay", phải compress trước.
        if self._OVERFLOW_RE.search(msg):
            return ErrorClassification(category="context_overflow",
                                       retryable=False, backoff_sec=0)
        # 2) Fatal: auth/config hỏng -> retry cũng vô ích, dừng ngay.
        if self._FATAL_RE.search(msg):
            return ErrorClassification(category="fatal",
                                       retryable=False, backoff_sec=0)
        # 3) Transient: mạng/timeout/rate-limit -> retry với backoff.
        if isinstance(error, self._TRANSIENT_TYPES) or self._TRANSIENT_RE.search(msg):
            return ErrorClassification(category="transient",
                                       retryable=True, backoff_sec=2)
        # 4) Mặc định: coi như tool fail -> retry 1 lần rồi skip.
        return ErrorClassification(category="tool_error",
                                   retryable=True, backoff_sec=1)


# ---------------------------------------------------------------------------
# 2. RetryPolicy: exponential backoff + jitter
# ---------------------------------------------------------------------------
class RetryPolicy:
    """delay = min(base * 2^attempt + jitter_ngẫu_nhiên, max_delay)."""

    def __init__(self, config: Optional[RetryConfig] = None,
                 rng: Optional[random.Random] = None):
        self.config = config or RetryConfig()
        self._rng = rng or random.Random()

    def delay_for(self, attempt: int) -> float:
        """Tính thời gian chờ trước lần retry thứ `attempt` (attempt=0 là retry đầu)."""
        jitter = self._rng.uniform(0, self.config.jitter)
        return min(self.config.base_delay * (2 ** attempt) + jitter,
                   self.config.max_delay)

    def should_retry(self, category: str, attempt: int,
                     max_attempts: Optional[int] = None) -> bool:
        """attempt = số retry đã thực hiện. Trả về True nếu được retry thêm."""
        if category == "fatal":
            return False  # fatal: dừng ngay, không retry
        if category == "tool_error":
            return attempt < 1  # retry đúng 1 lần rồi skip
        if category == "context_overflow":
            return attempt < 1  # compress xong retry 1 lần
        if category == "transient":
            cap = max_attempts if max_attempts is not None else self.config.max_attempts
            return attempt < cap
        return False


# ---------------------------------------------------------------------------
# 3. SelfHealingLoop: wrapper chạy func với tự phục hồi
# ---------------------------------------------------------------------------
class SelfHealingLoop:
    """Bọc mọi callable: lỗi -> classify -> retry / compress / skip / raise."""

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
        self.sleep = sleep  # inject được để test không phải chờ thật
        self.skip_on_tool_error = skip_on_tool_error
        self.recovery_log: List[RecoveryAttempt] = []

    # -- hook có thể override -------------------------------------------------
    def on_context_overflow(self, context_manager: ContextManagerProtocol) -> None:
        """Tự động nén context khi phát hiện tràn."""
        context_manager.compress()

    # -- API chính ------------------------------------------------------------
    def execute_with_recovery(
        self,
        func: Callable[..., Any],
        *args: Any,
        max_attempts: Optional[int] = None,
        context_manager: Optional[ContextManagerProtocol] = None,
        **kwargs: Any,
    ) -> Any:
        """Chạy func; nếu lỗi thì phân loại và retry / compress / skip / raise.

        - transient: retry tới max_attempts với backoff tăng dần.
        - context_overflow: gọi compress() rồi retry 1 lần.
        - tool_error: retry 1 lần rồi skip (trả None) thay vì làm sập loop.
        - fatal: dừng ngay, raise SelfHealingFailed.
        """
        cm = context_manager if context_manager is not None else self.context_manager
        attempt = 0  # số retry đã thực hiện
        func_name = getattr(func, "__name__", type(func).__name__)

        while True:
            try:
                return func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - classifier xử lý mọi lỗi
                cls = self.classifier.classify(exc)
                category = cls.category

                # Tràn context: nén trước rồi mới quyết định retry.
                if category == "context_overflow":
                    if cm is None:
                        self._log(func_name, category, attempt, 0.0, "raise", exc)
                        raise SelfHealingFailed(
                            f"Context overflow nhưng không có ContextManager để nén "
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
                        f"Context overflow vẫn còn sau khi nén ({func_name})",
                        category=category, attempts=attempt, original=exc,
                    ) from exc

                # Fatal: dừng ngay, không retry.
                if category == "fatal":
                    self._log(func_name, category, attempt, 0.0, "raise", exc)
                    raise SelfHealingFailed(
                        f"Lỗi nghiêm trọng, dừng ngay ({func_name}): {exc}",
                        category=category, attempts=attempt, original=exc,
                    ) from exc

                # tool_error hết retry: skip thay vì raise (không sập loop).
                if (category == "tool_error" and self.skip_on_tool_error
                        and not self.policy.should_retry(category, attempt,
                                                         max_attempts)):
                    self._log(func_name, category, attempt, 0.0, "skip", exc)
                    log.warning("selfheal: skip %s sau %d lần thử (tool_error): %s",
                                func_name, attempt + 1, exc)
                    return None

                # Hết lượt retry cho các loại còn lại: escalate.
                if not self.policy.should_retry(category, attempt, max_attempts):
                    self._log(func_name, category, attempt, 0.0, "raise", exc)
                    raise SelfHealingFailed(
                        f"Hết lượt retry ({attempt} lần) cho {func_name}: {exc}",
                        category=category, attempts=attempt, original=exc,
                    ) from exc

                # Còn lượt: backoff rồi retry.
                delay = self.policy.delay_for(attempt)
                self._log(func_name, category, attempt, delay, "retry", exc)
                log.info("selfheal: retry %s lần %d sau %.2fs (%s): %s",
                         func_name, attempt + 1, delay, category, exc)
                self.sleep(delay)
                attempt += 1

    # -- tiện ích --------------------------------------------------------------
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
        """Thống kê nhanh các lần phục hồi đã ghi."""
        by_action: Dict[str, int] = {}
        for e in self.recovery_log:
            by_action[e.action] = by_action.get(e.action, 0) + 1
        return {"total": len(self.recovery_log), "by_action": by_action}
