"""DeepSearch core cho Boom: nghiên cứu đa hop (multi-hop) qua web.

Luồng làm việc của ``DeepSearch.research(query)``:
  1. web_search(query) -> danh sách sources
  2. web_fetch từng source -> trích text
  3. Trích entities/keywords mới từ text bằng regex đơn giản
     (cụm viết hoa 2+ từ, ngày tháng, số liệu)
  4. Sinh follow-up queries từ entities chưa explore
  5. Lặp lại cho tới ``max_hops`` hoặc hết query mới

Triết lý: stdlib + httpx (qua agent/src/tools/web.py), không cần API key.
Deploy vào: agent/src/research/deepsearch.py
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

__all__ = ["DeepSearch"]

# --- Import web.py thật khi chạy trong repo; standalone test thì để None ---
try:
    from tools.web import web_search as _real_search  # type: ignore
    from tools.web import web_fetch as _real_fetch  # type: ignore
except ImportError:  # standalone (test): cho phép inject mock qua constructor
    _real_search = None
    _real_fetch = None

# --- lifecycle/timeout cho mỗi fetch; không import được thì bỏ qua ---
try:
    from lifecycle.timeout import with_timeout  # type: ignore
except ImportError:
    with_timeout = None  # type: ignore[assignment]

FETCH_TIMEOUT = 30.0      # giây cho mỗi lần fetch source
MAX_TEXT_CHARS = 20_000   # cắt text mỗi source để entities không bị nhiễu quá dài
FOLLOWUPS_PER_HOP = 3     # số follow-up query tối đa sinh ra mỗi hop

# Cụm viết hoa 2+ từ: chữ cái đầu mỗi từ là in hoa (kể cả tiếng Việt có dấu),
# cho phép tối đa 4 từ để không nuốt cả câu.
_CAP_LETTER = r"[A-ZÀ-ÝĂĐĨŨƠƯẠ-Ỹ]"
_CAP_WORD = _CAP_LETTER + r"[\wÀ-ỹ\-]*"
_RE_CAP_PHRASE = re.compile(r"\b" + _CAP_WORD + r"(?:\s+" + _CAP_WORD + r"){1,3}\b")

# Ngày tháng dạng 14/03/2023, 1-2-24, 2026-10-15 không khớp (năm trước) -> chấp nhận.
_RE_DATE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")

# Số liệu: 175, 3.5, 10,000 kèm đơn vị tùy chọn (triệu, tỷ, %, USD, VND...).
_RE_NUMBER = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:triệu|tỷ|nghìn|%|USD|VND|đồng|người)?\b"
)


class DeepSearch:
    """Nghiên cứu đa hop: search -> fetch -> trích entities -> follow-up -> lặp.

    Args:
        search_fn: hàm ``(query) -> [{title, url, snippet}]``. Mặc định dùng
            ``tools.web.web_search`` nếu import được.
        fetch_fn: hàm ``(url) -> {url, text, title, ...}``. Mặc định dùng
            ``tools.web.web_fetch`` (đã có circuit breaker + retry bên trong).
        Nếu cả hai đều None và web.py import thất bại -> raise RuntimeError.
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
                "DeepSearch cần search_fn/fetch_fn: web.py không import được "
                "và không có mock nào được inject."
            )
        # Bọc fetch bằng timeout của lifecycle nếu có; nếu không thì gọi trực tiếp.
        base_fetch = self._fetch_fn
        if with_timeout is not None:
            self._fetch_fn = with_timeout(FETCH_TIMEOUT)(base_fetch)

    # ------------------------------------------------------------------
    # Trích entities
    def _extract_entities(self, text: str) -> List[str]:
        """Trích entities/keywords từ text bằng 3 regex, giữ thứ tự xuất hiện.

        Trả về danh sách duy nhất (dedupe, so sánh không phân biệt hoa/thường)
        theo thứ tự: cụm viết hoa -> ngày tháng -> số liệu.
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
            # Bỏ số trần trụi quá ngắn (vd "1", "2") vì nhiễu; giữ số có đơn vị
            # hoặc >= 3 chữ số. Kiểm tra đơn vị bằng cách xem match có chữ không.
            num = m.group(0).strip()
            has_unit = bool(re.search(r"[a-zA-Z%]", num))
            digits = re.sub(r"\D", "", num)
            if has_unit or len(digits) >= 3:
                _add(num)
        return found

    # ------------------------------------------------------------------
    # Sinh follow-up query
    @staticmethod
    def _followup_query(entity: str, original_query: str, first: bool) -> str:
        """Sinh câu hỏi đào sâu từ một entity chưa explore.

        Entity đầu tiên của hop -> "{entity} là gì" (hỏi định nghĩa);
        các entity sau -> "{entity} {original_query}" (hỏi trong ngữ cảnh gốc).
        """
        if first:
            return f"{entity} là gì"
        return f"{entity} {original_query}"

    # ------------------------------------------------------------------
    # Fetch an toàn: lỗi mạng của 1 source không được giết cả vòng research
    def _safe_fetch(self, url: str) -> Dict[str, Any]:
        try:
            page = self._fetch_fn(url) or {}
        except Exception as exc:  # noqa: BLE001 - fetch lỗi thì bỏ source này
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
        """Chạy nghiên cứu đa hop.

        Args:
            query: câu hỏi gốc.
            max_hops: số vòng search tối đa.
            max_sources: tổng số source tối đa được fetch.

        Returns:
            {
              "query": query gốc,
              "sources": [{"url", "title", "text", "hop"}],
              "entities": [entity strings theo thứ tự xuất hiện],
              "hops_used": số hop thực tế đã chạy,
            }
        """
        query = (query or "").strip()
        if not query:
            raise ValueError("query must be a non-empty string")
        max_hops = max(1, int(max_hops))
        max_sources = max(1, int(max_sources))

        # Hàng đợi BFS các (query, hop); tránh lặp query bằng seen_queries.
        queue: List[Tuple[str, int]] = [(query, 0)]
        seen_queries: Set[str] = {query.lower()}

        # 2 tập chống trùng: URL đã fetch và entity đã sinh follow-up.
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
                continue  # search lỗi -> bỏ hop này, thử query khác trong queue

            new_entities_this_hop: List[str] = []
            for r in results:
                if len(sources) >= max_sources:
                    break
                url = (r or {}).get("url", "")
                if not url or url in fetched_urls:
                    continue  # dedup: URL đã fetch thì bỏ qua
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

            # Sinh follow-up: chỉ từ entities mới và chưa từng explore,
            # tối đa FOLLOWUPS_PER_HOP mỗi hop để không bùng nổ tổ hợp.
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
