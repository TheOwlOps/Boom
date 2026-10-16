"""Security tool group: secret scanning / input sanitize / rate limit.

Wraps the repo's existing modules (no logic copied):
  - ``agent/src/security/secretscan.py`` -> secret_scan
  - ``agent/src/security/sanitize.py``    -> sanitize_input
  - ``agent/src/security/ratelimit.py``   -> check_rate_limit

Try/except imports: a missing module makes its tool return {"error"}.
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
            f"the source module for tool {name!r} is not available in this environment"
        )
    return mod


# ------------------------------------------------------------------ tools
def secret_scan(target: str, diff: bool = False) -> Dict[str, Any]:
    """Scans a file / dir / unified diff for leaked secrets.

    target: file path, directory, or diff content (when diff=True).
    Returns {target, findings: [{line, type, preview (masked)}], has_secrets}.
    Previews mask 80% of the value — safe to log/show to the agent.
    """
    mod = _need(_secretscan, "secret_scan")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("target must be a non-empty string")
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
    """Sanitizes a dict of args before calling a tool (uses security/sanitize.py).

    For each string value: strip control chars (keeps \\n\\t) -> truncate to
    max_str_len -> detect prompt injection. Path keys are validated against
    path traversal per allowed_roots (omit = skip this step).

    Returns {ok, sanitized_args, warnings}. ok=False only on injection or
    path traversal — in that case the agent must NOT call the tool with
    these args.
    """
    mod = _need(_sanitize, "sanitize_input")
    if not isinstance(args, dict):
        raise ValueError("args must be a dict")
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


# Shared rate limiter for the tool (token bucket per key).
# Two module-level instances: default and strict — creating a new one per call
# would lose bucket state, so strict also shares a single instance.
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
    """Checks the rate limit for one key (user/channel/tool).

    Returns {key, allowed, retry_after}. allowed=False means the key is out
    of tokens — the agent should wait retry_after seconds and retry, not spam.
    strict=True uses the STRICT preset (for dangerous tools: sending, deleting...).
    Never raises: exceeding the limit just returns allowed=False.
    """
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key must be a non-empty string")
    key = key.strip()
    limiter = _get_limiter(strict=bool(strict))
    allowed = limiter.allow(key)
    retry_after = 0.0
    if not allowed:
        state = limiter.get_state(key)
        # get_state returns {"tokens", ...}: retry_after ~ time to refill 1 token
        tokens = float(state.get("tokens", 0.0) or 0.0)
        refill = float(getattr(limiter, "refill", 0.5) or 0.5)
        retry_after = max(0.0, (1.0 - tokens) / refill)
    return {"key": key, "allowed": allowed,
            "retry_after": round(retry_after, 2), "strict": bool(strict)}


# ------------------------------------------------------------- registry
def _guard(fn):
    try:
        return fn()
    except Exception as exc:  # tools never raise into the agent loop
        return {"error": f"{type(exc).__name__}: {exc}"}


def _schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {},
            "required": required or []}


def build_tools(env=None) -> Dict[str, Dict[str, Any]]:
    """Registry-ready tool dicts for Boom's environment."""

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
                "Scans a file/dir/unified diff for leaked secrets. Returns "
                "findings (masked previews) + has_secrets. Run before "
                "commit/push or when a key leak is suspected."
            ),
            "schema": _schema(
                {
                    "target": {
                        "type": "string",
                        "description": "File/dir path, or diff content when diff=true.",
                    },
                    "diff": {
                        "type": "boolean",
                        "description": "True when target is unified diff content.",
                    },
                },
                ["target"],
            ),
        },
        "sanitize_input": {
            "name": "sanitize_input",
            "func": t_sanitize_input,
            "description": (
                "Sanitizes args before calling a tool: strips control chars, "
                "truncates, detects prompt injection, validates paths against "
                "traversal. ok=false -> do NOT call the tool with these args."
            ),
            "schema": _schema(
                {
                    "tool_name": {"type": "string", "description": "Name of the tool about to be called."},
                    "args": {"type": "object", "description": "Dict of args about to be passed to the tool."},
                    "allowed_roots": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Allowed roots when args contain paths.",
                    },
                    "max_str_len": {
                        "type": "integer",
                        "description": "Max length per string. Default 100000.",
                    },
                },
                ["tool_name", "args"],
            ),
        },
        "check_rate_limit": {
            "name": "check_rate_limit",
            "func": t_check_rate_limit,
            "description": (
                "Checks the rate limit (token bucket) for one key. allowed=false "
                "-> wait retry_after seconds. strict=true for dangerous tools."
            ),
            "schema": _schema(
                {
                    "key": {"type": "string", "description": "Key to check (user/channel/tool)."},
                    "strict": {
                        "type": "boolean",
                        "description": "Use the STRICT preset for dangerous tools.",
                    },
                },
                ["key"],
            ),
        },
    }
