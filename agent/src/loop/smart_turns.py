"""Smart turn control for the ReAct loop.

The stock ReAct loop runs a fixed ``max_turns`` even when the model is stuck:
repeating the exact same tool call, getting empty observations, or hitting the
same error over and over.  ``TurnController`` watches a cheap signature of each
turn and asks for an early exit when the loop is clearly going nowhere.

This module is stdlib-only so it can live in ``agent/src/loop/smart_turns.py``
without new dependencies.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple


def _call_signature(tool_calls: List[Any]) -> Tuple[Tuple[str, str], ...]:
    """Stable per-turn signature of a list of tool calls.

    Accepts pydantic models (``name``/``arguments`` attributes), plain dicts
    (``{"name": ..., "arguments": ...}``) or (name, arguments) tuples. Two
    turns with the same signature made the same calls with the same arguments.
    """
    parts = []
    for tc in tool_calls or []:
        if isinstance(tc, dict):
            name, args = tc.get("name", ""), tc.get("arguments", {})
        elif isinstance(tc, (tuple, list)) and len(tc) == 2:
            name, args = tc[0], tc[1]
        else:
            name = getattr(tc, "name", "") or ""
            args = getattr(tc, "arguments", {}) or {}
        try:
            args_key = json.dumps(args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            args_key = str(args)
        parts.append((str(name), args_key))
    return tuple(parts)


def _is_blank(text: Any) -> bool:
    """True when the observation carries no information at all."""
    return text is None or not str(text).strip()


def _tool_of_signature(sig: Tuple[Tuple[str, str], ...]) -> str:
    """Tool name a single-call turn targeted, else the empty string."""
    if len(sig) == 1:
        return sig[0][0]
    return ""


class TurnController:
    """Decides when a ReAct loop should stop early.

    Keep one controller per prompt/turn: call ``record_turn()`` after each
    turn, then ``should_continue()`` to learn whether the next turn is worth
    running.
    """

    def __init__(self, max_turns: int = 6, patience: int = 2):
        self.max_turns = max_turns
        self.patience = patience
        # Per-turn tool-call signatures, in order.
        self._signatures: List[Tuple[Tuple[str, str], ...]] = []
        # Flat observation history across turns, in order.
        self._observations: List[str] = []
        # Consecutive error count per tool name: [tool_name, count].
        self._error_tool: str = ""
        self._error_streak: int = 0

    @property
    def turns_used(self) -> int:
        """How many turns have been recorded so far."""
        return len(self._signatures)

    def reset(self) -> None:
        """Forget all history; reuse the controller for a fresh prompt."""
        self._signatures.clear()
        self._observations.clear()
        self._error_tool = ""
        self._error_streak = 0

    def record_turn(self, tool_calls: List[Any], observations: List[str]) -> None:
        """Store this turn's calls and observations for future comparisons."""
        self._signatures.append(_call_signature(tool_calls))
        for obs in observations or []:
            self._observations.append("" if obs is None else str(obs))

    def should_continue(
        self, tool_calls: List[Any], observations: List[str]
    ) -> Tuple[bool, str]:
        """Return (continue?, reason).

        Reasons: ``"repeated_tool_calls"``, ``"empty_observations"``,
        ``"repeated_errors"``, or ``"ok"``.

        Note: the turn bound (``max_turns``) is enforced by the loop's own
        ``for _ in range(max_turns)`` so the ``for...else`` clause can keep
        emitting "Reached max_turns=..." when turns are truly exhausted.
        """
        # Note: the caller records the turn first (record_turn), so the
        # current turn is already the last entry of the history.
        sig = _call_signature(tool_calls)

        # 1) The model issued the exact same calls (names + arguments) as the
        #    previous turn: it is stuck in a loop, more turns will not help.
        if (
            sig
            and len(self._signatures) >= 2
            and self._signatures[-1] == self._signatures[-2]
        ):
            return False, "repeated_tool_calls"

        # 2) Two observations in a row are empty or whitespace-only: the tools
        #    are returning nothing, the model has nothing new to work with.
        if len(self._observations) >= 2 and _is_blank(
            self._observations[-1]
        ) and _is_blank(self._observations[-2]):
            return False, "empty_observations"

        # 3) The same single tool errored `patience + 1` turns in a row
        #    (default: 3). Also catches errors inside the current turn when it
        #    holds several erroring observations for one tool.
        tool = _tool_of_signature(sig)
        obs_texts = [("" if o is None else str(o)) for o in (observations or [])]
        if tool and obs_texts and all("Error" in o for o in obs_texts):
            if tool == self._error_tool:
                self._error_streak += 1
            else:
                self._error_tool = tool
                self._error_streak = 1
        else:
            self._error_tool = ""
            self._error_streak = 0
        if self._error_streak >= self.patience + 1:
            return False, "repeated_errors"

        return True, "ok"
