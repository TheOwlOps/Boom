# -*- coding: utf-8 -*-
"""
crossref.py — Cross-Reference cho DeepSearch (Boom).

Chức năng: nhận danh sách sources [{url, title, text}], tách mỗi source
thành câu, gom các câu nói về cùng một claim (dựa trên overlap từ khóa),
trả về các claim được sắp theo độ đồng thuận giảm dần.

Python stdlib only.
"""

import re

# ---------------------------------------------------------------------------
# Stopwords: ~30 từ phổ biến tiếng Việt / tiếng Anh, loại khỏi phép so sánh
# vì chúng xuất hiện ở mọi câu và làm nhiễu overlap.
# ---------------------------------------------------------------------------
STOPWORDS = {
    # tiếng Việt
    "và", "là", "của", "các", "cái", "những", "một", "trong", "được",
    "có", "đã", "sẽ", "với", "cho", "để", "này", "đó", "rất",
    # tiếng Anh
    "the", "is", "a", "an", "and", "of", "to", "in", "it", "that",
    "this", "for", "on", "with", "as", "are", "was", "be", "by",
}

# Ngưỡng overlap để coi 2 câu là cùng claim:
# |từ chung| / |từ của câu ngắn hơn| > 0.6
OVERLAP_THRESHOLD = 0.6

# Câu quá ngắn (sau khi chuẩn hóa) thì bỏ qua — câu 1-2 từ không đủ
# thông tin để gọi là "claim".
MIN_KEYWORD_COUNT = 3

# Regex tách câu: ngắt tại . ! ? (kèm 1+ ký tự trắng hoặc hết chuỗi).
# Cẩn thận với viết tắt: sau khi tách, các mảnh quá ngắn được bỏ.
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]+(?:\s+|$)")

# Tách từ: chữ Unicode + số. (bỏ dấu câu, ký tự đặc biệt)
_WORD_RE = re.compile(r"[\w]+", re.UNICODE)


def split_sentences(text):
    """Tách text thành câu. Trả về list câu đã strip, không rỗng."""
    if not text:
        return []
    sentences = [m.group(0).strip() for m in _SENTENCE_RE.finditer(text)]
    # Fallback: nếu regex không tách được (text không có dấu câu),
    # coi cả text là 1 câu.
    if not sentences:
        text = text.strip()
        return [text] if text else []
    return [s for s in sentences if s]


def normalize_sentence(sentence):
    """
    Chuẩn hóa câu thành set từ khóa:
    lowercase -> tách từ -> bỏ stopwords và từ 1 ký tự.
    """
    words = _WORD_RE.findall(sentence.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 1}


def sentences_same_claim(keywords_a, keywords_b, threshold=OVERLAP_THRESHOLD):
    """
    2 câu được coi là cùng claim khi:
    |từ chung| / |từ của câu ngắn hơn| > threshold.

    Chia cho câu NGẮN hơn (thay vì union) để câu chi tiết dài hơn vẫn
    match được với câu ngắn tóm tắt cùng ý.
    """
    if not keywords_a or not keywords_b:
        return False
    overlap = len(keywords_a & keywords_b)
    shorter = min(len(keywords_a), len(keywords_b))
    return (overlap / shorter) > threshold


def cross_reference(sources, threshold=OVERLAP_THRESHOLD):
    """
    Gom claim từ nhiều sources.

    Args:
        sources: list dict {url, title, text}
        threshold: ngưỡng overlap (mặc định 0.6)

    Returns:
        {
          "claims": [
            {
              "text": câu đại diện (câu dài nhất trong nhóm),
              "sources": [url, ...],   # url duy nhất, giữ thứ tự xuất hiện
              "count": int,            # số source đề cập
              "agreement": bool,       # True khi >= 2 sources (không phát
                                       # hiện mâu thuẫn ở mức đơn giản này
                                       # — để DeepThink xử lý)
            },
          ],
          "total_sources": int,
          "coverage": float,  # số source có >= 1 claim chung / tổng sources
        }
    """
    total_sources = len(sources)

    # Bước 1: tách câu + chuẩn hóa từng source.
    # Mỗi phần tử: (url, câu gốc, set từ khóa)
    sentences = []
    for src in sources or []:
        url = src.get("url") or ""
        for sent in split_sentences(src.get("text") or ""):
            kw = normalize_sentence(sent)
            if len(kw) >= MIN_KEYWORD_COUNT:
                sentences.append((url, sent, kw))

    # Bước 2: gom nhóm — câu mới so với từng nhóm hiện có, match nhóm
    # đầu tiên có bất kỳ câu nào "same claim" (single-linkage).
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

    # Bước 3: dựng claim. Câu đại diện = câu dài nhất (nhiều chữ nhất),
    # giả định câu dài chứa nhiều chi tiết nhất.
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

    # Bước 4: sắp xếp theo count giảm dần.
    claims.sort(key=lambda c: c["count"], reverse=True)

    # Bước 5: coverage = số source xuất hiện trong ít nhất 1 claim
    # CÓ agreement (claim chung 2+ sources) / tổng sources.
    shared_urls = {u for c in claims if c["agreement"] for u in c["sources"]}
    coverage = (len(shared_urls) / total_sources) if total_sources else 0.0

    return {
        "claims": claims,
        "total_sources": total_sources,
        "coverage": coverage,
    }
