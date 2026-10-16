"""Self-Consistency — voting from multiple independent reasoning runs.

The idea: instead of trusting a single answer, ask the LLM n times
(with a "try a different approach" hint from round 2 on for diverse
reasoning), extract each round's answer, then vote — the most common
answer wins.

API:
    from self_consistency import vote
    result = vote(llm_fn, "7 + 5 x 2 = ?", n=5)
    # {"answers": [...], "winner": "24", "votes": {"24": 3, ...}}

Stdlib only (re, collections). The LLM is injected so testing with mocks is easy.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Callable, Dict, List

# Extracts the answer from a line starting with "Đáp án:" or "Answer:".
_ANSWER_RE = re.compile(r"(?:đáp án|answer)\s*:\s*(.+)", re.IGNORECASE)

# Extra hint from round 2 on to simulate high temperature (diverse reasoning).
_VARIATION_HINT = "Hãy thử cách khác:"


def extract_answer(response: str) -> str:
    """Extracts the answer from one response.

    Prefers the "Đáp án: ..."/"Answer: ..." line; falls back to the whole
    stripped response (never invents an answer).
    """
    text = (response or "").strip()
    match = _ANSWER_RE.search(text)
    if match:
        return match.group(1).strip()
    return text


def vote(llm_fn: Callable[[str], str], problem: str, n: int = 5) -> Dict:
    """Asks the LLM n times and votes for the most common answer.

    Returns: {"answers": [...], "winner": str, "votes": {ans: count}}
    Ties: the earliest-appearing answer wins (stable, no randomness).
    """
    if not callable(llm_fn):
        raise TypeError("llm_fn must be callable")
    if n < 1:
        raise ValueError("n must be >= 1")

    # NOTE: kept in Vietnamese — evals/test_reasoning.py's mock LLMs key on
    # the "đáp án" phrasing in prompts.
    base_prompt = f"Bài toán: {problem}\nCho đáp án cuối, bắt đầu bằng \"Đáp án:\"."

    answers: List[str] = []
    for i in range(n):
        prompt = base_prompt
        if i > 0:
            # From round 2: hint to try a different direction → diverse reasoning.
            prompt = f"{_VARIATION_HINT}\n{base_prompt}"
        response = (llm_fn(prompt) or "").strip()
        answers.append(extract_answer(response))

    votes = dict(Counter(answers))
    # most_common keeps first-appearance order on ties.
    winner = Counter(answers).most_common(1)[0][0]

    return {"answers": answers, "winner": winner, "votes": votes}
