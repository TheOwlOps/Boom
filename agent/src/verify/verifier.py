"""Full verification orchestration: critic + codecheck + factcheck + Self-Refine.

Flow:
  Step 1. critique(result, kind, llm) -> issues
  Step 2. by kind:
      - "code": run_code_check -> on failure self_debug -> re-critique, merge issues
      - "fact": check_claims (needs search_fn) -> refuted=high, uncertain=low
      - "plan"/"text": critique only
  Step 3. Self-Refine: loop fixing "high"-severity issues until none remain
      or max_iters is hit. For kind=="fact" check_claims is NOT re-run
      during refine to avoid burning search (old issues are carried over).

verified = True when no "high"-severity issue remains.
Any llm exception during refine -> stops refining, keeps the current version, no crash.
"""

from verify.critic import critique
from verify.codecheck import run_code_check, self_debug
from verify.factcheck import check_claims

VALID_KINDS = ("code", "fact", "plan", "text")


def _has_high(issues):
    return any(i.get("severity") == "high" for i in issues)


def _merge_issues(*issue_lists):
    """Merges issues, deduping on (severity, issue)."""
    seen = set()
    merged = []
    for issues in issue_lists:
        for i in issues or []:
            key = (i.get("severity"), i.get("issue"))
            if key not in seen:
                seen.add(key)
                merged.append(i)
    return merged


def _strip_fences(text):
    """Unwraps a ```...``` code block to raw code (for kind code after refine)."""
    text = (text or "").strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        last = text.rfind("```")
        if first_nl != -1 and last > first_nl:
            return text[first_nl + 1:last].strip()
    return text


def _refine_prompt(kind, content, issues):
    """Builds the refine prompt: fixes content per the listed issues, keeps what's right."""
    issue_lines = "\n".join(
        "- [%s] %s (suggestion: %s)" % (
            i.get("severity"), i.get("issue"), i.get("suggestion", "-"))
        for i in issues
    )
    if kind == "code":
        return (
            "Fix the Python code below according to the listed issues, "
            "keeping what's correct. Return the complete fixed version, "
            "containing only code in a ```python block:\n\n"
            "```python\n" + content + "\n```\n\n"
            "Issues to fix:\n" + issue_lines
        )
    return (
        "Fix the content below according to the listed issues, keeping "
        "what's correct. Return the complete version, no extra explanations:\n\n"
        + content + "\n\nIssues to fix:\n" + issue_lines
    )


def verify(result, kind, llm, search_fn=None, max_iters=2):
    """Runs 1 complete verify round + Self-Refine.

    Args:
        result: content to verify (str).
        kind: "code" | "fact" | "plan" | "text".
        llm: callable taking a prompt (str) -> text (str).
        search_fn: callable for factcheck; None -> skips check_claims.
        max_iters: max Self-Refine rounds.

    Returns:
        {"verified": bool, "issues": [...], "refined": str,
         "checks": {"critic": True, "code": bool, "fact": bool,
                    "refine_iters": int}}
    """
    if kind not in VALID_KINDS:
        raise ValueError("kind must be one of %s, got %r" % (VALID_KINDS, kind))

    refined = result
    refine_iters = 0

    # Step 1 — critique
    issues = list(critique(result, kind, llm) or [])

    # Step 2 — by kind
    if kind == "code":
        check = run_code_check(result) or {}
        if not check.get("passed"):
            error_hint = check.get("error") or check.get("output") or "unknown error"
            dbg = self_debug(result, error_hint=error_hint, llm=llm, max_iters=2) or {}
            refined = dbg.get("code") or result
            if not dbg.get("passed"):
                issues = _merge_issues(issues, [{
                    "severity": "high",
                    "issue": "Code still fails after self-debug: %s" % dbg.get("error", error_hint),
                    "suggestion": "Fix manually",
                }])
            # re-critique the fixed code, merge with old issues
            issues = _merge_issues(issues, critique(refined, kind, llm))
        else:
            refined = result
    elif kind == "fact":
        if search_fn is None:
            issues = _merge_issues(issues, [{
                "severity": "medium",
                "issue": "No search_fn provided — factual claims could not be checked",
                "suggestion": "Provide search_fn",
            }])
        else:
            for c in check_claims(result, llm, search_fn) or []:
                claim = str(c.get("claim", ""))[:300]
                verdict = c.get("verdict")
                if verdict == "refuted":
                    issues = _merge_issues(issues, [{
                        "severity": "high",
                        "issue": "Claim refuted: %s" % claim,
                        "suggestion": "Remove or correct this claim.",
                    }])
                elif verdict == "uncertain":
                    issues = _merge_issues(issues, [{
                        "severity": "low",
                        "issue": "Claim uncertain: %s" % claim,
                        "suggestion": "Verify this claim manually.",
                    }])
    # plan/text: critique only (issues from Step 1)

    # Step 3 — Self-Refine: loop while high issues remain and iters are left
    while _has_high(issues) and refine_iters < max_iters:
        try:
            prompt = _refine_prompt(kind, refined, issues)
            new_text = llm(prompt)
        except Exception:
            # llm error during refine -> stops, keeps the current version, no crash
            break
        refined = _strip_fences(new_text) if kind == "code" else (new_text or "")
        # re-critique the new version; for fact check_claims is NOT re-run
        # (costs search), so old fact issues are carried over.
        fresh = list(critique(refined, kind, llm) or [])
        if kind == "fact":
            old_fact = [i for i in issues
                        if str(i.get("issue", "")).startswith(("Claim refuted:", "Claim uncertain:"))]
            fresh = _merge_issues(fresh, old_fact)
        issues = fresh
        refine_iters += 1

    return {
        "verified": not _has_high(issues),
        "issues": issues,
        "refined": refined,
        "checks": {
            "critic": True,
            "code": kind == "code",
            "fact": kind == "fact",
            "refine_iters": refine_iters,
        },
    }
