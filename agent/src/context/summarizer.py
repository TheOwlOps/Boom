"""Tóm tắt heuristic KHÔNG cần LLM cho Boom.

Trích ra 3 thứ quan trọng nhất từ lịch sử hội thoại cũ trước khi nén:
  - decisions:    câu chứa "quyết định", "chọn", "chốt", "decided", ...
  - action items: câu chứa "cần", "phải", "todo", "deadline", ...
  - entities:     URL, đường dẫn file, tên riêng (người/dự án/module)

Output của summarize() là bullet list tiếng Việt, dùng làm nội dung
cho message tóm tắt khi ContextManager.compress().
"""
from __future__ import annotations

import re
from typing import Dict, List, Sequence


# ---------------------------------------------------------------------------
# Pattern trích xuất (regex biên dịch sẵn để chạy nhanh, không cần LLM)
# ---------------------------------------------------------------------------

# Câu chứa các từ này được coi là một quyết định đã chốt.
_DECISION_RES = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"quyết định",
        r"\bchốt\b",
        r"\bchọn\b",
        r"\blựa chọn\b",
        r"\bđồng ý\b",
        r"\bdecided?\b",
        r"\bdecision\b",
        r"\bagreed?\b",
        r"\bconclusion\b",
    )
]

# Câu chứa các từ này được coi là việc cần làm tiếp theo.
_ACTION_RES = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bcần\b",
        r"\bphải\b",
        r"\btodo\b",
        r"\baction items?\b",
        r"\bnhiệm vụ\b",
        r"\bgiao cho\b",
        r"\bdeadline\b",
        r"\bhạn chót\b",
        r"\btiếp theo\b",
    )
]

_URL_RE = re.compile(r"https?://[^\s<>\")\]]+")

# Đường dẫn kiểu agent/src/x.py, /tmp/a/b/, ~/workspace/c/
_FILE_RE = re.compile(
    r"(?:~?/(?:[\w.\-]+/)+[\w.\-]+|(?:[\w.\-]+/)+[\w.\-]+\.\w{1,6})"
)
# Tên file đơn lẻ có đuôi mở rộng quen thuộc (manager.py, README.md, ...)
_BAREFILE_RE = re.compile(
    r"\b[\w\-]+\.(?:py|ts|js|tsx|jsx|md|json|yaml|yml|toml|sh|txt|go|rs|java)\b"
)

# Tên riêng: chuỗi 1-4 từ, mỗi từ bắt đầu bằng chữ hoa (kiểm tra bằng
# str.isupper() nên tự hỗ trợ mọi dấu tiếng Việt + CamelCase như BoomBot).
_WORD_SEQ_RE = re.compile(r"[^\W\d_]\w*(?:[ \t]+[^\W\d_]\w*){0,3}")

# Từ viết hoa thường chỉ là đầu câu, không phải tên riêng → loại bỏ.
_NAME_STOPWORDS = frozenset(
    "Tôi Anh Em Bạn Chúng Ta Hôm Đây Trong Ngoài Khi Nếu Vì Với Của Đã Đang Sẽ "
    # Từ viết hoa thường chỉ là đầu câu, không phải tên riêng → loại bỏ.
    "Một Các Những Không Có Là Được Này Kia Việc Người Sau Trước Tại Theo Từ Đến "
    "Cho Về Trên Dưới Mỗi Tất Rất Quá Chỉ Cũng Vẫn Lại Hãy Xin Nào Gì Ai Đâu Sao "
    "Thế Vậy Nhưng Mà Thì Hay Và Hoặc Xem Thêm Cần Phải".split()
)

# Tách câu theo dấu kết thúc câu hoặc xuống dòng.
_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")


def _sentences(text: str) -> List[str]:
    """Tách text thành các câu, bỏ câu rỗng."""
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]


def _content(msg) -> str:
    """Lấy content từ Message (pydantic) hoặc dict."""
    if isinstance(msg, dict):
        return msg.get("content") or ""
    return getattr(msg, "content", "") or ""


def _role(msg) -> str:
    if isinstance(msg, dict):
        return msg.get("role") or ""
    return getattr(msg, "role", "") or ""


def _dedupe(items: Sequence[str]) -> List[str]:
    """Loại trùng, giữ nguyên thứ tự xuất hiện."""
    seen = set()
    out: List[str] = []
    for it in items:
        key = it.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(it.strip())
    return out


