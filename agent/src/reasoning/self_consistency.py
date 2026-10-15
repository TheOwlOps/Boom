"""Self-Consistency — bỏ phiếu từ nhiều lần suy luận độc lập.

Ý tưởng: thay vì tin một lần trả lời duy nhất, hỏi LLM n lần
(với gợi ý "Hãy thử cách khác" từ vòng 2 trở đi để đa dạng suy luận),
trích đáp án mỗi lần rồi bỏ phiếu — đáp án phổ biến nhất thắng.

API:
    from self_consistency import vote
    result = vote(llm_fn, "7 + 5 x 2 = ?", n=5)
    # {"answers": [...], "winner": "24", "votes": {"24": 3, ...}}

Chỉ dùng stdlib (re, collections). LLM được inject nên dễ test với mock.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Callable, Dict, List

# Trích đáp án từ dòng bắt đầu bằng "Đáp án:" hoặc "Answer:".
_ANSWER_RE = re.compile(r"(?:đáp án|answer)\s*:\s*(.+)", re.IGNORECASE)

# Gợi ý thêm từ vòng 2 trở đi để giả lập temperature cao (đa dạng cách suy luận).
_VARIATION_HINT = "Hãy thử cách khác:"


def extract_answer(response: str) -> str:
    """Trích đáp án từ một lần trả lời.

    Ưu tiên dòng "Đáp án: ..."/"Answer: ..."; không có thì lấy toàn bộ
    câu trả lời đã strip (fallback, không bao giờ bịa ra đáp án).
    """
    text = (response or "").strip()
    match = _ANSWER_RE.search(text)
    if match:
        return match.group(1).strip()
    return text


def vote(llm_fn: Callable[[str], str], problem: str, n: int = 5) -> Dict:
    """Hỏi LLM n lần và bỏ phiếu chọn đáp án phổ biến nhất.

    Trả về: {"answers": [...], "winner": str, "votes": {ans: count}}
    Hòa phiếu: đáp án xuất hiện trước thắng (giữ ổn định, không random).
    """
    if not callable(llm_fn):
        raise TypeError("llm_fn phải là callable")
    if n < 1:
        raise ValueError("n phải >= 1")

    base_prompt = f"Bài toán: {problem}\nCho đáp án cuối, bắt đầu bằng \"Đáp án:\"."

    answers: List[str] = []
    for i in range(n):
        prompt = base_prompt
        if i > 0:
            # Từ vòng 2: gợi ý thử hướng khác → đa dạng suy luận.
            prompt = f"{_VARIATION_HINT}\n{base_prompt}"
        response = (llm_fn(prompt) or "").strip()
        answers.append(extract_answer(response))

    votes = dict(Counter(answers))
    # most_common giữ thứ tự xuất hiện lần đầu khi hòa phiếu.
    winner = Counter(answers).most_common(1)[0][0]

    return {"answers": answers, "winner": winner, "votes": votes}
