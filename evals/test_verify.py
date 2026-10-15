"""GOM tests for agent/src/verify (offline: fake llm + fake search_fn).

Run from the staging dir:  python3 evals/test_verify.py
"""
from __future__ import annotations

import json
import os
import sys

STAGING = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(STAGING, "agent", "src"))

from verify import (  # noqa: E402
    build_tools,
    check_claims,
    critique,
    run_code_check,
    self_debug,
    split_claims,
    verify,
)

CHECKS: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    CHECKS.append((name, bool(cond)))


# ---------------------------------------------------------------------------
# Fake LLMs / fake search
# ---------------------------------------------------------------------------
def fake_critic_llm(prompt: str) -> str:
    """Adversarial fake: finds 'division by zero' in code, 'flat-earth' in text."""
    if "division by zero" in prompt:
        return json.dumps({
            "issues": [{
                "severity": "high",
                "issue": "division by zero at runtime",
                "suggestion": "guard against zero divisor",
            }]
        })
    if "flat-earth" in prompt:
        return json.dumps({
            "issues": [{
                "severity": "high",
                "issue": "asserts the Earth is flat",
                "suggestion": "correct to the established spherical shape",
            }]
        })
    return json.dumps({"issues": []})


def fake_fix_llm(prompt: str) -> str:
    """Fix prompt for ``print(undefined_var)`` -> working code."""
    if "undefined_var" in prompt:
        return "```python\nundefined_var = 1\nprint(undefined_var)\n```"
    return prompt.split("=== CODE ===")[1] if "=== CODE ===" in prompt else ""


def fake_bad_llm(prompt: str) -> str:
    """Dishonest/garbage fixer: always returns still-broken code."""
    return "```python\nraise RuntimeError('still broken')\n```"


def fake_split_llm(prompt: str) -> str:
    if "You split text" in prompt:
        return (
            "1. Hà Nội is the capital of Vietnam\n"
            "2. The Eiffel Tower is in Paris\n"
            "3. Water boils at 100°C at sea level"
        )
    if "SUPPORTS:" in prompt or "REFUTES:" in prompt or "(no evidence" in prompt:
        return fake_judge_llm(prompt)
    return ""


def fake_judge_llm(prompt: str) -> str:
    """Judge verdict follows the evidence markers in the prompt."""
    if "REFUTES:" in prompt:
        return '{"verdict": "refuted", "reason": "evidence contradicts the claim"}'
    if "SUPPORTS:" in prompt:
        return '{"verdict": "supported", "reason": "evidence directly states the claim"}'
    return '{"verdict": "uncertain", "reason": "no relevant evidence"}'


def fake_claim_split_llm(prompt: str) -> str:
    """Splitter for the check_claims test: 3 claims routed by keyword."""
    if "You split text" in prompt:
        return (
            "1. the supported claim example\n"
            "2. the refuted claim example\n"
            "3. the unknown claim xyz example"
        )
    return fake_judge_llm(prompt)


def fake_search_fn(query: str):
    """Routes by keyword: support / refute / nothing."""
    q = (query or "").lower()
    if "supported claim" in q:
        return [{"title": "T1", "url": "http://example.com/1",
                 "snippet": "SUPPORTS: the evidence directly states the claim"}]
    if "refuted claim" in q:
        return [{"title": "T2", "url": "http://example.com/2",
                 "snippet": "REFUTES: the evidence contradicts the claim"}]
    return []


def fake_refine_llm(prompt: str) -> str:
    """Critic pass: finds 'bugword'; refine pass: returns text without it."""
    if "WORK TO VERIFY" in prompt:
        if "bugword" in prompt:
            return json.dumps({
                "issues": [{
                    "severity": "high",
                    "issue": "contains the marker bugword",
                    "suggestion": "remove bugword",
                }]
            })
        return json.dumps({"issues": []})
    # Self-Refine prompt (text kind, Vietnamese) -> fixed text.
    return "This is the corrected text with no marker."


def fake_code_verify_llm(prompt: str) -> str:
    """For verify(kind='code'): critic finds nothing, self-debug fixes."""
    if "Sửa code Python" in prompt:
        return "```python\nundefined_var = 1\nprint(undefined_var)\n```"
    if "WORK TO VERIFY" in prompt:
        return json.dumps({"issues": []})
    return "undefined_var = 1\nprint(undefined_var)"


# ---------------------------------------------------------------------------
# 1. critic
# ---------------------------------------------------------------------------
issues = critique("def f(x):\n    return 10 / x  # division by zero when x==0\n",
                  "code", fake_critic_llm)
check("critic finds planted bug", len(issues) >= 1)
check("critic severity is high", issues and issues[0]["severity"] == "high")
check("critic issue mentions division", issues and "division" in issues[0]["issue"].lower())

issues_text = critique("The Earth is flat-earth nonsense claims aside.",
                       "text", fake_critic_llm)
check("critic finds planted false claim", len(issues_text) >= 1
      and issues_text[0]["severity"] == "high")

clean = critique("Water boils at 100°C at sea level under standard pressure.",
                 "fact", fake_critic_llm)
check("critic returns [] for clean content (no fabrication)", clean == [])


# ---------------------------------------------------------------------------
# 2. codecheck: run_code_check
# ---------------------------------------------------------------------------
r = run_code_check('print(2 + 2)')
check("run_code_check good code passes", r["passed"] is True)
check("run_code_check good code output", r["output"].strip() == "4")

r = run_code_check("print(1/0)")
check("run_code_check runtime error fails", r["passed"] is False)
check("run_code_check error has traceback",
      "ZeroDivisionError" in r["error"] and "Traceback" in r["error"])

