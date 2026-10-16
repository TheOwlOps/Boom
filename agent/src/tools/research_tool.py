"""Multi-source research briefs — deterministic assembly, standard library only.

The agent collects sources with ``web_search`` / ``web_extract`` (or the ``browser_*``
tools) and hands them here. This module does NOT fetch anything and does NOT call an
LLM: it validates the sources, numbers them in order, and assembles an extractive
brief with stable ``[1]``, ``[2]`` citations the agent can quote back. Any real
synthesis or judgement is the agent's own writing on top of this structure — nothing
here invents a claim that is not in a supplied excerpt.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

MAX_SOURCES = 20       # hard cap: a brief is not a bibliography dump
SUMMARY_SOURCES = 5    # digest sentences: one extractive sentence per source, up to 5
EXCERPT_CAP = 400      # chars of an excerpt ever quoted into summary / key points
SENTENCE_CAP = 280     # chars per quoted sentence


def _sentences(text: str) -> List[str]:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return []
    return [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]


def _first_sentence(text: str, cap: int = SENTENCE_CAP) -> str:
    """First sentence of ``text``, capped; "" when there is nothing quotable."""
    found = _sentences(text)
    first = found[0] if found else ""
    if len(first) > cap:
        first = first[:cap].rstrip() + "…"
    return first


def _clean_source(raw: Any) -> Optional[Dict[str, str]]:
    """Normalise one source dict; None when it cannot be cited (no usable url)."""
    if not isinstance(raw, dict):
        return None
    url = str(raw.get("url") or "").strip()
    if not url:
        return None
    title = str(raw.get("title") or "").strip() or url
    excerpt = re.sub(r"\s+", " ", str(raw.get("excerpt") or "")).strip()
    if len(excerpt) > EXCERPT_CAP:
        excerpt = excerpt[:EXCERPT_CAP].rstrip() + "…"
    return {"url": url, "title": title, "excerpt": excerpt}


def research_brief(topic: str, sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Assemble a cited brief from already-collected sources.

    ``sources`` is [{"url", "title", "excerpt"}, ...] in the agent's collection order.
    Returns {"topic", "summary", "key_points", "sources"} where every claim carries
    its ``[n]`` citation, or {"error": ...} when there is nothing usable to brief.
    Pure function: no network, no model calls.
    """
    name = (topic or "").strip()
    if not name:
        return {"error": "empty topic"}
    if not isinstance(sources, list) or not sources:
        return {
            "error": "no sources: collect them first with web_search/web_extract "
                     "or the browser_* tools"
        }

    cleaned: List[Dict[str, str]] = []
    seen = set()
    skipped = 0
    for raw in sources:
        item = _clean_source(raw)
        if item is None or item["url"] in seen:
            skipped += 1
            continue
        seen.add(item["url"])
        cleaned.append(item)
        if len(cleaned) >= MAX_SOURCES:
            break
    if not cleaned:
        return {"error": "no usable sources: each source needs a non-empty url"}

    # Digest: one extractive sentence per source, in order, up to SUMMARY_SOURCES.
    digest = []
    for i, src in enumerate(cleaned[:SUMMARY_SOURCES], 1):
        sentence = _first_sentence(src["excerpt"]) or src["title"]
        digest.append(f"[{i}] {sentence}")
    summary = " ".join(digest)
    rest = len(cleaned) - SUMMARY_SOURCES
    if rest > 0:
        summary += f" (+{rest} more sources, see the list below.)"

    key_points = []
    for i, src in enumerate(cleaned, 1):
        point = _first_sentence(src["excerpt"]) or src["title"]
        key_points.append(f"[{i}] {src['title']}: {point}")

    out: Dict[str, Any] = {
        "topic": name,
        "summary": summary,
        "key_points": key_points,
        # Original order kept; each source annotated with its citation number.
        "sources": [
            {"ref": f"[{i}]", "title": s["title"], "url": s["url"], "excerpt": s["excerpt"]}
            for i, s in enumerate(cleaned, 1)
        ],
    }
    if skipped:
        out["skipped"] = skipped
    return out


def build_tools(env: Any = None) -> Dict[str, Dict[str, Any]]:
    """Registry-ready tool dicts for AgentEnvironment._tools.

    ``env`` is accepted for the registration call site; this tool holds no per-env state.
    """
    return {
        "research_brief": {
            "name": "research_brief",
            "func": research_brief,
            "description": (
                "Assemble a cited research brief from sources ALREADY collected with "
                "web_search/web_extract or the browser_* tools. Deterministic: validates "
                "and numbers the sources, then builds an extractive summary and key points "
                "with stable [1], [2] citations. No fetching, no model calls — the agent "
                "does the collecting and the judging; this tool only structures it."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Research topic, e.g. 'perovskite solar cells 2026'.",
                    },
                    "sources": {
                        "type": "array",
                        "description": (
                            "Sources in collection order, each with url, title and the "
                            "excerpt passages collected from the page."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string"},
                                "title": {"type": "string"},
                                "excerpt": {"type": "string"},
                            },
                            "required": ["url"],
                        },
                    },
                },
                "required": ["topic", "sources"],
            },
        },
    }
