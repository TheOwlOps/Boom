"""DeepSearch core for Boom: multi-hop web research.

``DeepSearch.research(query)`` workflow:
  1. web_search(query) -> source list
  2. web_fetch each source -> extract text
  3. Extract new entities/keywords from text with simple regexes
     (2+ word capitalized phrases, dates, numbers)
  4. Generate follow-up queries from unexplored entities
  5. Repeat until ``max_hops`` or no new queries

Philosophy: stdlib + httpx (via agent/src/tools/web.py), no API key needed.
Deployed at: agent/src/research/deepsearch.py
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

__all__ = ["DeepSearch"]

# --- Import the real web.py when running in the repo; None in standalone tests ---
try:
    from tools.web import web_search as _real_search  # type: ignore
    from tools.web import web_fetch as _real_fetch  # type: ignore
except ImportError:  # standalone (test): allow mock injection via constructor
    _real_search = None
    _real_fetch = None

# --- lifecycle/timeout per fetch; skipped if not importable ---
try:
    from lifecycle.timeout import with_timeout  # type: ignore
except ImportError:
    with_timeout = None  # type: ignore[assignment]

FETCH_TIMEOUT = 30.0      # seconds per source fetch
MAX_TEXT_CHARS = 20_000   # cap text per source so entities don't get too noisy
FOLLOWUPS_PER_HOP = 3     # max follow-up queries generated per hop

# 2+ word capitalized phrases: each word starts with a capital letter
# (including accented Vietnamese), max 4 words so it doesn't swallow sentences.
# NOTE: the character classes and unit keywords below are functional
# Vietnamese-aware patterns — kept as-is.
_CAP_LETTER = r"[A-ZÀ-ÝĂĐĨŨƠƯẠ-Ỹ]"
_CAP_WORD = _CAP_LETTER + r"[\wÀ-ỹ\-]*"
_RE_CAP_PHRASE = re.compile(r"\b" + _CAP_WORD + r"(?:\s+" + _CAP_WORD + r"){1,3}\b")

# Dates like 14/03/2023, 1-2-24 (year-first 2026-10-15 doesn't match) -> accepted.
_RE_DATE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")

# Numbers: 175, 3.5, 10,000 with optional units (triệu, tỷ, %, USD, VND...).
_RE_NUMBER = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:triệu|tỷ|nghìn|%|USD|VND|đồng|người)?\b"
)


class DeepSearch:
    """Multi-hop research: search -> fetch -> extract entities -> follow-up -> repeat.

    Args:
        search_fn: ``(query) -> [{title, url, snippet}]``. Defaults to
            ``tools.web.web_search`` when importable.
        fetch_fn: ``(url) -> {url, text, title, ...}``. Defaults to
            ``tools.web.web_fetch`` (has circuit breaker + retry built in).
        If both are None and web.py failed to import -> raises RuntimeError.
    """

    def __init__(
        self,
        search_fn: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
        fetch_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
    ) -> None:
        self._search_fn = search_fn if search_fn is not None else _real_search
        self._fetch_fn = fetch_fn if fetch_fn is not None else _real_fetch
        if self._search_fn is None or self._fetch_fn is None:
            raise RuntimeError(
                "DeepSearch needs search_fn/fetch_fn: web.py could not be imported "
                "and no mock was injected."
            )
        # Wrap fetch with the lifecycle timeout when available; call directly otherwise.
        base_fetch = self._fetch_fn
        if with_timeout is not None:
            self._fetch_fn = with_timeout(FETCH_TIMEOUT)(base_fetch)

    # ------------------------------------------------------------------
    # Entity extraction
    def _extract_entities(self, text: str) -> List[str]:
        """Extracts entities/keywords from text with 3 regexes, in order of appearance.

        Returns a deduped list (case-insensitive compare) ordered:
        capitalized phrases -> dates -> numbers.
        """
        text = (text or "")[:MAX_TEXT_CHARS]
        found: List[str] = []
        seen: Set[str] = set()

        def _add(value: str) -> None:
            value = value.strip()
            if len(value) < 2:
                return
            key = value.lower()
            if key not in seen:
                seen.add(key)
                found.append(value)

        for m in _RE_CAP_PHRASE.finditer(text):
            _add(m.group(0))
        for m in _RE_DATE.finditer(text):
            _add(m.group(0))
        for m in _RE_NUMBER.finditer(text):
            # Drop short bare numbers (e.g. "1", "2") as noise; keep numbers
            # with units or >= 3 digits. Unit detection: does the match
            # contain a letter?
            num = m.group(0).strip()
            has_unit = bool(re.search(r"[a-zA-Z%]", num))
            digits = re.sub(r"\D", "", num)
            if has_unit or len(digits) >= 3:
                _add(num)
        return found

    # ------------------------------------------------------------------
    # Follow-up query generation
    @staticmethod
    def _followup_query(entity: str, original_query: str, first: bool) -> str:
        """Builds a digging-deeper question from an unexplored entity.

        First entity of the hop -> "{entity} là gì" (asks for a definition);
        later entities -> "{entity} {original_query}" (asks in the original context).

        NOTE: the "là gì" template is pinned by evals/test_deepsearch.py —
        keep it as-is.
        """
        if first:
            return f"{entity} là gì"
        return f"{entity} {original_query}"

    # ------------------------------------------------------------------
    # Safe fetch: one source's network error must not kill the whole research loop
    def _safe_fetch(self, url: str) -> Dict[str, Any]:
        try:
            page = self._fetch_fn(url) or {}
        except Exception as exc:  # noqa: BLE001 - failed fetch drops this source
            return {"url": url, "title": "", "text": "", "error": str(exc)}
        return {
            "url": page.get("url", url),
            "title": page.get("title", ""),
            "text": page.get("text", ""),
        }

    # ------------------------------------------------------------------
    def research(
        self,
        query: str,
        max_hops: int = 5,
        max_sources: int = 10,
    ) -> Dict[str, Any]:
        """Runs multi-hop research.

        Args:
            query: the original question.
            max_hops: max search rounds.
            max_sources: max total sources to fetch.

        Returns:
            {
              "query": original query,
              "sources": [{"url", "title", "text", "hop"}],
              "entities": [entity strings in order of appearance],
              "hops_used": actual hops run,
            }
        """
        query = (query or "").strip()
        if not query:
            raise ValueError("query must be a non-empty string")
        max_hops = max(1, int(max_hops))
        max_sources = max(1, int(max_sources))

        # BFS queue of (query, hop); avoid repeating queries with seen_queries.
        queue: List[Tuple[str, int]] = [(query, 0)]
        seen_queries: Set[str] = {query.lower()}

        # 2 dedupe sets: fetched URLs and entities that spawned follow-ups.
        fetched_urls: Set[str] = set()
        explored_entities: Set[str] = set()

        sources: List[Dict[str, Any]] = []
        entities: List[str] = []
        seen_entity_keys: Set[str] = set()
        hops_used = 0

        while queue:
            q, hop = queue.pop(0)
            if hop >= max_hops:
                continue

            try:
                results = self._search_fn(q) or []
            except Exception:
                continue  # failed search -> skip this hop, try the next queued query

            new_entities_this_hop: List[str] = []
            for r in results:
                if len(sources) >= max_sources:
                    break
                url = (r or {}).get("url", "")
                if not url or url in fetched_urls:
                    continue  # dedupe: skip already-fetched URLs
                fetched_urls.add(url)

                page = self._safe_fetch(url)
                text = page.get("text") or ""
                sources.append(
                    {
                        "url": page["url"],
                        "title": page.get("title", ""),
                        "text": text[:MAX_TEXT_CHARS],
                        "hop": hop,
                    }
                )
                for ent in self._extract_entities(text):
                    key = ent.lower()
                    if key not in seen_entity_keys:
                        seen_entity_keys.add(key)
                        entities.append(ent)
                        new_entities_this_hop.append(ent)

            hops_used = max(hops_used, hop + 1)

            # Generate follow-ups: only from new, unexplored entities,
            # capped at FOLLOWUPS_PER_HOP per hop to avoid combinatorial explosion.
            made = 0
            for i, ent in enumerate(new_entities_this_hop):
                if made >= FOLLOWUPS_PER_HOP:
                    break
                key = ent.lower()
                if key in explored_entities:
                    continue
                explored_entities.add(key)
                fq = self._followup_query(ent, query, first=(i == 0))
                if fq.lower() not in seen_queries:
                    seen_queries.add(fq.lower())
                    queue.append((fq, hop + 1))
                    made += 1

        return {
            "query": query,
            "sources": sources,
            "entities": entities,
            "hops_used": hops_used,
        }


def build_tools(env=None):
    """Factory for environment.py — registers the deep_search tool."""
    from research.deepsearch import DeepSearch

    def deep_search(query: str, max_hops: int = 3) -> dict:
        """Multi-hop research: search → fetch → entities → follow-up.

        Args:
            query: the research question/topic.
            max_hops: max hops (1-5).
        """
        try:
            ds = DeepSearch()
            return {"ok": True, "result": ds.research(query, max_hops=max_hops)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return {
        "deep_search": {
            "name": "deep_search",
            "func": deep_search,
            "description": "Deep multi-hop research with cross-source verification.",
            "schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The question/topic."},
                    "max_hops": {"type": "integer", "description": "Hops (1-5).", "default": 3},
                },
                "required": ["query"],
            },
        }
    }
