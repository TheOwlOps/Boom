"""Verify đa vòng — critic tìm lỗi, revise sửa đáp án.

Quy trình:
  - Vòng 1: LLM trả lời bài toán.
  - Vòng 2..N: LLM đóng vai critic ("Tìm lỗi trong đáp án sau:...").
    + Nếu critic không tìm thấy lỗi → dừng sớm, giữ nguyên đáp án.
    + Nếu tìm thấy lỗi → LLM revise lại đáp án, lặp tiếp vòng critic.

API:
    from verify import verify_answer
    result = verify_answer(llm_fn, "Nếu A>B và B>C thì A so với C như thế nào?")
    # {"final_answer": ..., "rounds": [...], "revised": bool}

Chỉ dùng stdlib (re). LLM được inject nên dễ test với mock.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List

# Cụm báo "không có lỗi" từ critic (hỗ trợ cả tiếng Việt và tiếng Anh).
_NO_ERROR_RE = re.compile(
    r"(không có lỗi|không thấy lỗi|no (issues?|errors?|problems?) (found|detected))",
    re.IGNORECASE,
)


def _critic_prompt(problem: str, answer: str) -> str:
    return (
        f"Bài toán: {problem}\n\n"
        f"Đáp án được đề xuất: {answer}\n\n"
        "Tìm lỗi trong đáp án sau: kiểm tra kỹ lập luận và kết quả. "
        "Nếu không thấy lỗi, chỉ trả lời đúng cụm \"KHÔNG CÓ LỖI\". "
        "Nếu có lỗi, mô tả rõ lỗi ở đâu và vì sao."
    )


def _revise_prompt(problem: str, answer: str, critique: str) -> str:
    return (
        f"Bài toán: {problem}\n\n"
        f"Đáp án hiện tại: {answer}\n\n"
        f"Critic phát hiện lỗi: {critique}\n\n"
        "Sửa lại đáp án cho đúng dựa trên lỗi đã chỉ ra. "
        "Trả lời trực tiếp đáp án đúng, bắt đầu bằng \"Đáp án:\"."
    )


def verify_answer(
    llm_fn: Callable[[str], str],
    problem: str,
    max_rounds: int = 3,
) -> Dict:
    """Chạy vòng verify critic→revise.

    Trả về:
      {"final_answer": str, "rounds": [...], "revised": bool}
    rounds gồm các entry:
      {"round": 1, "kind": "initial", "answer": str}
      {"round": N, "kind": "critic", "critique": str, "found_error": bool}
      {"round": N, "kind": "revise", "answer": str}
    """
    if not callable(llm_fn):
        raise TypeError("llm_fn phải là callable")
    if max_rounds < 1:
        raise ValueError("max_rounds phải >= 1")

    rounds: List[Dict] = []
    current = (llm_fn(f"Bài toán: {problem}\nCho đáp án cuối, bắt đầu bằng \"Đáp án:\".") or "").strip()
    rounds.append({"round": 1, "kind": "initial", "answer": current})

    revised = False
    round_no = 1
    for _ in range(max_rounds - 1):
        round_no += 1
        critique = (llm_fn(_critic_prompt(problem, current)) or "").strip()
        found_error = not _NO_ERROR_RE.search(critique)
        rounds.append({"round": round_no, "kind": "critic",
                       "critique": critique, "found_error": found_error})
        if not found_error:
            # Critic không thấy lỗi → dừng sớm, không revise thừa.
            break
        round_no += 1
        current = (llm_fn(_revise_prompt(problem, current, critique)) or "").strip()
        revised = True
        rounds.append({"round": round_no, "kind": "revise", "answer": current})

    return {"final_answer": current, "rounds": rounds, "revised": revised}
