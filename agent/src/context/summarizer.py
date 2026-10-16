"""Heuristic summarization WITHOUT an LLM for Boom.

Extracts the 3 most important things from old conversation history before
compressing:
  - decisions:    sentences containing "quyết định", "chọn", "chốt", "decided", ...
  - action items: sentences containing "cần", "phải", "todo", "deadline", ...
  - entities:     URLs, file paths, proper names (people/projects/modules)

summarize()'s output is a bullet list (kept in Vietnamese — pinned by
evals/test_context.py), used as the summary message content when
ContextManager.compress().
"""
from __future__ import annotations

import re
from typing import Dict, List, Sequence


# ---------------------------------------------------------------------------
# Extraction patterns (precompiled regexes for speed, no LLM needed).
# NOTE: the keyword/stopword lists below are functional Vietnamese data —
# kept as-is.
# ---------------------------------------------------------------------------

# A sentence containing these words counts as a finalized decision.
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

# A sentence containing these words counts as upcoming work.
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

# Paths like agent/src/x.py, /tmp/a/b/, ~/workspace/c/
_FILE_RE = re.compile(
    r"(?:~?/(?:[\w.\-]+/)+[\w.\-]+|(?:[\w.\-]+/)+[\w.\-]+\.\w{1,6})"
)
# Lone filenames with familiar extensions (manager.py, README.md, ...)
_BAREFILE_RE = re.compile(
    r"\b[\w\-]+\.(?:py|ts|js|tsx|jsx|md|json|yaml|yml|toml|sh|txt|go|rs|java)\b"
)

# Proper names: 1-4 word runs, each starting uppercase (checked with
# str.isupper(), so it natively handles Vietnamese diacritics + CamelCase
# like BoomBot).
_WORD_SEQ_RE = re.compile(r"[^\W\d_]\w*(?:[ \t]+[^\W\d_]\w*){0,3}")

# Capitalized words that are usually just sentence starts, not names → drop.
_NAME_STOPWORDS = frozenset(
    "Tôi Anh Em Bạn Chúng Ta Hôm Đây Trong Ngoài Khi Nếu Vì Với Của Đã Đang Sẽ "
    "Một Các Những Không Có Là Được Này Kia Việc Người Sau Trước Tại Theo Từ Đến "
    "Cho Về Trên Dưới Mỗi Tất Rất Quá Chỉ Cũng Vẫn Lại Hãy Xin Nào Gì Ai Đâu Sao "
    "Thế Vậy Nhưng Mà Thì Hay Và Hoặc Xem Thêm Cần Phải".split()
)

# Split sentences on sentence-ending punctuation or newlines.
_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")


def _sentences(text: str) -> List[str]:
    """Splits text into sentences, dropping empties."""
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]


def _content(msg) -> str:
    """Gets content from a Message (pydantic) or a dict."""
    if isinstance(msg, dict):
        return msg.get("content") or ""
    return getattr(msg, "content", "") or ""


def _role(msg) -> str:
    if isinstance(msg, dict):
        return msg.get("role") or ""
    return getattr(msg, "role", "") or ""


def _dedupe(items: Sequence[str]) -> List[str]:
    """Dedupes, preserving order of appearance."""
    seen = set()
    out: List[str] = []
    for it in items:
        key = it.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(it.strip())
    return out


class HeuristicSummarizer:
    """Heuristic summarizer, no LLM calls (offline, fast, cheap)."""

    # -- extract decisions ------------------------------------------------
    def extract_decisions(self, messages: Sequence, limit: int = 10) -> List[str]:
        """Returns sentences containing finalized decisions."""
        found: List[str] = []
        for msg in messages:
            for sent in _sentences(_content(msg)):
                if any(rx.search(sent) for rx in _DECISION_RES):
                    found.append(sent)
        return _dedupe(found)[:limit]

    # -- extract action items ------------------------------------------------
    def extract_action_items(self, messages: Sequence, limit: int = 10) -> List[str]:
        """Returns sentences containing upcoming work."""
        found: List[str] = []
        for msg in messages:
            for sent in _sentences(_content(msg)):
                if any(rx.search(sent) for rx in _ACTION_RES):
                    found.append(sent)
        return _dedupe(found)[:limit]

    # -- extract entities ----------------------------------------------------
    def extract_entities(self, messages: Sequence) -> Dict[str, List[str]]:
        """Returns a dict {urls, files, names} extracted from all messages."""
        urls: List[str] = []
        files: List[str] = []
        names: List[str] = []
        for msg in messages:
            text = _content(msg)
            url_hits = [u.rstrip(".,;") for u in _URL_RE.findall(text)]
            urls.extend(url_hits)
            # Drop file paths that are really URL fragments (e.g. "/github.com/...").
            for f in _FILE_RE.findall(text) + _BAREFILE_RE.findall(text):
                if not any(f in u for u in url_hits):
                    files.append(f)
            for m in _WORD_SEQ_RE.finditer(text):
                # Keep only runs of capitalized words (real proper names).
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
                # Drop too-short names or sentence-start stopwords.
                if len(name) >= 2 and name_words[0] not in _NAME_STOPWORDS:
                    names.append(name)
        return {
            "urls": _dedupe(urls)[:10],
            "files": _dedupe(files)[:10],
            "names": _dedupe(names)[:15],
        }

    # -- combined summary ----------------------------------------------------
    def summarize(self, messages: Sequence) -> str:
        """Summarizes messages into a bullet list (Vietnamese, pinned by evals).

        Keeps: decisions, action items, entities (URL/file/proper names)
        and per-role message counts.
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

        # Quick stats so readers can picture the compressed segment's scale.
        counts: Dict[str, int] = {}
        for msg in messages:
            r = _role(msg)
            counts[r] = counts.get(r, 0) + 1
        stat = ", ".join(f"{counts.get(r, 0)} {r}" for r in ("system", "user", "assistant", "tool") if counts.get(r))
        lines.append(f"- Tổng quan: {len(messages)} messages ({stat}).")

        return "\n".join(lines)
