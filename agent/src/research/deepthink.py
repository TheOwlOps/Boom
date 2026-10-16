# -*- coding: utf-8 -*-
"""
deepthink.py — DeepThink for DeepSearch (Boom).

Analyzes cross-reference results into:
  - perspectives: groups claims into viewpoints (consensus / individual views)
  - gaps:        entities in the topic no source mentions (blind spots)
  - consensus:   claims reaching the agreement threshold (>= ceil(total_sources/2))
  - disputes:    claims appearing in only 1 source — KEPT AS-IS, never dropped
  - source_scores: each source's cross-trust score, sorted descending

NO-EVASION PRINCIPLE:
  - Every topic (including politics, religion, sensitive subjects) is analyzed
    fully and equally. This module has NO topic-based filtering/censorship
    logic — disputes always keep every single-source claim.
  - Each perspective cites its source as "Theo [url], ..." — readers judge for
    themselves; the module adds no subjective commentary.

Python stdlib only. Deployed at: agent/src/research/deepthink.py
"""

import math
import re

# ---------------------------------------------------------------------------
# Stopwords: used to extract "entities" from the topic/query.
# These words appear in every sentence so they don't count as entities.
# NOTE: the lists below are functional keyword data — kept as-is.
# ---------------------------------------------------------------------------
STOPWORDS = {
    # Vietnamese
    "và", "là", "của", "các", "cái", "những", "một", "trong", "được",
    "có", "đã", "sẽ", "với", "cho", "để", "này", "đó", "rất", "tại",
    "về", "như", "khi", "thì", "mà", "hay", "hoặc", "nhưng", "cũng",
    # English
    "the", "is", "a", "an", "and", "of", "to", "in", "it", "that",
    "this", "for", "on", "with", "as", "are", "was", "be", "by",
}

_WORD_RE = re.compile(r"[\w]+", re.UNICODE)
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]+(?:\s+|$)")


def extract_keywords(text):
    """
    Extracts meaningful keywords from text: lowercase -> tokenize ->
    drop stopwords and 1-char words -> keep order, dedupe.
    """
    seen = set()
    keywords = []
    for w in _WORD_RE.findall((text or "").lower()):
        if w in STOPWORDS or len(w) < 2:
            continue
        if w not in seen:
            seen.add(w)
            keywords.append(w)
    return keywords


def first_sentence(text):
    """Takes the first sentence of text (fallback for no-crossref mode)."""
    if not text:
        return ""
    m = _SENTENCE_RE.search(text.strip())
    return m.group(0).strip() if m else text.strip()


