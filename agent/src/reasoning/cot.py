"""Chain-of-Thought (CoT) — suy luận theo từng bước.

Ý tưởng: thay vì trả lời ngay, tách bài toán thành nhiều bước nhỏ;
mỗi bước gọi LLM một lần, rồi tổng hợp tất cả các bước thành đáp án cuối.
Bài toán phức tạp được chia nhỏ → ít sai sót hơn.

API:
    from cot import ChainOfThought
    cot = ChainOfThought(llm_fn)          # llm_fn(prompt) -> str
    result = cot.think("Nếu A>B và B>C thì A so với C như thế nào?")
    # {"steps": [{"step": 1, "reasoning": ...}, ...], "answer": "A > C"}

Chỉ dùng stdlib (re). LLM được inject nên dễ test với mock.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List

# Tìm dòng "Đáp án: ..." hoặc "Answer: ..." trong câu trả lời tổng hợp.
_ANSWER_RE = re.compile(r"(?:đáp án|answer)\s*:\s*(.+)", re.IGNORECASE)


class ChainOfThought:
    """Suy luận từng bước với LLM inject được."""

    def __init__(self, llm_fn: Callable[[str], str]) -> None:
        """llm_fn: callable nhận prompt (str) và trả về chuỗi text."""
        if not callable(llm_fn):
            raise TypeError("llm_fn phải là callable")
        self.llm_fn = llm_fn

    def _step_prompt(self, problem: str, history: List[str], step: int, max_steps: int) -> str:
        """Dựng prompt cho từng bước suy nghĩ."""
        prompt = f"Bài toán: {problem}\n\n"
        if history:
            prompt += "Các bước đã suy nghĩ:\n"
            for i, h in enumerate(history, start=1):
                prompt += f"- Bước {i}: {h}\n"
            prompt += "\n"
        prompt += (
            f"Bước {step} (trong tối đa {max_steps} bước): "
            "Viết suy luận tiếp theo, chỉ tập trung vào bước này."
        )
        return prompt

    def _summary_prompt(self, problem: str, steps: List[str]) -> str:
        """Dựng prompt tổng hợp thành đáp án cuối."""
        prompt = f"Bài toán: {problem}\n\nCác bước suy luận:\n"
        for i, s in enumerate(steps, start=1):
            prompt += f"{i}. {s}\n"
        prompt += (
            "\nDựa vào các bước trên, cho đáp án cuối cùng, "
            "bắt đầu dòng đáp án bằng \"Đáp án:\"."
        )
        return prompt

    def think(self, problem: str, max_steps: int = 5) -> Dict:
        """Chạy CoT: max_steps bước suy luận + 1 lần tổng hợp.

        Trả về: {"steps": [{"step": int, "reasoning": str}, ...], "answer": str}
        """
        if max_steps < 1:
            raise ValueError("max_steps phải >= 1")

        steps: List[Dict] = []
        history: List[str] = []
        for n in range(1, max_steps + 1):
            reasoning = (self.llm_fn(self._step_prompt(problem, history, n, max_steps)) or "").strip()
            steps.append({"step": n, "reasoning": reasoning})
            history.append(reasoning)

        raw_answer = (self.llm_fn(self._summary_prompt(problem, history)) or "").strip()
        match = _ANSWER_RE.search(raw_answer)
        # Nếu LLM tuân thủ "Đáp án:", trích lấy; không thì lấy toàn bộ câu trả lời.
        answer = match.group(1).strip() if match else raw_answer

        return {"steps": steps, "answer": answer}
