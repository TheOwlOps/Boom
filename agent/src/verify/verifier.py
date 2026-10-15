"""Điều phối verify hoàn chỉnh: critic + codecheck + factcheck + Self-Refine.

Flow:
  B1. critique(result, kind, llm) -> issues
  B2. theo kind:
      - "code": run_code_check -> fail thì self_debug -> re-critique, gộp issues
      - "fact": check_claims (cần search_fn) -> refuted=high, uncertain=low
      - "plan"/"text": chỉ critique
  B3. Self-Refine: lặp sửa theo issues severity "high" tới khi hết high
      hoặc hết max_iters. Với kind=="fact" KHÔNG re-run check_claims trong
      refine để tránh tốn search (issue cũ được carry qua).

verified = True khi không còn issue severity "high".
Mọi exception từ llm trong refine -> dừng refine, giữ bản hiện tại, không crash.
"""

from verify.critic import critique
from verify.codecheck import run_code_check, self_debug
from verify.factcheck import check_claims

VALID_KINDS = ("code", "fact", "plan", "text")


def _has_high(issues):
    return any(i.get("severity") == "high" for i in issues)


def _merge_issues(*issue_lists):
    """Gộp issues, dedupe theo (severity, issue)."""
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
    """Bóc code block ```...``` ra lấy code thô (cho kind code sau refine)."""
    text = (text or "").strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        last = text.rfind("```")
        if first_nl != -1 and last > first_nl:
            return text[first_nl + 1:last].strip()
    return text


def _refine_prompt(kind, content, issues):
    """Dựng prompt refine: sửa content theo đúng issues, giữ nguyên phần đúng."""
    issue_lines = "\n".join(
        "- [%s] %s (gợi ý: %s)" % (
            i.get("severity"), i.get("issue"), i.get("suggestion", "-"))
        for i in issues
    )
    if kind == "code":
        return (
            "Sửa đoạn code Python bên dưới theo đúng các issues liệt kê, "
            "giữ nguyên phần đúng. Trả về bản hoàn chỉnh, chỉ chứa code trong "
            "block ```python:\n\n"
            "```python\n" + content + "\n```\n\n"
            "Issues cần sửa:\n" + issue_lines
        )
    return (
        "Sửa nội dung bên dưới theo đúng các issues liệt kê, giữ nguyên "
        "phần đúng. Trả về bản hoàn chỉnh, không thêm giải thích thừa:\n\n"
        + content + "\n\nIssues cần sửa:\n" + issue_lines
    )


def verify(result, kind, llm, search_fn=None, max_iters=2):
    """Chạy 1 vòng verify hoàn chỉnh + Self-Refine.

    Args:
        result: nội dung cần verify (str).
        kind: "code" | "fact" | "plan" | "text".
        llm: callable nhận prompt (str) -> text (str).
        search_fn: callable cho factcheck; None -> bỏ qua check_claims.
        max_iters: số vòng Self-Refine tối đa.

    Returns:
        {"verified": bool, "issues": [...], "refined": str,
         "checks": {"critic": True, "code": bool, "fact": bool,
                    "refine_iters": int}}
    """
    if kind not in VALID_KINDS:
        raise ValueError("kind must be one of %s, got %r" % (VALID_KINDS, kind))

    refined = result
    refine_iters = 0

    # B1 — critique
    issues = list(critique(result, kind, llm) or [])

    # B2 — theo kind
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
            # re-critique code đã sửa, gộp với issues cũ
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
    # plan/text: chỉ critique (issues B1)

    # B3 — Self-Refine: lặp khi còn high issue và còn lượt
    while _has_high(issues) and refine_iters < max_iters:
        try:
            prompt = _refine_prompt(kind, refined, issues)
            new_text = llm(prompt)
        except Exception:
            # llm lỗi trong refine -> dừng, giữ bản hiện tại, không crash
            break
        refined = _strip_fences(new_text) if kind == "code" else (new_text or "")
        # re-critique bản mới; với fact KHÔNG re-run check_claims (tốn search)
        # nên carry lại các issue fact cũ vào.
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