class DeepThink:
    """Deep multi-source research analysis."""

    # -- main API -------------------------------------------------------

    def analyze(self, topic, sources, crossref_result=None):
        """
        Args:
            topic: research topic/query (str).
            sources: list of dicts {url, title, text}.
            crossref_result: dict {claims: [{text, sources, count, agreement}],
                                   total_sources, ...} from cross_reference().
                             None -> simple logic: each source = 1 claim.

        Returns:
            {topic, summary, perspectives, gaps, consensus, disputes,
             source_scores}
        """
        sources = list(sources or [])
        topic = (topic or "").strip()

        if crossref_result is None:
            claims = self._simple_claims(sources)
            total_sources = len(sources)
        else:
            claims = list(crossref_result.get("claims") or [])
            # total_sources comes from crossref; falls back to the passed-in
            # source count when crossref doesn't provide it or it's 0.
            total_sources = crossref_result.get("total_sources") or len(sources)

        perspectives = self._build_perspectives(claims)
        gaps = self._find_gaps(topic, sources)

        # Consensus threshold: >= half the sources (rounded up).
        majority = math.ceil(total_sources / 2) if total_sources else 1
        consensus = [c.get("text", "") for c in claims
                     if c.get("count", 0) >= majority]
        # Disputes = single-source claims. NEVER filtered by sensitive topic —
        # the no-evasion principle: keep them so readers judge for themselves.
        disputes = [c.get("text", "") for c in claims
                    if c.get("count", 0) == 1]

        source_scores = self._score_sources(sources, claims)
        summary = self._summarize(topic, total_sources, majority,
                                  consensus, disputes, gaps, source_scores)

        return {
            "topic": topic,
            "summary": summary,
            "perspectives": perspectives,
            "gaps": gaps,
            "consensus": consensus,
            "disputes": disputes,
            "source_scores": source_scores,
        }

    # -- builders --------------------------------------------------------

    def _simple_claims(self, sources):
        """
        Fallback without crossref_result: each source contributes exactly 1
        claim taken from its text's first sentence, count=1 (not cross-checked).
        """
        claims = []
        for src in sources:
            url = src.get("url") or ""
            text = (src.get("text") or "").strip()
            claims.append({
                "text": first_sentence(text) or text[:200],
                "sources": [url],
                "count": 1,
                "agreement": False,
            })
        return claims

    def _build_perspectives(self, claims):
        """
        Each claim -> 1 perspective:
          - count >= 2 : "consensus" view (multiple sources agree)
          - count == 1 : "individual" view (single source)
        Every view opens with "Theo [url], ..." to make the source explicit.

        NOTE: the kind values ("dong_thuan"/"rieng") and the "Theo" prefix are
        pinned by evals/test_deepthink.py — keep them as-is.
        """
        perspectives = []
        for claim in claims:
            urls = list(claim.get("sources") or [])
            text = (claim.get("text") or "").strip()
            count = claim.get("count", 0)
            if count >= 2:
                kind = "dong_thuan"
                view = "Theo %s (đồng thuận %d nguồn): %s" % (
                    ", ".join(urls), count, text)
            else:
                kind = "rieng"
                src = urls[0] if urls else "không rõ nguồn"
                view = "Theo %s (quan điểm riêng): %s" % (src, text)
            perspectives.append({
                "kind": kind,
                "view": view,
                "sources": urls,
                "support_count": count,
            })
        return perspectives

    def _find_gaps(self, topic, sources):
        """
        Finds blind spots: topic/query entities that NO source mentions.

        Simple approach, no heavy NLP:
          1. Extract meaningful keywords from the topic (drop stopwords).
          2. Build the set of all words appearing in the sources.
          3. Topic keywords absent from that set -> gaps.
        Word-set matching (not substring) avoids false positives like "tin"
        matching inside "tin tức".
        """
        keywords = extract_keywords(topic)
        if not keywords:
            return []
        corpus_words = set()
        for src in sources:
            corpus_words.update(_WORD_RE.findall((src.get("text") or "").lower()))
        return [kw for kw in keywords if kw not in corpus_words]

    def _score_sources(self, sources, claims):
        """
        A source's cross-trust score =
            (# of its claims that got cross-referenced, i.e. count >= 2)
            / (its total # of claims).
        A source contributing no claims -> score 0.0 (still listed).
        Sorted descending by score; ties broken by url for stability.
        """
        scored = []
        for src in sources:
            url = src.get("url") or ""
            mine = [c for c in claims if url in (c.get("sources") or [])]
            cross_refed = [c for c in mine if c.get("count", 0) >= 2]
            score = (len(cross_refed) / len(mine)) if mine else 0.0
            scored.append({"url": url, "score": round(score, 3)})
        scored.sort(key=lambda d: (-d["score"], d["url"]))
        return scored

    def _summarize(self, topic, total_sources, majority,
                   consensus, disputes, gaps, source_scores):
        """2-3 sentence rollup: consensus, blind spots, most trusted source."""
        s1 = ("Tổng hợp chủ đề '%s' từ %d nguồn: %d claim đạt đồng thuận "
              "(xuất hiện ở >= %d nguồn), %d quan điểm chỉ xuất hiện ở 1 nguồn."
              % (topic, total_sources, len(consensus), majority, len(disputes)))
        if gaps:
            s2 = "Góc khuất chưa nguồn nào đề cập: %s." % ", ".join(gaps)
        else:
            s2 = "Không phát hiện góc khuất lớn so với từ khóa chủ đề."
        if source_scores:
            top = source_scores[0]
            s3 = ("Nguồn có độ tin cậy chéo cao nhất: %s (score %.2f)."
                  % (top["url"], top["score"]))
        else:
            s3 = "Không có nguồn để đánh giá độ tin cậy."
        return " ".join([s1, s2, s3])


def build_tools(env=None):
    """Factory for environment.py — registers the deep_think tool."""
    from research.deepthink import DeepThink

    def deep_think(topic: str, sources: list = None) -> dict:
        """Deep multi-angle analysis, no topic evasion.

        Args:
            topic: the topic to analyze.
            sources: list of {url, title, text}.
        """
        try:
            dt = DeepThink()
            return {"ok": True, "result": dt.analyze(topic, sources or [])}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return {
        "deep_think": {
            "name": "deep_think",
            "func": deep_think,
            "description": "Deep multi-perspective analysis with source trust scoring.",
            "schema": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "The topic."},
                    "sources": {"type": "array", "description": "List of sources.", "default": []},
                },
                "required": ["topic"],
            },
        }
    }