class HeuristicSummarizer:
    """Tóm tắt bằng heuristic, không gọi LLM (chạy offline, nhanh, rẻ)."""

    # -- trích quyết định ---------------------------------------------------
    def extract_decisions(self, messages: Sequence, limit: int = 10) -> List[str]:
        """Trả về các câu chứa quyết định đã chốt."""
        found: List[str] = []
        for msg in messages:
            for sent in _sentences(_content(msg)):
                if any(rx.search(sent) for rx in _DECISION_RES):
                    found.append(sent)
        return _dedupe(found)[:limit]

    # -- trích việc cần làm ---------------------------------------------------
    def extract_action_items(self, messages: Sequence, limit: int = 10) -> List[str]:
        """Trả về các câu chứa việc cần làm tiếp theo."""
        found: List[str] = []
        for msg in messages:
            for sent in _sentences(_content(msg)):
                if any(rx.search(sent) for rx in _ACTION_RES):
                    found.append(sent)
        return _dedupe(found)[:limit]

    # -- trích thực thể -------------------------------------------------------
    def extract_entities(self, messages: Sequence) -> Dict[str, List[str]]:
        """Trả về dict {urls, files, names} trích từ toàn bộ messages."""
        urls: List[str] = []
        files: List[str] = []
        names: List[str] = []
        for msg in messages:
            text = _content(msg)
            url_hits = [u.rstrip(".,;") for u in _URL_RE.findall(text)]
            urls.extend(url_hits)
            # Bỏ file path nào thực chất là mảnh của URL (vd "/github.com/...").
            for f in _FILE_RE.findall(text) + _BAREFILE_RE.findall(text):
                if not any(f in u for u in url_hits):
                    files.append(f)
            for m in _WORD_SEQ_RE.finditer(text):
                # Chỉ giữ các từ đầu viết hoa liên tiếp (tên riêng thật).
                words = [w for w in m.group(0).split()]
                name_words: List[str] = []
                for w in words:
                    if w and w[0].isupper():
                        name_words.append(w)
                    else:
                        break
                if not name_words:
                    continue
                name = " ".join(name_words)
                # Bỏ tên quá ngắn hoặc nằm trong stopwords (từ đầu câu).
                if len(name) >= 2 and name_words[0] not in _NAME_STOPWORDS:
                    names.append(name)
        return {
            "urls": _dedupe(urls)[:10],
            "files": _dedupe(files)[:10],
            "names": _dedupe(names)[:15],
        }

    # -- tóm tắt tổng hợp ------------------------------------------------------
    def summarize(self, messages: Sequence) -> str:
        """Tóm tắt messages thành bullet list tiếng Việt.

        Giữ lại: quyết định, việc cần làm, thực thể (URL/file/tên riêng)
        và thống kê số lượng message theo role.
        """
        messages = list(messages)
        if not messages:
            return "- (không có nội dung để tóm tắt)"

        decisions = self.extract_decisions(messages)
        actions = self.extract_action_items(messages)
        entities = self.extract_entities(messages)

        lines: List[str] = []
        if decisions:
            lines.append(f"- Quyết định đã chốt ({len(decisions)}):")
            lines.extend(f"  - {d}" for d in decisions[:5])
        if actions:
            lines.append(f"- Việc cần làm ({len(actions)}):")
            lines.extend(f"  - {a}" for a in actions[:8])

        ent_lines: List[str] = []
        ent_lines.extend(f"  - URL: {u}" for u in entities["urls"][:8])
        ent_lines.extend(f"  - File: {f}" for f in entities["files"][:8])
        ent_lines.extend(f"  - Tên: {n}" for n in entities["names"][:10])
        if ent_lines:
            lines.append("- Thực thể liên quan:")
            lines.extend(ent_lines)

        # Thống kê nhanh để người đọc hình dung quy mô đoạn đã nén.
        counts: Dict[str, int] = {}
        for msg in messages:
            r = _role(msg)
            counts[r] = counts.get(r, 0) + 1
        stat = ", ".join(f"{counts.get(r, 0)} {r}" for r in ("system", "user", "assistant", "tool") if counts.get(r))
        lines.append(f"- Tổng quan: {len(messages)} messages ({stat}).")

        return "\n".join(lines)
