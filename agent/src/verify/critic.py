"""Adversarial critique — a second, hostile pair of eyes on agent output.

This module is the non-LLM half of the CRITIC role defined in
``agent/src/orchestration/mass.py``: an adversarial verifier whose job is to
falsify the work in front of it, not to praise it. It assumes the content is
wrong and goes looking for the proof — and if it finds nothing, it says so
plainly rather than inventing a finding.

The module itself never judges: it builds a kind-specific adversarial prompt,
sends it to the caller's LLM callable, and robustly extracts the resulting
issue list. Anything it cannot parse is dropped, never fabricated.
Standard library only (``json``, ``re``); no network; no other ``verify``
module imports.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List

VALID_KINDS = ("code", "fact", "plan", "text")
VALID_SEVERITIES = ("high", "medium", "low")

_KIND_FOCUS = {
    "code": (
        "logic bugs and wrong assumptions; edge cases and empty/None inputs that "
        "are not handled; security flaws (injection, command injection, path "
        "traversal, SSRF, unsafe deserialization); runtime errors that are "
        "visible from reading the code; race conditions and resource leaks; "
        "incorrect error handling."
    ),
    "fact": (
        "claims with no source or evidence attached; vague or unverifiable "
        "numbers; internal contradictions between statements; claims that "
        "conflict with the cited evidence; weasel words that hide uncertainty."
    ),
    "plan": (
        "missing steps needed to reach the goal; dependencies in the wrong "
        "order; steps that are not feasible with the available tools; missing "
        "verification or rollback steps; steps that are vague enough to be "
        "unexecutable; single points of failure."
    ),
    "text": (
        "logical holes in the argument; overconfident or absolute claims; "
        "missing evidence for key assertions; conclusions that do not follow "
        "from the premises; biased or one-sided framing."
    ),
}

_PERSONA = (
    "You are an adversarial verifier. Your job is to falsify the work in "
    "front of you, not to praise it. Assume it is wrong and go looking for "
    "the proof. Read it skeptically: what is missing, what is wrong, what "
    "would break, what is asserted without evidence. A claim you cannot "
    "check against the content is a defect in the work, not a pass. "
    "If the work genuinely survives your attempts, say so — never invent a "
    "finding to look busy. Returning an empty list is a valid, honest answer."
)

_OUTPUT_FORMAT = (
    'Respond with JSON ONLY, no prose around it, in exactly this shape:\n'
    '{"issues": [{"severity": "high|medium|low", "issue": "<what is wrong>", '
    '"suggestion": "<how to fix it>"}]}\n'
    'If you found nothing, respond {"issues": []}.'
)

_SEVERITY_GUIDE = (
    "severity guide: high = likely wrong/broken/unsafe and matters; "
    "medium = probably an issue or a gap worth fixing; "
    "low = minor, cosmetic, or low-confidence concern."
)


def _build_prompt(content: str, kind: str) -> str:
    return (
        f"{_PERSONA}\n\n"
        f"The work below is of kind '{kind}'. "
        f"Focus on: {_KIND_FOCUS[kind]}\n\n"
        f"{_SEVERITY_GUIDE}\n\n"
        f"{_OUTPUT_FORMAT}\n\n"
        f"--- WORK TO VERIFY ---\n{content}\n--- END OF WORK ---"
    )


def _extract_json(text: str) -> str | None:
    """Return the most likely JSON object substring of ``text``, or None."""
    if not text:
        return None
    # Prefer a ```json fenced block when present.
    fence = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE
    )
    if fence:
        return fence.group(1)
    # Otherwise take the first balanced {...} — brace counting, since a plain
    # regex cannot balance braces and LLMs often add prose around the payload.
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _normalize_severity(raw: Any) -> str | None:
    """Map a free-form severity to one of the valid values, else None."""
    if not isinstance(raw, str):
        return None
    token = raw.strip().lower()
    if token in VALID_SEVERITIES:
        return token
    # Tolerant mapping for common model phrasings.
    if token in ("critical", "severe", "urgent"):
        return "high"
    if token in ("moderate", "warn", "warning"):
        return "medium"
    if token in ("minor", "info", "informational", "trivial"):
        return "low"
    return None


def _clean_issue(raw: Any) -> Dict[str, str] | None:
    """Normalize one issue dict; None when it is unusable (never fabricate)."""
    if not isinstance(raw, dict):
        return None
    severity = _normalize_severity(raw.get("severity"))
    issue = raw.get("issue")
    suggestion = raw.get("suggestion")
    if (
        severity is None
        or not isinstance(issue, str)
        or not issue.strip()
        or not isinstance(suggestion, str)
        or not suggestion.strip()
    ):
        return None
    return {
        "severity": severity,
        "issue": issue.strip(),
        "suggestion": suggestion.strip(),
    }


def critique(content: str, kind: str, llm: Callable[[str], str]) -> List[Dict[str, str]]:
    """Adversarially critique ``content`` of the given ``kind``.

    ``kind`` must be one of "code", "fact", "plan", "text"; anything else
    raises ``ValueError`` (fail fast, honestly).

    ``llm`` is a callable ``(prompt: str) -> str`` (sync). It is invoked
    exactly once with the kind-specific adversarial prompt; its reply is
    parsed for a JSON issue list.

    Returns a list of
    ``{"severity": "high"|"medium"|"low", "issue": str, "suggestion": str}``.
    Returns ``[]`` honestly when nothing was found — or when the model reply
    could not be parsed. Issues with missing/unusable fields are dropped,
    never invented.
    """
    if kind not in VALID_KINDS:
        raise ValueError(
            f"unknown critique kind {kind!r}; expected one of {', '.join(VALID_KINDS)}"
        )
    if not isinstance(content, str) or not content.strip():
        return []

    raw_reply = llm(_build_prompt(content, kind))
    payload = _extract_json(raw_reply or "")
    if payload is None:
        return []
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []

    issues = data.get("issues", [])
    if not isinstance(issues, list):
        return []

    cleaned: List[Dict[str, str]] = []
    for raw in issues:
        item = _clean_issue(raw)
        if item is not None:
            cleaned.append(item)
    return cleaned
