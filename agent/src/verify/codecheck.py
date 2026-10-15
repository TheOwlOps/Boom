"""Code verification: run Python code for real + self-debugging fix loop.

Contract:
    run_code_check(code, language="python", timeout=10) -> dict
        {"passed": bool, "output": str, "error": str}
    self_debug(code, error_hint, llm, max_iters=3) -> dict
        {"passed": bool, "code": str, "output": str, "error": str, "iters": int}

Stdlib only (subprocess, sys, re). No shell=True.

Safety note: code executes in a separate subprocess with a timeout, but this
is still real code execution. Only run code you trust — this module is NOT a
sandbox and must not be used to execute untrusted/arbitrary hostile input.
"""

import re
import subprocess
import sys

MAX_ERROR_CHARS = 4000
_CODE_FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


def run_code_check(code: str, language: str = "python", timeout: int = 10) -> dict:
    """Run `code` for real and report the result.

    Non-Python languages are reported as unsupported (never faked).
    """
    if language != "python":
        return {"passed": False, "output": "", "error": f"unsupported language: {language}"}
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return {"passed": False, "output": out, "error": f"timeout after {timeout}s"}
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        return {
            "passed": False,
            "output": proc.stdout or "",
            "error": err[:MAX_ERROR_CHARS],
        }
    return {"passed": True, "output": proc.stdout or "", "error": ""}


def _extract_code(response: str) -> str:
    """Pull the fixed code out of an LLM response.

    Prefer a fenced ```python block; fall back to the whole response.
    """
    match = _CODE_FENCE_RE.search(response or "")
    if match:
        return match.group(1).strip("\n")
    return (response or "").strip()


def _build_fix_prompt(code: str, error: str, error_hint) -> str:
    hint_line = f"\nGợi ý thêm: {error_hint}\n" if error_hint else ""
    return (
        "Sửa code Python dưới đây cho chạy đúng, không lỗi.\n"
        "Yêu cầu: trả về TOÀN BỘ code đã sửa, bọc trong ```python ... ```, không giải thích dài.\n"
        f"{hint_line}"
        "=== CODE ===\n"
        f"{code}\n"
        "=== LỖI ===\n"
        f"{error}\n"
    )


def self_debug(code: str, error_hint, llm, max_iters: int = 3) -> dict:
    """Run code; if it fails, ask `llm` to fix it and retry up to max_iters.

    `llm` is a callable(prompt: str) -> str. Honest result: if all iters fail,
    returns passed=False with the last code version and iters=max_iters.
    """
    check = run_code_check(code)
    if check["passed"]:
        return {
            "passed": True,
            "code": code,
            "output": check["output"],
            "error": "",
            "iters": 0,
        }

    current = code
    error = check["error"]
    output = check["output"]
    for it in range(1, max_iters + 1):
        prompt = _build_fix_prompt(current, error, error_hint)
        fixed = _extract_code(llm(prompt))
        check = run_code_check(fixed)
        current, error, output = fixed, check["error"], check["output"]
        if check["passed"]:
            return {
                "passed": True,
                "code": current,
                "output": output,
                "error": "",
                "iters": it,
            }
    return {
        "passed": False,
        "code": current,
        "output": output,
        "error": error,
        "iters": max_iters,
    }


if __name__ == "__main__":
    import json

    def fake_llm(prompt: str) -> str:
        if "name 'x' is not defined" in prompt or "'x'" in prompt:
            return "```python\nx = 1\nprint(x + 1)\n```"
        return prompt.split("=== CODE ===")[1] if "=== CODE ===" in prompt else ""

    checks = [
        ("good code passes", run_code_check('print("hello")')["passed"] is True),
        ("syntax error fails with traceback", "SyntaxError" in run_code_check("def broken(:")["error"]),
        ("runtime error fails with traceback", "NameError" in run_code_check("print(x)")["error"]),
        ("timeout", run_code_check("while True: pass", timeout=1)["error"] == "timeout after 1s"),
        ("unsupported language", run_code_check("x", language="go")["error"] == "unsupported language: go"),
        ("error truncated", len(run_code_check("raise Exception('x'*9000)")["error"]) <= MAX_ERROR_CHARS),
    ]
    sd = self_debug("print(x)", None, fake_llm)
    checks.append(("self_debug fixes with fake llm", sd["passed"] is True and sd["iters"] == 1 and sd["output"].strip() == "2"))
    sd2 = self_debug("import nonexistent_module_xyz", None, fake_llm)
    checks.append(("self_debug honest on unfixable", sd2["passed"] is False and sd2["iters"] == 3))
    print(json.dumps([{k: v for k, v in [c]} for c in checks], indent=1))
    failed = [c[0] for c in checks if not c[1]]
    print("FAIL:", failed if failed else "none")
    sys.exit(1 if failed else 0)
