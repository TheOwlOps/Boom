"""factcheck.py - Split text into atomic claims and verify each against search evidence.

Stdlib-only. No network calls here: search goes through the injected ``search_fn``,
LLM goes through the injected ``llm`` callable (both are fakeable in tests).
"""

from __future__ import annotations

import json
import re

VALID_VERDICTS = ("supported", "refuted", "uncertain")

_BULLET_PREFIX = re.compile(r"^(\s*([-*•]|\d+[.)])\s+)")


def _parse_claim_lines(raw: str) -> list[str]:
    """Parse one-claim-per-line LLM output into clean claim strings."""
    claims: list[str] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        line = _BULLET_PREFIX.sub("", line).strip()
        if line:
            claims.append(line)
    return claims


def split_claims(text: str, llm) -> list[str]:
    """Split text into atomic, verifiable claims via the LLM.

    ``llm`` is a callable(prompt: str) -> str.
    Returns [] for empty/whitespace text.
    """
    if not text or not text.strip():
        return []
    prompt = (
        "You split text into atomic claims for fact-checking.\n"
        "Rules:\n"
        "1. One atomic claim per line: a single verifiable fact per claim.\n"
        "2. Split compound sentences so each line holds exactly one fact.\n"
        "3. Drop exclamations, greetings, and purely subjective opinions that cannot\n"
        "   be verified (e.g. 'this is great!').\n"
        "4. Keep each claim as a standalone sentence in the original language.\n"
        "5. Output only the numbered list of claims, nothing else.\n\n"
        f"Text:\n{text.strip()}"
    )
    raw = llm(prompt)
    return _parse_claim_lines(raw)


def _parse_verdict(raw: str) -> str:
    """Robustly extract the verdict from LLM judge output. Defaults to 'uncertain'."""
    if not raw:
        return "uncertain"
    raw = raw.strip()
    # Strip markdown fences if present.
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    # Try full JSON parse first.
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            v = str(data.get("verdict", "")).strip().lower()
            if v in VALID_VERDICTS:
                return v
    except (json.JSONDecodeError, ValueError):
        pass
    # Fallback: find a verdict-looking value anywhere in the text.
    m = re.search(
        r'"verdict"\s*:\s*"?(supported|refuted|uncertain)"?', raw, re.IGNORECASE
    )
    if m:
        return m.group(1).lower()
    return "uncertain"


def _build_judge_prompt(claim: str, snippets: list[dict]) -> str:
    evidence_lines = []
    for i, r in enumerate(snippets, 1):
        title = str(r.get("title", ""))
        url = str(r.get("url", ""))
        snippet = str(r.get("snippet", ""))
        evidence_lines.append(
            f"[{i}] title: {title}\n    url: {url}\n    snippet: {snippet}"
        )
    evidence = "\n".join(evidence_lines) if evidence_lines else "(no evidence found)"
    return (
        "You are a strict fact-check judge. Given a CLAIM and EVIDENCE snippets from web search,\n"
        "return ONLY a JSON object: {\"verdict\": \"supported|refuted|uncertain\", \"reason\": \"...\"}.\n"
        "Verdict rules (follow exactly):\n"
        "- \"supported\": ONLY when at least one snippet DIRECTLY states the claim. Do not infer.\n"
        "- \"refuted\": when a snippet CONTRADICTS the claim.\n"
        "- \"uncertain\": when evidence is insufficient, only mentions the claim in passing,\n"
        "  or says nothing relevant. NEVER guess: absence of evidence is NOT evidence.\n\n"
        f"CLAIM: {claim}\n\n"
        f"EVIDENCE:\n{evidence}"
    )


def check_claims(text: str, llm, search_fn) -> list[dict]:
    """Split claims and verify each via search evidence.

    Returns [{"claim": str,
              "verdict": "supported"|"refuted"|"uncertain",
              "sources": [{"title": str, "url": str}]}].
    """
    results: list[dict] = []
    for claim in split_claims(text, llm):
        try:
            hits = search_fn(claim)
        except Exception:
            hits = None
        if not isinstance(hits, list):
            results.append({"claim": claim, "verdict": "uncertain", "sources": []})
            continue
        hits = [h for h in hits if isinstance(h, dict)]
        sources = [
            {"title": str(h.get("title", "")), "url": str(h.get("url", ""))}
            for h in hits[:3]
        ]
        try:
            judge_raw = llm(_build_judge_prompt(claim, hits))
            verdict = _parse_verdict(judge_raw)
        except Exception:
            verdict = "uncertain"
        results.append({"claim": claim, "verdict": verdict, "sources": sources})
    return results
