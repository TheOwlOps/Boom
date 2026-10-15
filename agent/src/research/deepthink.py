# -*- coding: utf-8 -*-
"""
deepthink.py — DeepThink cho DeepSearch (Boom).

Phân tích kết quả cross-reference thành:
  - perspectives: gom claims thành các góc nhìn (đồng thuận / quan điểm riêng)
  - gaps:        các thực thể trong topic mà không source nào đề cập (góc khuất)
  - consensus:   claims đạt ngưỡng đồng thuận (>= ceil(total_sources/2))
  - disputes:    claims chỉ xuất hiện ở 1 source — GIỮ NGUYÊN, không loại
  - source_scores: độ tin cậy chéo của từng source, sắp xếp giảm dần

NGUYÊN TẮC KHÔNG NÉ TRÁNH:
  - Mọi topic (kể cả chính trị, tôn giáo, chủ đề nhạy cảm) đều được phân tích
    đầy đủ như nhau. Module KHÔNG có bất kỳ logic lọc/kiểm duyệt nào theo
    chủ đề — disputes luôn giữ nguyên mọi claim 1-source.
  - Mỗi perspective ghi rõ nguồn theo dạng "Theo [url], ..." — người đọc tự
    đánh giá, module không thêm ý kiến chủ quan.

Python stdlib only. Deploy vào: agent/src/research/deepthink.py
"""

import math
import re

# ---------------------------------------------------------------------------
# Stopwords: dùng để trích "thực thể" (entity) từ topic/query.
# Các từ này xuất hiện ở mọi câu nên không coi là entity.
# ---------------------------------------------------------------------------
STOPWORDS = {
    # tiếng Việt
    "và", "là", "của", "các", "cái", "những", "một", "trong", "được",
    "có", "đã", "sẽ", "với", "cho", "để", "này", "đó", "rất", "tại",
    "về", "như", "khi", "thì", "mà", "hay", "hoặc", "nhưng", "cũng",
    # tiếng Anh
    "the", "is", "a", "an", "and", "of", "to", "in", "it", "that",
    "this", "for", "on", "with", "as", "are", "was", "be", "by",
}

_WORD_RE = re.compile(r"[\w]+", re.UNICODE)
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]+(?:\s+|$)")


def extract_keywords(text):
    """
    Trích từ khóa có nghĩa từ text: lowercase -> tách từ ->
    bỏ stopwords và từ 1 ký tự -> giữ thứ tự, loại trùng.
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
    """Lấy câu đầu tiên của text (fallback cho mode không có crossref)."""
    if not text:
        return ""
    m = _SENTENCE_RE.search(text.strip())
    return m.group(0).strip() if m else text.strip()


class DeepThink:
    """Phân tích sâu kết quả nghiên cứu đa nguồn."""

    # -- API chính ------------------------------------------------------

    def analyze(self, topic, sources, crossref_result=None):
        """
        Args:
            topic: chủ đề/query nghiên cứu (str).
            sources: list dict {url, title, text}.
            crossref_result: dict {claims: [{text, sources, count, agreement}],
                                   total_sources, ...} từ cross_reference().
                             None -> chạy logic đơn giản: mỗi source = 1 claim.

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
            # total_sources lấy từ crossref; fallback về số sources truyền vào
            # nếu crossref không cung cấp hoặc = 0.
            total_sources = crossref_result.get("total_sources") or len(sources)

        perspectives = self._build_perspectives(claims)
        gaps = self._find_gaps(topic, sources)

        # Ngưỡng đồng thuận: >= nửa số nguồn (làm tròn lên).
        majority = math.ceil(total_sources / 2) if total_sources else 1
        consensus = [c.get("text", "") for c in claims
                     if c.get("count", 0) >= majority]
        # Disputes = claim chỉ 1 source. KHÔNG loại theo chủ đề nhạy cảm —
        # nguyên tắc không né tránh: giữ nguyên để người đọc tự đánh giá.
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

    # -- Builders --------------------------------------------------------

    def _simple_claims(self, sources):
        """
        Fallback khi không có crossref_result: mỗi source đóng góp đúng 1
        claim lấy từ câu đầu tiên của text, count=1 (chưa được đối chiếu).
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
        Mỗi claim -> 1 perspective:
          - count >= 2 : góc nhìn "đồng thuận" (nhiều nguồn cùng nêu)
          - count == 1 : "quan điểm riêng" (chỉ 1 nguồn nêu)
        Mỗi view đều mở đầu bằng "Theo [url], ..." để rõ nguồn.
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
        Tìm góc khuất: entity nào trong topic/query mà KHÔNG source nào
        đề cập -> gap.

        Cách làm đơn giản, không cần NLP nặng:
          1. Trích từ khóa có nghĩa từ topic (bỏ stopwords).
          2. Dựng tập hợp toàn bộ từ xuất hiện trong các source.
          3. Từ khóa topic nào vắng mặt trong tập hợp đó -> gap.
        So khớp theo từ nguyên (word-set) thay vì substring để tránh
        false positive kiểu "tin" match trong "tin tức".
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
        Độ tin cậy chéo của 1 source =
            (# claims của source được cross-ref, tức count >= 2)
            / (tổng # claims của source).
        Source không đóng góp claim nào -> score 0.0 (vẫn liệt kê).
        Sắp xếp giảm dần theo score; hòa thì theo url cho ổn định.
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
        """Tổng hợp 2-3 câu: đồng thuận, góc khuất, nguồn tin cậy nhất."""
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
    """Factory cho environment.py — đăng ký deep_think tool."""
    from research.deepthink import DeepThink

    def deep_think(topic: str, sources: list = None) -> dict:
        """Phân tích sâu đa chiều, không né tránh.
        
        Args:
            topic: chủ đề phân tích.
            sources: list {url, title, text}.
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
            "description": "Phân tích sâu đa góc nhìn, đánh giá độ tin cậy nguồn.",
            "schema": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Chủ đề."},
                    "sources": {"type": "array", "description": "List sources.", "default": []},
                },
                "required": ["topic"],
            },
        }
    }
