"""Tools nhóm security: quét secret / sanitize input / rate limit.

Wrap các module có sẵn trong repo (không copy logic):
  - ``agent/src/security/secretscan.py`` -> secret_scan
  - ``agent/src/security/sanitize.py``    -> sanitize_input
  - ``agent/src/security/ratelimit.py``   -> check_rate_limit

Import theo kiểu try/except: module nào thiếu thì tool đó trả {"error"}.
"""

from typing import Any, Dict, List, Optional

__all__ = [
    "secret_scan",
    "sanitize_input",
    "check_rate_limit",
    "build_tools",
]

try:
    from security import secretscan as _secretscan
except ImportError:
    _secretscan = None  # type: ignore[assignment]

try:
    from security import sanitize as _sanitize
except ImportError:
    _sanitize = None  # type: ignore[assignment]

try:
    from security.ratelimit import UserRateLimiter as _UserRateLimiter
    from security.ratelimit import RateLimitError as _RateLimitError
except ImportError:
    _UserRateLimiter = None  # type: ignore[assignment]
    _RateLimitError = None  # type: ignore[assignment]


def _need(mod, name: str):
    if mod is None:
        raise RuntimeError(
            f"module gốc cho tool {name!r} không khả dụng trong môi trường này"
        )
    return mod


# ------------------------------------------------------------------ tools
def secret_scan(target: str, diff: bool = False) -> Dict[str, Any]:
    """Quét secret bị lộ trong file / thư mục / unified diff.

    target: đường dẫn file, thư mục, hoặc nội dung diff (khi diff=True).
    Trả về {target, findings: [{line, type, preview (đã che)}], has_secrets}.
    preview đã che 80% giá trị — an toàn để log/show cho agent.
    """
    mod = _need(_secretscan, "secret_scan")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("target phải là string không rỗng")
    target = target.strip()
    if diff:
        findings = mod.scan_diff(target)
    else:
        from pathlib import Path
        p = Path(target)
        if p.is_dir():
            findings = mod.scan_dir(p)
        else:
            findings = mod.scan_file(p)
    return {
        "target": target,
        "findings": findings,
        "has_secrets": mod.has_secrets(findings),
        "count": len(findings),
    }


def sanitize_input(
    tool_name: str,
    args: Dict[str, Any],
    allowed_roots: Optional[List[str]] = None,
    max_str_len: int = 100_000,
) -> Dict[str, Any]:
    """Sanitize dict args trước khi gọi 1 tool (dùng security/sanitize.py).

    Mỗi giá trị string: strip ký tự điều khiển (giữ \\n\\t) -> truncate về
    max_str_len -> phát hiện prompt injection. Key là path thì validate
    chống path traversal theo allowed_roots (không truyền = bỏ qua bước này).

    Trả về {ok, sanitized_args, warnings}. ok=False chỉ khi có injection hoặc
    path traversal — trường hợp đó agent KHÔNG được gọi tool với args này.
    """
    mod = _need(_sanitize, "sanitize_input")
    if not isinstance(args, dict):
        raise ValueError("args phải là dict")
    try:
        max_len = int(max_str_len)
    except (TypeError, ValueError):
        max_len = 100_000
    return mod.check_tool_args(
        tool_name or "unknown",
        args,
        allowed_roots=list(allowed_roots or []),
        max_str_len=max_len,
    )


# Rate limiter dùng chung cho tool (token bucket theo key).
# Giữ 2 instance module-level: default và strict — tạo mới mỗi lần gọi sẽ
# làm mất trạng thái bucket nên strict cũng phải dùng chung 1 instance.
_limiter = None
_limiter_strict = None


def _get_limiter(strict: bool = False):
    global _limiter, _limiter_strict
    cls = _need(_UserRateLimiter, "check_rate_limit")
    if strict:
        if _limiter_strict is None:
            _limiter_strict = cls(**cls.PRESET_STRICT)
        return _limiter_strict
    if _limiter is None:
        _limiter = cls(**cls.PRESET_DEFAULT)
    return _limiter


