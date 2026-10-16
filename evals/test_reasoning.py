"""Test cho agent/src/reasoning/ — dùng mock LLM, chạy thật với stdlib.

Chạy:  python3 test_reasoning.py
"""

import os
import sys
from pathlib import Path
import unittest

# Import root is agent/src (repo convention); the modules under test live
# in subpackages there (they previously lived next to this file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from reasoning.cot import ChainOfThought
from reasoning.self_consistency import vote, extract_answer
from reasoning.verify import verify_answer

PROBLEM = "Nếu A>B và B>C thì A so với C như thế nào?"


# ---------- Mock LLM: trả lời đúng ----------
class GoodLLM:
    def __call__(self, prompt: str) -> str:
        if "tổng hợp" in prompt.lower() or "đáp án cuối cùng" in prompt.lower():
            return "Tổng hợp: A>B và B>C nên A>C.\nĐáp án: A > C"
        if "tìm lỗi" in prompt.lower():
            return "KHÔNG CÓ LỖI"
        return "Suy nghĩ trung gian: A lớn hơn B, B lớn hơn C."


# ---------- Mock LLM cho vote: 5 lần trả 3 đáp án đúng, 2 đáp án sai ----------
class VotingLLM:
    def __init__(self):
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        if self.calls in (2, 4):
            return "Đáp án: 18"   # 2 phiếu cho đáp án sai
        return "Đáp án: 24"       # 3 phiếu cho đáp án đúng


# ---------- Mock LLM cho verify: vòng 1 sai, critic thấy lỗi, revise đúng ----------
class FlawedLLM:
    def __call__(self, prompt: str) -> str:
        low = prompt.lower()
        if "tìm lỗi" in low:
            if "A < C" in prompt:
                return "Lỗi: từ A>B và B>C suy ra A>C, không phải A<C."
            return "KHÔNG CÓ LỖI"
        if "sửa lại đáp án" in low:
            return "Đáp án: A > C"
        return "Đáp án: A < C"  # vòng 1 trả lời sai


# ---------- Mock LLM: critic không thấy lỗi → không revise ----------
class CleanLLM:
    def __call__(self, prompt: str) -> str:
        if "tìm lỗi" in prompt.lower():
            return "KHÔNG CÓ LỖI"
        return "Đáp án: A > C"


class TestChainOfThought(unittest.TestCase):
    def test_think_returns_steps_and_answer(self):
        cot = ChainOfThought(GoodLLM())
        result = cot.think(PROBLEM, max_steps=3)
        self.assertEqual(len(result["steps"]), 3)          # đủ 3 bước
        self.assertEqual([s["step"] for s in result["steps"]], [1, 2, 3])
        for s in result["steps"]:
            self.assertTrue(s["reasoning"], "mỗi bước phải có nội dung")
        self.assertIn("A > C", result["answer"])           # đáp án đúng

    def test_think_respects_max_steps(self):
        cot = ChainOfThought(GoodLLM())
        result = cot.think(PROBLEM, max_steps=1)
        self.assertEqual(len(result["steps"]), 1)

    def test_think_rejects_bad_max_steps(self):
        cot = ChainOfThought(GoodLLM())
        with self.assertRaises(ValueError):
            cot.think(PROBLEM, max_steps=0)

    def test_answer_fallback_when_no_answer_line(self):
        cot = ChainOfThought(lambda p: "A > C thôi, không có tiền tố")
        result = cot.think(PROBLEM, max_steps=1)
        self.assertTrue(result["answer"], "fallback phải trả nguyên văn")


class TestSelfConsistency(unittest.TestCase):
    def test_vote_picks_majority_winner(self):
        llm = VotingLLM()
        result = vote(llm, "7 + 5 x 2 = ?", n=5)
        self.assertEqual(len(result["answers"]), 5)
        self.assertEqual(result["winner"], "24")           # 3/5 phiếu
        self.assertEqual(result["votes"], {"24": 3, "18": 2})

    def test_variation_hint_from_round_2(self):
        seen = []
        def llm(p):
            seen.append(p)
            return "Đáp án: X"
        vote(llm, PROBLEM, n=3)
        self.assertNotIn("Hãy thử cách khác", seen[0])     # vòng 1 không có hint
        self.assertIn("Hãy thử cách khác", seen[1])        # vòng 2+ có hint
        self.assertIn("Hãy thử cách khác", seen[2])

    def test_extract_answer_regex(self):
        self.assertEqual(extract_answer("Dài dòng...\nĐáp án: 42"), "42")
        self.assertEqual(extract_answer("Blah\nAnswer: blue"), "blue")
        self.assertEqual(extract_answer("không có tiền tố"), "không có tiền tố")

    def test_vote_tie_breaks_by_first_seen(self):
        calls = {"n": 0}
        def llm(p):
            calls["n"] += 1
            return "Đáp án: B" if calls["n"] % 2 == 0 else "Đáp án: A"
        result = vote(llm, PROBLEM, n=4)                  # A:2, B:2
        self.assertIn(result["winner"], ("A", "B"))       # hòa → ổn định, không crash
        self.assertEqual(result["votes"], {"A": 2, "B": 2})


class TestVerify(unittest.TestCase):
    def test_revise_when_critic_finds_error(self):
        result = verify_answer(FlawedLLM(), PROBLEM, max_rounds=3)
        self.assertTrue(result["revised"], "critic thấy lỗi → phải revise")
        self.assertIn("A > C", result["final_answer"])
        kinds = [r["kind"] for r in result["rounds"]]
        self.assertEqual(kinds[0], "initial")
        self.assertIn("critic", kinds)
        self.assertIn("revise", kinds)

    def test_stop_early_when_no_error(self):
        result = verify_answer(CleanLLM(), PROBLEM, max_rounds=3)
        self.assertFalse(result["revised"], "không lỗi → không revise")
        self.assertIn("A > C", result["final_answer"])
        self.assertEqual(len(result["rounds"]), 2)        # initial + critic, dừng sớm

    def test_respects_max_rounds(self):
        # Critic luôn thấy lỗi → phải dừng sau đúng max_rounds-1 vòng critic
        # (không lặp vô hạn).
        def always_wrong(prompt):
            if "tìm lỗi" in prompt.lower():
                return "Lỗi: vẫn sai."
            return "Đáp án: sai"
        result = verify_answer(always_wrong, PROBLEM, max_rounds=3)
        critic_rounds = [r for r in result["rounds"] if r["kind"] == "critic"]
        self.assertEqual(len(critic_rounds), 2)
        self.assertTrue(result["revised"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
