"""Streaming helpers for the ReAct loop.

This module is merge-ready for ``agent/src/loop/streaming.py``: it has no
dependency on the provider implementation, only on the event protocol produced
by ``LLMProvider.chat_complete_stream``:

    {"type": "token", "text": str}        # one decoded content chunk
    {"type": "tool_calls", "calls": [...]} # accumulated tool calls, one event per turn
    {"type": "done", "usage": {...}}      # end of stream; usage may be empty

``StreamHandler`` collects those events, forwards every token to an optional
callback as it arrives (so a UI can render the first word immediately), and
returns the final text together with the tool calls for the turn.

``TokenBuffer`` groups raw token chunks into complete sentences so a UI that
forwards callback text verbatim does not flicker mid-sentence.
"""

import inspect
import re
from typing import Any, Callable, Dict, List, Optional, Tuple


class StreamHandler:
    """Consume an LLM event stream and fan tokens out to a callback.

    Parameters
    ----------
    on_token:
        Optional callable invoked with the text of every ``token`` event as
        soon as it arrives. May be a plain function or a coroutine function.
    """

    def __init__(self, on_token: Optional[Callable[[str], Any]] = None):
        self.on_token = on_token

    async def _emit(self, text: str) -> None:
        if self.on_token is None or not text:
            return
        result = self.on_token(text)
        if inspect.isawaitable(result):
            await result

    async def consume(self, stream) -> Tuple[str, List[Dict[str, Any]]]:
        """Drain an event stream.

        Returns ``(full_text, tool_calls)`` where ``tool_calls`` is the list of
        dicts from the final ``tool_calls`` event (``[]`` when the model did not
        request any tool). The ``done`` event's usage is stashed on
        ``self.last_usage`` for the caller to record.
        """
        parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        self.last_usage: Dict[str, Any] = {}

        async for event in stream:
            etype = (event or {}).get("type")
            if etype == "token":
                text = event.get("text") or ""
                if text:
                    parts.append(text)
                    await self._emit(text)
            elif etype == "tool_calls":
                tool_calls = list(event.get("calls") or [])
            elif etype == "done":
                self.last_usage = dict(event.get("usage") or {})
            # Unknown event types are ignored so a provider can extend the
            # protocol without breaking older consumers.

        return "".join(parts), tool_calls


# A sentence ends at . ! ? ... (or a bare newline), guarded against common
# abbreviations so "e.g." or "U.S." does not flush early.
_ABBREVIATIONS = frozenset(
    {"e.g", "i.e", "vs", "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st"}
)
_SENTENCE_END = re.compile(r"([.!?…]+)(?=\s|$)|\n")


class TokenBuffer:
    """Accumulate token chunks and release complete sentences.

    ``feed`` returns every sentence that became complete after appending the
    chunk; ``flush`` returns whatever is left (possibly an empty string).
    """

    def __init__(self):
        self._buf = ""

    def _split_point(self, text: str, match: re.Match) -> Optional[int]:
        """Index just past the sentence-ending punctuation, or None to skip."""
        punct = match.group(1)
        if punct is None:
            # Bare newline: flush the line.
            return match.end()
        # Do not split after an abbreviation like "e.g." or an initial like "J.".
        before = text[: match.start()].rstrip()
        word = before.split()[-1] if before else ""
        stripped = word.rstrip(".")
        if (
            stripped.lower() in _ABBREVIATIONS
            or (len(stripped) == 1 and stripped.isupper())
            or re.search(r"(?:^|\.)[A-Z]$", stripped) is not None  # "U.S."
        ):
            return None
        return match.end(1)

    def feed(self, text: str) -> List[str]:
        """Append a chunk; return the sentences that are now complete."""
        self._buf += text or ""
        sentences: List[str] = []
        search_from = 0
        while True:
            match = _SENTENCE_END.search(self._buf, search_from)
            if match is None:
                break
            point = self._split_point(self._buf, match)
            if point is None:
                # Abbreviation guard fired: keep scanning past this match.
                search_from = match.end()
                if search_from >= len(self._buf):
                    break
                continue
            sentence = self._buf[:point].strip()
            self._buf = self._buf[point:].lstrip()
            if sentence:
                sentences.append(sentence)
            search_from = 0
        return sentences

    def flush(self) -> str:
        """Return and clear any buffered (incomplete) text."""
        text = self._buf.strip()
        self._buf = ""
        return text