r = run_code_check("def broken(:")
check("run_code_check syntax error fails", r["passed"] is False
      and "SyntaxError" in r["error"])

r = run_code_check("while True: pass", timeout=1)
check("run_code_check timeout", r["passed"] is False and r["error"] == "timeout after 1s")

r = run_code_check("fn main() {}", language="rust")
check("run_code_check unsupported language",
      r["passed"] is False and r["error"] == "unsupported language: rust")


# ---------------------------------------------------------------------------
# 3. codecheck: self_debug
# ---------------------------------------------------------------------------
sd = self_debug("print(undefined_var)", None, fake_fix_llm)
check("self_debug fixes with fake llm", sd["passed"] is True)
check("self_debug within 2 iters", 0 < sd["iters"] <= 2)
check("self_debug output correct", sd["output"].strip() == "1")

sd = self_debug("print(undefined_var)", None, fake_bad_llm)
check("self_debug honest when llm fixes badly", sd["passed"] is False)
check("self_debug reports max iters on failure", sd["iters"] == 3)


# ---------------------------------------------------------------------------
# 4. factcheck: split_claims
# ---------------------------------------------------------------------------
claims = split_claims("Hà Nội is the capital. The Eiffel Tower is in Paris. "
                      "Water boils at 100°C at sea level.", fake_split_llm)
check("split_claims splits 3 sentences", len(claims) >= 2)
check("split_claims claims are non-empty strings",
      all(isinstance(c, str) and c.strip() for c in claims))
check("split_claims empty text -> []", split_claims("   ", fake_split_llm) == [])


# ---------------------------------------------------------------------------
# 5. factcheck: check_claims
# ---------------------------------------------------------------------------
results = check_claims("irrelevant wrapper", fake_claim_split_llm, fake_search_fn)
by_verdict = {r["claim"]: r["verdict"] for r in results}
check("check_claims runs 3 claims", len(results) == 3)
check("check_claims supported snippet -> supported",
      by_verdict.get("the supported claim example") == "supported")
check("check_claims contradicting snippet -> refuted",
      by_verdict.get("the refuted claim example") == "refuted")
check("check_claims no relevant snippet -> uncertain",
      by_verdict.get("the unknown claim xyz example") == "uncertain")
check("check_claims sources attached",
      all(isinstance(r["sources"], list) for r in results))


# ---------------------------------------------------------------------------
# 6. verify end-to-end kind="text"
# ---------------------------------------------------------------------------
v = verify("This text contains bugword which is wrong.", "text",
           fake_refine_llm, search_fn=None, max_iters=2)
check("verify text verified after refine", v["verified"] is True)
check("verify text refine ran", v["checks"]["refine_iters"] >= 1)
check("verify text refined removed the bug", "bugword" not in v["refined"])
check("verify text critic ran", v["checks"]["critic"] is True)


# ---------------------------------------------------------------------------
# 7. verify end-to-end kind="code"
# ---------------------------------------------------------------------------
v = verify("print(undefined_var)", "code", fake_code_verify_llm,
           search_fn=None, max_iters=2)
check("verify code verified after self-debug", v["verified"] is True)
check("verify code checks['code']", v["checks"]["code"] is True)
check("verify code refined is fixed", "undefined_var = 1" in v["refined"])


# ---------------------------------------------------------------------------
# 8. verify kind="fact" without search_fn -> honest, no crash
# ---------------------------------------------------------------------------
v = verify("Some claim about the world.", "fact", fake_critic_llm,
           search_fn=None, max_iters=2)
check("verify fact w/o search_fn does not crash", isinstance(v, dict))
check("verify fact checks['fact']", v["checks"]["fact"] is True)
check("verify fact has medium issue",
      any(i.get("severity") == "medium" for i in v["issues"]))
check("verify fact no false high issue", v["verified"] is True)


# ---------------------------------------------------------------------------
# 9. build_tools
# ---------------------------------------------------------------------------
class FakeEnv:
    llm = staticmethod(fake_critic_llm)
    search_fn = staticmethod(fake_search_fn)


tools = build_tools(FakeEnv())
check("build_tools returns 2 tools",
      set(tools.keys()) == {"verify_text", "verify_code"})
for tname in ("verify_text", "verify_code"):
    t = tools[tname]
    check(f"tool {tname} has name/func/description/schema",
          t["name"] == tname and callable(t["func"])
          and isinstance(t["description"], str)
          and isinstance(t["schema"], dict))
    check(f"tool {tname} schema is JSON-schema object",
          t["schema"].get("type") == "object"
          and isinstance(t["schema"].get("properties"), dict)
          and isinstance(t["schema"].get("required"), list))

res = tools["verify_text"]["func"]("Some plain text.", kind="text")
check("tool verify_text works with llm env", isinstance(res, dict)
      and "verified" in res)

tools_noenv = build_tools(env=None)
res = tools_noenv["verify_text"]["func"]("x")
check("tool without llm returns honest error",
      res == {"error": "no llm configured"})
res = tools_noenv["verify_code"]["func"]("print(1)")
check("tool verify_code without llm returns honest error",
      res == {"error": "no llm configured"})


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
failed = [name for name, ok in CHECKS if not ok]
print("ran %d checks, %d failed" % (len(CHECKS), len(failed)))
for name in failed:
    print("FAIL:", name)
if failed:
    sys.exit(1)
print("ALL VERIFY TESTS PASSED (%d checks)" % len(CHECKS))
