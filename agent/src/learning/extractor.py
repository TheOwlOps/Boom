"""Extract durable learnings about the user from conversation text.

This is the *learning* pipeline (knowledge about the user: preferences,
facts, decisions, corrections), distinct from ``memory/reflexion.py`` which
stores technical problem->solution lessons.

Security: this module NEVER stores secrets. Any extracted item whose content
looks like it carries a credential (password, API key, token, bearer token,
AWS/GH/OpenAI key formats, Vietnamese "mật khẩu ...") is silently dropped.
See :func:`_looks_like_secret`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from learning.models import Learning, LearningType

# ---------------------------------------------------------------------------
# 1. Fast heuristic gate (no LLM needed)
# ---------------------------------------------------------------------------

_LEARN_SIGNALS = (
    # Vietnamese
    "nhớ",
    "thích",
    "không thích",
    "đừng",
    "sai rồi",
    "quyết định",
    "từ nay",
    "luôn luôn",
    # English
    "remember",
    "prefer",
    "don't",
    "wrong",
    "decided",
)


def should_learn(text: str) -> bool:
    """Return True if *text* contains a signal worth extracting a learning from.

    Pure keyword heuristic, case-insensitive, no LLM call. Empty/blank text
    returns False.
    """
    if not text or not text.strip():
        return False
    lowered = text.lower()
    return any(signal in lowered for signal in _LEARN_SIGNALS)


# ---------------------------------------------------------------------------
# 2. Secret filtering (mandatory)
# ---------------------------------------------------------------------------

# Key=value style credential declarations, EN (+ Vietnamese "mật khẩu").
_SECRET_KEY_VALUE = re.compile(
    r"(?i)(password|passwd|pwd|api[_-]?key|secret|token|mật khẩu|mat khau)"
    r"\s*(là|:|=)"
)

# Vietnamese "mật khẩu <...> là <value>", e.g. "mật khẩu wifi là abc123".
_SECRET_VI_PASSWORD = re.compile(r"(?i)mật khẩu.{0,40}?\blà\b")

# Known token formats.
_SECRET_TOKEN_FORMATS = (
    re.compile(r"sk-[a-zA-Z0-9]{16,}"),                      # OpenAI-style
    re.compile(r"ghp_[a-zA-Z0-9]{20,}"),                     # GitHub PAT
    re.compile(r"AKIA[0-9A-Z]{16}"),                         # AWS access key
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/=]{20,}"),  # Bearer token
)


def _looks_like_secret(content: str) -> bool:
    """Return True if *content* appears to carry a credential.

    Never returns True for empty text. When in doubt we err on the side of
    dropping the item: a lost learning is cheap, a stored secret is not.
    """
    if not content:
        return False
    if _SECRET_KEY_VALUE.search(content):
        return True
    if _SECRET_VI_PASSWORD.search(content):
        return True
    return any(rx.search(content) for rx in _SECRET_TOKEN_FORMATS)


# ---------------------------------------------------------------------------
# 3. LLM extraction
# ---------------------------------------------------------------------------

_EXTRACTION_PROMPT_TEMPLATE = """\
You extract DURABLE learnings about the user from the conversation below.

Return ONLY a JSON list (no markdown, no commentary). Each element:
{{"type": "preference|fact|decision|correction", "content": "...", "confidence": 0.0-1.0}}

Rules:
- ONLY extract things that stay true over time: user preferences, facts about
  the user, decisions they have made, and corrections they gave you.
- DO NOT extract ephemeral details (times, one-off tasks, transient state).
- DO NOT quote the transcript verbatim; summarize each learning in one line.
- confidence: how sure you are the learning is durable (0.0 = guess, 1.0 = certain).
- If there is nothing durable to learn, return an empty list: []

Conversation:
{text}
"""


def _build_prompt(text: str) -> str:
    return _EXTRACTION_PROMPT_TEMPLATE.format(text=text)


def _coerce_type(raw: object) -> LearningType:
    """Map a raw type name to LearningType; unknown -> PREFERENCE."""
    name = str(raw or "").strip().upper()
    if name and hasattr(LearningType, name):
        return getattr(LearningType, name)
    return getattr(LearningType, "PREFERENCE", "preference")


def _coerce_confidence(raw: object) -> float:
    """Coerce to a float in [0.0, 1.0]; unparseable -> 0.5."""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.5
    if value != value:  # NaN
        return 0.5
    return max(0.0, min(1.0, value))


_MAX_CONTENT_LEN = 500


def _build_learning(item: dict, source: str, now_iso: str) -> Learning | None:
    """Validate one raw item into a Learning; return None to drop it."""
    if not isinstance(item, dict):
        return None
    content = item.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    content = content.strip()
    # Secret filter: never store credentials.
    if _looks_like_secret(content):
        return None
    if len(content) > _MAX_CONTENT_LEN:
        content = content[:_MAX_CONTENT_LEN] + "..."
    return Learning(
        id="",
        type=_coerce_type(item.get("type")),
        content=content,
        confidence=_coerce_confidence(item.get("confidence")),
        source=source,
        created_at=now_iso,
        updated_at=now_iso,
    )


def extract_learnings(text: str, llm, source: str = "") -> list[Learning]:
    """Extract durable learnings from *text* via *llm*.

    *llm* is a sync callable ``(prompt: str) -> str`` (kept as a plain
    callable so tests can inject a fake).

    Honest-failure behavior: if the LLM output is not valid JSON, or the JSON
    is not a list, return ``[]`` (never fabricate learnings).
    """
    if not text or not text.strip():
        return []
    try:
        raw = llm(_build_prompt(text))
    except Exception:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    now_iso = datetime.now(timezone.utc).isoformat()
    learnings: list[Learning] = []
    for item in data:
        learning = _build_learning(item, source, now_iso)
        if learning is not None:
            learnings.append(learning)
    return learnings
