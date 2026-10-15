"""Tool registry wrappers for the verify package.

``build_tools(env=None)`` returns registry-ready dicts in the same shape as
the rest of ``agent/src/tools`` (e.g. ``scraper.build_tools``):

    {"<tool_name>": {"name": ..., "func": ..., "description": ...,
                    "schema": {"type": "object", "properties": {...},
                               "required": [...]}}}

The ``llm`` and ``search_fn`` are read LAZILY from ``env`` inside each tool
function (``getattr(env, "llm", None)`` / ``getattr(env, "search_fn", None)``)
so ``build_tools`` never requires a configured environment up front. When no
``llm`` is configured the tool returns an honest
``{"error": "no llm configured"}`` instead of crashing.

Standard library only (``typing``); all logic lives in the sibling modules.
"""

from __future__ import annotations

from typing import Any, Dict

from verify.verifier import verify


def _require_llm(env):
    """Return ``env.llm`` or None; never raises."""
    return getattr(env, "llm", None) if env is not None else None


def build_tools(env=None) -> Dict[str, Dict[str, Any]]:
    """Registry-ready tool dicts for agent verification."""

    def verify_text(text: str, kind: str = "text") -> Dict[str, Any]:
        """Adversarially verify agent-written text (or fact/plan/code).

        Runs the full ``verify`` pipeline (critic + per-kind checks +
        Self-Refine). Returns the pipeline dict
        ``{"verified", "issues", "refined", "checks"}``.
        """
        llm = _require_llm(env)
        if llm is None:
            return {"error": "no llm configured"}
        search_fn = getattr(env, "search_fn", None) if env is not None else None
        try:
            return verify(text, kind, llm, search_fn=search_fn)
        except Exception as exc:
            return {"error": "verify_text failed: %s" % exc}

    def verify_code(code: str, language: str = "python") -> Dict[str, Any]:
        """Verify Python code: run it for real, self-debug failures.

        Runs the full ``verify`` pipeline with ``kind="code"``
        (``run_code_check`` + ``self_debug`` + critic). Returns the pipeline
        dict ``{"verified", "issues", "refined", "checks"}``.
        """
        llm = _require_llm(env)
        if llm is None:
            return {"error": "no llm configured"}
        try:
            return verify(code, "code", llm)
        except Exception as exc:
            return {"error": "verify_code failed: %s" % exc}

    return {
        "verify_text": {
            "name": "verify_text",
            "func": verify_text,
            "description": (
                "Verify agent-written text (or fact/plan) with an adversarial "
                "critic plus Self-Refine: returns verified/refined text, the "
                "issue list, and per-check flags. For 'fact' kind a configured "
                "search_fn is used to check claims against web evidence."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text (or fact/plan) to verify.",
                    },
                    "kind": {
                        "type": "string",
                        "description": (
                            "One of 'text', 'fact', 'plan', 'code'. "
                            "Default 'text'."
                        ),
                    },
                },
                "required": ["text"],
            },
        },
        "verify_code": {
            "name": "verify_code",
            "func": verify_code,
            "description": (
                "Verify Python code for real: execute it in a subprocess, "
                "self-debug failures with the LLM up to a few iterations, "
                "then adversarially re-critique. Returns verified flag, "
                "the fixed code, issues, and check flags."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The Python source code to verify.",
                    },
                    "language": {
                        "type": "string",
                        "description": (
                            "Only 'python' is supported; anything else returns "
                            "an honest 'unsupported language' error. "
                            "Default 'python'."
                        ),
                    },
                },
                "required": ["code"],
            },
        },
    }