def check_rate_limit(key: str, strict: bool = False) -> Dict[str, Any]:
    """Kiểm tra rate limit cho 1 key (user/channel/tool).

    Trả về {key, allowed, retry_after}. allowed=False nghĩa là key đã hết
    token — agent nên chờ retry_after giây rồi thử lại, không spam.
    strict=True dùng preset STRICT (cho tool nguy hiểm: gửi tin, xóa...).
    Không raise: vượt limit chỉ trả allowed=False.
    """
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key phải là string không rỗng")
    key = key.strip()
    limiter = _get_limiter(strict=bool(strict))
    allowed = limiter.allow(key)
    retry_after = 0.0
    if not allowed:
        state = limiter.get_state(key)
        # get_state trả {"tokens", ...}: retry_after ~ thời gian nạp lại 1 token
        tokens = float(state.get("tokens", 0.0) or 0.0)
        refill = float(getattr(limiter, "refill", 0.5) or 0.5)
        retry_after = max(0.0, (1.0 - tokens) / refill)
    return {"key": key, "allowed": allowed,
            "retry_after": round(retry_after, 2), "strict": bool(strict)}


# ------------------------------------------------------------- registry
def _guard(fn):
    try:
        return fn()
    except Exception as exc:  # tool không bao giờ raise vào agent loop
        return {"error": f"{type(exc).__name__}: {exc}"}


def _schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {},
            "required": required or []}


def build_tools(env=None) -> Dict[str, Dict[str, Any]]:
    """Registry-ready tool dicts cho environment của Boom."""

    def t_secret_scan(target: str, diff: bool = False):
        return _guard(lambda: secret_scan(target, bool(diff)))

    def t_sanitize_input(tool_name: str, args: dict, allowed_roots=None,
                         max_str_len: int = 100_000):
        return _guard(lambda: sanitize_input(tool_name, dict(args or {}),
                                             allowed_roots, int(max_str_len)))

    def t_check_rate_limit(key: str, strict: bool = False):
        return _guard(lambda: check_rate_limit(key, bool(strict)))

    return {
        "secret_scan": {
            "name": "secret_scan",
            "func": t_secret_scan,
            "description": (
                "Quét secret lộ trong file/thư mục/unified diff. Trả về "
                "findings (preview đã che) + has_secrets. Chạy trước khi "
                "commit/push hoặc khi nghi ngờ lộ key."
            ),
            "schema": _schema(
                {
                    "target": {
                        "type": "string",
                        "description": "Đường dẫn file/thư mục, hoặc nội dung diff khi diff=true.",
                    },
                    "diff": {
                        "type": "boolean",
                        "description": "True nếu target là nội dung unified diff.",
                    },
                },
                ["target"],
            ),
        },
        "sanitize_input": {
            "name": "sanitize_input",
            "func": t_sanitize_input,
            "description": (
                "Sanitize args trước khi gọi tool: strip ký tự điều khiển, "
                "truncate, phát hiện prompt injection, validate path chống "
                "traversal. ok=false -> KHÔNG gọi tool với args này."
            ),
            "schema": _schema(
                {
                    "tool_name": {"type": "string", "description": "Tên tool sắp gọi."},
                    "args": {"type": "object", "description": "Dict args sắp truyền cho tool."},
                    "allowed_roots": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Root cho phép nếu args chứa path.",
                    },
                    "max_str_len": {
                        "type": "integer",
                        "description": "Độ dài tối đa mỗi string. Mặc định 100000.",
                    },
                },
                ["tool_name", "args"],
            ),
        },
        "check_rate_limit": {
            "name": "check_rate_limit",
            "func": t_check_rate_limit,
            "description": (
                "Kiểm tra rate limit (token bucket) cho 1 key. allowed=false "
                "-> chờ retry_after giây. strict=true cho tool nguy hiểm."
            ),
            "schema": _schema(
                {
                    "key": {"type": "string", "description": "Key cần check (user/channel/tool)."},
                    "strict": {
                        "type": "boolean",
                        "description": "Dùng preset STRICT cho tool nguy hiểm.",
                    },
                },
                ["key"],
            ),
        },
    }
