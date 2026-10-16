"""Chain-of-Thought (CoT) — step-by-step reasoning.

The idea: instead of answering directly, split the problem into small steps;
call the LLM once per step, then synthesize all steps into the final answer.
A complex problem broken down → fewer mistakes.

API:
    from cot import ChainOfThought
    cot = ChainOfThought(llm_fn)          # llm_fn(prompt) -> str
    result = cot.think("Nếu A>B và B>C thì A so với C như thế nào?")
    # {"steps": [{"step": 1, "reasoning": ...}, ...], "answer": "A > C"}

Stdlib only (re). The LLM is injected so testing with mocks is easy.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List

# Finds a "Đáp án: ..." or "Answer: ..." line in the synthesized answer.
_ANSWER_RE = re.compile(r"(?:đáp án|answer)\s*:\s*(.+)", re.IGNORECASE)


class ChainOfThought:
    """Step-by-step reasoning with an injectable LLM."""

    def __init__(self, llm_fn: Callable[[str], str]) -> None:
        """llm_fn: callable taking a prompt (str) and returning text."""
        if not callable(llm_fn):
            raise TypeError("llm_fn must be callable")
        self.llm_fn = llm_fn

    def _step_prompt(self, problem: str, history: List[str], step: int, max_steps: int) -> str:
        # NOTE: kept in Vietnamese — evals/test_reasoning.py's mock LLMs key on
        # "tìm lỗi" / "sửa lại đáp án" / "đáp án cuối cùng".
        """Builds the prompt for each thinking step."""
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
        # NOTE: kept in Vietnamese — evals/test_reasoning.py's mock LLMs key on
        # "tìm lỗi" / "sửa lại đáp án" / "đáp án cuối cùng".
        """Builds the prompt that synthesizes the final answer."""
        prompt = f"Bài toán: {problem}\n\nCác bước suy luận:\n"
        for i, s in enumerate(steps, start=1):
            prompt += f"{i}. {s}\n"
        prompt += (
            "\nDựa vào các bước trên, cho đáp án cuối cùng, "
            "bắt đầu dòng đáp án bằng \"Đáp án:\"."
        )
        return prompt

    def think(self, problem: str, max_steps: int = 5) -> Dict:
        """Runs CoT: max_steps reasoning steps + 1 synthesis.

        Returns: {"steps": [{"step": int, "reasoning": str}, ...], "answer": str}
        """
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")

        steps: List[Dict] = []
        history: List[str] = []
        for n in range(1, max_steps + 1):
            reasoning = (self.llm_fn(self._step_prompt(problem, history, n, max_steps)) or "").strip()
            steps.append({"step": n, "reasoning": reasoning})
            history.append(reasoning)

        raw_answer = (self.llm_fn(self._summary_prompt(problem, history)) or "").strip()
        match = _ANSWER_RE.search(raw_answer)
        # When the LLM honors "Đáp án:", extract it; otherwise take the whole answer.
        answer = match.group(1).strip() if match else raw_answer

        return {"steps": steps, "answer": answer}
