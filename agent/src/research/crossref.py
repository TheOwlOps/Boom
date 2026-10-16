# -*- coding: utf-8 -*-
"""
crossref.py — Cross-Reference for DeepSearch (Boom).

Job: takes a list of sources [{url, title, text}], splits each source into
sentences, groups sentences talking about the same claim (by keyword
overlap), and returns the claims sorted by decreasing consensus.

Python stdlib only.
"""

import re

# ---------------------------------------------------------------------------
# Stopwords: ~30 common Vietnamese / English words, excluded from comparison
# because they appear in every sentence and pollute the overlap.
# NOTE: functional keyword list — kept as-is.
# ---------------------------------------------------------------------------
STOPWORDS = {
    # tiếng Việt
    "và", "là", "của", "các", "cái", "những", "một", "trong", "được",
    "có", "đã", "sẽ", "với", "cho", "để", "này", "đó", "rất",
    # tiếng Anh
    "the", "is", "a", "an", "and", "of", "to", "in", "it", "that",
    "this", "for", "on", "with", "as", "are", "was", "be", "by",
}

# Overlap threshold for 2 sentences to count as the same claim:
# |shared words| / |words of the shorter sentence| > 0.6
OVERLAP_THRESHOLD = 0.6

# Sentences too short (after normalization) are skipped — 1-2 word sentences
# don't carry enough information to be a "claim".
MIN_KEYWORD_COUNT = 3

# Sentence splitter: breaks at . ! ? (followed by 1+ whitespace or end).
# Careful with abbreviations: after splitting, too-short fragments are dropped.
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]+(?:\s+|$)")

# Word tokenizer: Unicode letters + digits. (drops punctuation, special chars)
_WORD_RE = re.compile(r"[\w]+", re.UNICODE)


def split_sentences(text):
    """Splits text into sentences. Returns stripped, non-empty sentence list."""
    if not text:
        return []
    sentences = [m.group(0).strip() for m in _SENTENCE_RE.finditer(text)]
    # Fallback: when the regex can't split (text without punctuation),
    # treat the whole text as 1 sentence.
    if not sentences:
        text = text.strip()
        return [text] if text else []
    return [s for s in sentences if s]


def normalize_sentence(sentence):
    """
    Normalizes a sentence into a keyword set:
    lowercase -> tokenize -> drop stopwords and 1-char words.
    """
    words = _WORD_RE.findall(sentence.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 1}


def sentences_same_claim(keywords_a, keywords_b, threshold=OVERLAP_THRESHOLD):
    """
    2 sentences count as the same claim when:
    |shared words| / |words of the shorter sentence| > threshold.

    Divides by the SHORTER sentence (rather than the union) so a long detailed
    sentence still matches a short summary sentence with the same meaning.
    """
    if not keywords_a or not keywords_b:
        return False
    overlap = len(keywords_a & keywords_b)
    shorter = min(len(keywords_a), len(keywords_b))
    return (overlap / shorter) > threshold


def cross_reference(sources, threshold=OVERLAP_THRESHOLD):
    """
    Groups claims from multiple sources.

    Args:
        sources: list of dicts {url, title, text}
        threshold: overlap threshold (default 0.6)

    Returns:
        {
          "claims": [
            {
              "text": representative sentence (the longest in the group),
              "sources": [url, ...],   # unique urls, appearance order kept
              "count": int,            # sources mentioning it
              "agreement": bool,       # True when >= 2 sources (no contradiction
                                       # detection at this simple level
                                       # — DeepThink handles that)
            },
          ],
          "total_sources": int,
          "coverage": float,  # sources with >= 1 shared claim / total sources
        }
    """
    total_sources = len(sources)

    # Step 1: split into sentences + normalize per source.
    # Each element: (url, original sentence, keyword set)
    sentences = []
    for src in sources or []:
        url = src.get("url") or ""
        for sent in split_sentences(src.get("text") or ""):
            kw = normalize_sentence(sent)
            if len(kw) >= MIN_KEYWORD_COUNT:
                sentences.append((url, sent, kw))

    # Step 2: group — each new sentence is compared against existing groups,
    # matching the first group where any sentence is "same claim" (single-linkage).
    groups = []  # list of {"sentences": [(url, text, kw), ...], "urls": [url, ...]}
    for url, sent, kw in sentences:
        matched = None
        for g in groups:
            if any(sentences_same_claim(kw, other_kw)
                   for _, _, other_kw in g["sentences"]):
                matched = g
                break
        if matched is None:
            matched = {"sentences": [], "urls": []}
            groups.append(matched)
        matched["sentences"].append((url, sent, kw))
        if url not in matched["urls"]:
            matched["urls"].append(url)

    # Step 3: build claims. Representative sentence = the longest (most chars),
    # assuming the longest carries the most detail.
    claims = []
    for g in groups:
        urls = g["urls"]
        representative = max(g["sentences"], key=lambda t: len(t[1]))[1]
        claims.append({
            "text": representative,
            "sources": list(urls),
            "count": len(urls),
            "agreement": len(urls) >= 2,
        })

    # Step 4: sort by decreasing count.
    claims.sort(key=lambda c: c["count"], reverse=True)

    # Step 5: coverage = sources appearing in at least 1 claim WITH
    # agreement (shared by 2+ sources) / total sources.
    shared_urls = {u for c in claims if c["agreement"] for u in c["sources"]}
    coverage = (len(shared_urls) / total_sources) if total_sources else 0.0

    return {
        "claims": claims,
        "total_sources": total_sources,
        "coverage": coverage,
    }
