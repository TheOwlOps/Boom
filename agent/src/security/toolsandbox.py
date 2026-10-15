# -*- coding: utf-8 -*-
"""Tool Execution Sandbox: boc moi tool call bang validate + rate limit + timeout.

Lop chinh:
- ``ToolPolicy``: cau hinh per-tool {timeout, rate_limit, require_approval}.
- ``SandboxedToolRunner``: wrap moi tool call theo trinh tu:
    1. validate args bang ``security.sanitize.check_tool_args``
       (chan prompt injection / path traversal / ky tu dieu khien),
    2. neu policy ``require_approval`` -> tra ve trang thai cho duyet,
       KHONG thuc thi tool,
    3. check rate limit bang ``security.ratelimit.UserRateLimiter``
       (key rieng theo tung tool + user),
    4. chay tool trong daemon thread voi timeout (mac dinh 60s),
    5. catch moi exception -> tra ve dict loi, khong bao gio crash caller.

Ket qua tra ve luon la dict co ``status`` la mot trong:
``"ok" | "error" | "timeout" | "blocked" | "rate_limited" | "approval_required"``.

Chi dung stdlib + pydantic. Import theo style repo (``from security.xxx``)
de khi copy file vao ``agent/src/security/`` thi chay ngay khong can sua.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from security.ratelimit import RateLimitError, UserRateLimiter
from security.sanitize import check_tool_args

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hang so mac dinh
# ---------------------------------------------------------------------------
# Timeout mac dinh cho moi tool call (giay).
DEFAULT_TIMEOUT: float = 60.0

# Rate limit mac dinh khi policy khong cau hinh rieng:
# burst 120 call, nap lai 2 token/giay (~120 call/phut neu goi deu).
DEFAULT_RATE_LIMIT: Dict[str, float] = {"capacity": 120.0, "refill": 2.0}


# ---------------------------------------------------------------------------
# ToolPolicy
# ---------------------------------------------------------------------------
class ToolPolicy(BaseModel):
    """Cau hinh sandbox cho 1 tool cu the.

    - ``timeout``: so giay toi da cho 1 lan chay (het gio thi bo cho,
      khong doi nua).
    - ``rate_limit``: dict ``{"capacity": float, "refill": float}`` theo
      kieu token bucket cua ``UserRateLimiter``; ``None`` = dung default
      cua runner.
    - ``require_approval``: True -> runner khong chay tool ma tra ve
      ``status="approval_required"`` de tang approval xu ly tiep.
    """

    tool_name: str = Field(min_length=1)
    timeout: float = Field(default=DEFAULT_TIMEOUT, gt=0, le=3600)
    rate_limit: Optional[Dict[str, float]] = Field(default=None)
    require_approval: bool = False

    @model_validator(mode="after")
    def _validate_rate_limit(self) -> "ToolPolicy":
        """Dam bao rate_limit (neu co) co capacity/refill hop le."""
        if self.rate_limit is not None:
            cap = self.rate_limit.get("capacity", 0)
            refill = self.rate_limit.get("refill", 0)
            if not isinstance(cap, (int, float)) or cap <= 0:
                raise ValueError("rate_limit['capacity'] phai la so > 0")
            if not isinstance(refill, (int, float)) or refill <= 0:
                raise ValueError("rate_limit['refill'] phai la so > 0")
        return self


# ---------------------------------------------------------------------------
# Chay ham voi timeout bang thread
# ---------------------------------------------------------------------------
def _call_with_timeout(
    func: Callable, args: Dict[str, Any], timeout: float
) -> Dict[str, Any]:
    """Chay ``func(**args)`` trong daemon thread, doi toi da ``timeout`` giay.

    Tra ve dict ``{"timed_out": bool, "value": ..., "exc": ...}``.
    - Neu het timeout: ``timed_out=True``. Python khong kill duoc thread
      dang chay nen thread bi "bo roi" — nhung vi la daemon thread, no se
      tu ket thuc khi func xong va khong bao gio chan tien trinh chinh.
    - Neu func raise: exception duoc giu trong ``exc`` de main thread
      xu ly thong nhat (khong de crash thread con lam mat stack trace).
    """
    box: Dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = func(**args)
        except BaseException as exc:  # noqa: BLE001 — giu lai, xu ly o main
            box["exc"] = exc

    t = threading.Thread(target=_target, daemon=True, name="tool-sandbox")
    t.start()
    t.join(timeout)
    if t.is_alive():
        return {"timed_out": True, "value": None, "exc": None}
    return {"timed_out": False, "value": box.get("value"), "exc": box.get("exc")}


# ---------------------------------------------------------------------------
# SandboxedToolRunner
# ---------------------------------------------------------------------------
class SandboxedToolRunner:
    """Wrap moi tool call: validate -> approval gate -> rate limit -> timeout.

    Args:
        policies: list ``ToolPolicy`` cho tung tool; tool khong co policy
            rieng thi dung cau hinh mac dinh.
        default_timeout: timeout ap dung cho tool khong co policy.
        default_rate_limit: dict ``{"capacity", "refill"}`` mac dinh.
        audit_log: optional ``ToolAuditLog`` — neu co, moi call (ke ca
            blocked/timeout/error) deu duoc ghi audit. Audit hong khong
            bao gio lam hong tool call (fail-open).
    """

    def __init__(
        self,
        policies: Optional[List[ToolPolicy]] = None,
        default_timeout: float = DEFAULT_TIMEOUT,
        default_rate_limit: Optional[Dict[str, float]] = None,
        audit_log: Any = None,
    ) -> None:
        if default_timeout <= 0:
            raise ValueError("default_timeout phai > 0")
        self._policies: Dict[str, ToolPolicy] = {}
        for p in policies or []:
            if not isinstance(p, ToolPolicy):
                raise TypeError(
                    "policies phai la list ToolPolicy, nhan duoc %s"
                    % type(p).__name__
                )
            self._policies[p.tool_name] = p
        self._default_timeout = float(default_timeout)
        self._default_rate_limit = dict(default_rate_limit or DEFAULT_RATE_LIMIT)
        # Moi tool co 1 UserRateLimiter rieng (key = "tool:user").
        self._limiters: Dict[str, UserRateLimiter] = {}
        self._lock = threading.Lock()
        self._audit_log = audit_log

    # ------------------------------------------------------------ internal
    def _policy_for(self, tool_name: str) -> ToolPolicy:
        """Policy cua tool; tool la thi dung policy mac dinh tam thoi."""
        policy = self._policies.get(tool_name)
        if policy is not None:
            return policy
        return ToolPolicy(
            tool_name=tool_name,
            timeout=self._default_timeout,
            rate_limit=dict(self._default_rate_limit),
        )

    def _limiter_for(self, tool_name: str, policy: ToolPolicy) -> UserRateLimiter:
        """Lay (hoac tao) UserRateLimiter rieng cho 1 tool — thread-safe."""
        with self._lock:
            limiter = self._limiters.get(tool_name)
            if limiter is None:
                cfg = policy.rate_limit or self._default_rate_limit
                limiter = UserRateLimiter(
                    capacity=float(cfg.get("capacity", 120.0)),
                    refill=float(cfg.get("refill", 2.0)),
                )
                self._limiters[tool_name] = limiter
            return limiter

    # ---------------------------------------------------------------- api
    def run(
        self,
        tool_name: str,
        func: Callable,
        args: Optional[Dict[str, Any]],
        user: str,
    ) -> Dict[str, Any]:
        """Chay 1 tool call trong sandbox. Khong bao gio raise.

        Tra ve dict luon co: ``status``, ``tool_name``, ``user``,
        ``duration_ms``. Tuy ``status`` ma co them:
        - ``"ok"``: them ``result``.
        - ``"error"`` / ``"timeout"`` / ``"blocked"`` / ``"rate_limited"`` /
          ``"approval_required"``: them ``error`` (str) va field phu
          (``error_type`` / ``timeout`` / ``warnings`` / ``retry_after``).
        """
        started = time.perf_counter()
        result = self._run_inner(tool_name, func, args, user, started)
        # Hook audit: fail-open — audit hong thi chi log warning.
        if self._audit_log is not None:
            try:
                self._audit_log.log_call(
                    user=str(user),
                    tool_name=str(tool_name),
                    args=args if isinstance(args, dict) else {},
                    result=result.get("result", result.get("error", "")),
                    duration_ms=result.get("duration_ms", 0.0),
                    success=(result.get("status") == "ok"),
                )
            except Exception as exc:  # noqa: BLE001 — audit khong duoc pha tool
                _log.warning("ghi audit that bai (fail-open): %r", exc)
        return result

    def _run_inner(
        self,
        tool_name: str,
        func: Callable,
        args: Optional[Dict[str, Any]],
        user: str,
        started: float,
    ) -> Dict[str, Any]:
        def _done(status: str, **kw: Any) -> Dict[str, Any]:
            kw.update(
                {
                    "status": status,
                    "tool_name": tool_name,
                    "user": user,
                    "duration_ms": (time.perf_counter() - started) * 1000.0,
                }
            )
            return kw

        # 0. Kiem tra kieu dau vao co ban (tra ve dict loi, khong raise).
        if not isinstance(tool_name, str) or not tool_name:
            return _done(
                "error",
                error="tool_name phai la str khac rong",
                error_type="TypeError",
            )
        if not callable(func):
            return _done(
                "error", error="func phai la callable", error_type="TypeError"
            )
        safe_args = args if isinstance(args, dict) else {}
        policy = self._policy_for(tool_name)

        # 1. Validate args (sanitize: injection / path traversal / control char).
        try:
            validation = check_tool_args(tool_name, safe_args)
        except Exception as exc:  # noqa: BLE001 — validate hong thi chan
            return _done(
                "blocked",
                error="validate args that bai: %s" % (exc,),
                warnings=[str(exc)],
            )
        if not validation.get("ok", False):
            return _done(
                "blocked",
                error="args khong dat validation",
                warnings=list(validation.get("warnings", [])),
            )
        clean_args = validation.get("sanitized_args", safe_args)

        # 2. Approval gate: tool nhay cam thi khong chay, doi duyet.
        if policy.require_approval:
            return _done(
                "approval_required",
                error="tool '%s' yeu cau approval truoc khi chay" % tool_name,
            )

        # 3. Rate limit theo cap (tool, user).
        limiter = self._limiter_for(tool_name, policy)
        try:
            limiter.allow_or_raise("%s:%s" % (tool_name, user))
        except RateLimitError as exc:
            return _done(
                "rate_limited", error=str(exc), retry_after=exc.retry_after
            )

        # 4. Thuc thi voi timeout; 5. catch moi exception -> dict loi.
        try:
            outcome = _call_with_timeout(func, clean_args, policy.timeout)
        except Exception as exc:  # noqa: BLE001 — phong thu tang ngoai
            return _done(
                "error",
                error="%s: %s" % (type(exc).__name__, exc),
                error_type=type(exc).__name__,
            )
        if outcome["timed_out"]:
            return _done(
                "timeout",
                error="tool chay qua %ss — da dung doi" % (policy.timeout,),
                timeout=policy.timeout,
            )
        if outcome["exc"] is not None:
            exc = outcome["exc"]
            return _done(
                "error",
                error="%s: %s" % (type(exc).__name__, exc),
                error_type=type(exc).__name__,
            )
        return _done("ok", result=outcome["value"])
