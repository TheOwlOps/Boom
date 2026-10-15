"""Agent output verification: adversarial critique, real code checks, fact-checking.

Public API::

    from verify import (
        critique,        # adversarial critique via LLM  (critic.py)
        run_code_check,  # run Python code for real     (codecheck.py)
        self_debug,      # fix-and-retry loop           (codecheck.py)
        split_claims,    # split text into claims       (factcheck.py)
        check_claims,    # verify claims via search     (factcheck.py)
        verify,          # full pipeline + Self-Refine   (verifier.py)
        build_tools,     # tool registry dicts          (tools.py)
    )

Standard library only. The ``llm`` injected anywhere in this package is a
callable ``(prompt: str) -> str``; it is never called at import time.
"""

from verify.critic import critique
from verify.codecheck import run_code_check, self_debug
from verify.factcheck import check_claims, split_claims
from verify.verifier import verify
from verify.tools import build_tools

__all__ = [
    "critique",
    "run_code_check",
    "self_debug",
    "split_claims",
    "check_claims",
    "verify",
    "build_tools",
]
