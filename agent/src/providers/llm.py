import json
import os
import re
from typing import Any, AsyncIterator, Dict, List, Optional
import httpx
from pydantic import BaseModel

class MissingAPIKeyError(RuntimeError):
    """Raised when a live completion is requested but no API key is configured."""

class ToolCallSpec(BaseModel):
    id: str
    name: str
    arguments: Dict[str, Any]
    args_error: Optional[str] = None

class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

class LLMResponse(BaseModel):
    content: str
    thought: Optional[str] = None
    tool_calls: List[ToolCallSpec] = []
    usage: Optional[LLMUsage] = None
    finish_reason: Optional[str] = None

class LLMProvider:
    """Universal OpenAI-compatible API Provider with Thought Scrubber & Structured Tool Calling."""

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: Optional[str] = None,
        model: str = "gpt-4o",
        timeout: float = 60.0
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY")
        self.model = model
        self.timeout = timeout

    @staticmethod
    def scrub_thought(raw_text: str) -> tuple[Optional[str], str]:
        """Extract <thought>...</thought> blocks from model response."""
        thought_match = re.search(r"<thought>(.*?)</thought>", raw_text, re.DOTALL)
        if thought_match:
            thought = thought_match.group(1).strip()
            clean_text = re.sub(r"<thought>.*?</thought>", "", raw_text, flags=re.DOTALL).strip()
            return thought, clean_text
        return None, raw_text

    @staticmethod
    def parse_usage(raw: Optional[Dict[str, Any]]) -> LLMUsage:
        """Map an OpenAI-compatible `usage` object onto LLMUsage. Absent fields stay 0."""
        u = raw or {}
        completion_details = u.get("completion_tokens_details") or {}
        prompt_details = u.get("prompt_tokens_details") or {}
        return LLMUsage(
            input_tokens=u.get("prompt_tokens") or 0,
            output_tokens=u.get("completion_tokens") or 0,
            cache_read_tokens=prompt_details.get("cached_tokens") or 0,
            cache_write_tokens=prompt_details.get("cache_creation_tokens") or 0,
            reasoning_tokens=completion_details.get("reasoning_tokens") or 0,
        )

    async def chat_complete(
        self,
        messages: List[Dict[str, str]],
        tools_schema: Optional[List[Dict[str, Any]]] = None,
        timeout: Optional[float] = None
    ) -> LLMResponse:
        """Call the completions endpoint. Raises MissingAPIKeyError if no key is configured."""
        if not self.api_key:
            raise MissingAPIKeyError(
                f"No API key configured for provider at {self.base_url}. "
                "Set OPENAI_API_KEY (or OPENROUTER_API_KEY / ANTHROPIC_API_KEY) before running a live turn."
            )

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            # Several OpenAI-compatible gateways default to SSE; force a single JSON body.
            "stream": False,
        }
        if tools_schema:
            payload["tools"] = [
                {"type": "function", "function": t} for t in tools_schema
            ]

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"LLM request failed: HTTP {resp.status_code} from {self.base_url} "
                    f"for model '{self.model}': {resp.text[:400]}"
                )
            data = resp.json()

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"LLM returned no choices for model '{self.model}': {str(data)[:400]}")
        choice = choices[0].get("message") or {}

        raw_content = choice.get("content") or ""
        thought, clean_content = self.scrub_thought(raw_content)
        # Reasoning models expose chain-of-thought on a dedicated field rather than inline tags.
        if not thought and choice.get("reasoning_content"):
            thought = str(choice["reasoning_content"]).strip() or None

        tool_calls = []
        for tc in choice.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            args_error = None
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                args = {}
                args_error = f"{e}: {str(raw_args)[:200]}"
            tool_calls.append(
                ToolCallSpec(
                    id=tc.get("id") or f"call_{len(tool_calls)}",
                    name=fn.get("name") or "",
                    arguments=args,
                    args_error=args_error
                )
            )

        return LLMResponse(
            content=clean_content,
            thought=thought,
            tool_calls=tool_calls,
            usage=self.parse_usage(data.get("usage")),
            finish_reason=choices[0].get("finish_reason")
        )

    async def chat_complete_stream(
        self,
        messages: List[Dict[str, str]],
        tools_schema: Optional[List[Dict[str, Any]]] = None,
        timeout: Optional[float] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream a chat completion as Server-Sent Events.

        Same payload as :meth:`chat_complete` but with ``"stream": True`` and
        ``stream_options.include_usage`` so token counts still arrive. Yields the
        event protocol documented in this file's header. Raises
        ``MissingAPIKeyError`` when no key is configured, and ``RuntimeError`` on
        HTTP errors or provider-sent ``error`` payloads.

        ``client`` is an optional pre-built ``httpx.AsyncClient`` (used by tests
        with a mock transport); when omitted the method creates and closes its own.
        """
        # Same guard as the non-streaming method so both entry points fail alike.
        if not self.api_key:
            raise MissingAPIKeyError(
                f"No API key configured for provider at {self.base_url}. "
                "Set OPENAI_API_KEY (or OPENROUTER_API_KEY / ANTHROPIC_API_KEY) before running a live turn."
            )

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            # Ask for the final usage block; without it streamed turns report 0 tokens.
            "stream_options": {"include_usage": True},
        }
        if tools_schema:
            payload["tools"] = [{"type": "function", "function": t} for t in tools_schema]

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        owned = client is None
        if owned:
            client = httpx.AsyncClient(timeout=timeout or self.timeout)

        try:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise RuntimeError(
                        f"LLM stream failed: HTTP {resp.status_code} from {self.base_url} "
                        f"for model '{self.model}': {body[:400]!r}"
                    )
                async for event in self._iter_sse_events(resp):
                    yield event
        finally:
            if owned:
                await client.aclose()


    async def _iter_sse_events(
        self, resp: "httpx.Response"
    ) -> AsyncIterator[Dict[str, Any]]:
        """Parse one SSE response body into the event protocol.

        Tool calls arrive as deltas keyed by ``index`` (name/id on the first
        chunk, JSON ``arguments`` text split across chunks); they are accumulated
        here and emitted once as a single ``tool_calls`` event so consumers never
        see partial JSON.
        """
        # index -> {"id": ..., "name": ..., "arguments": "..."}
        pending_calls: Dict[int, Dict[str, Any]] = {}
        thought_parts: List[str] = []
        usage: Dict[str, Any] = {}

        async for line in resp.aiter_lines():
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue  # keep-alive comments / malformed frames are not fatal

            if isinstance(chunk, dict) and chunk.get("error"):
                raise RuntimeError(
                    f"LLM stream error from {self.base_url}: {chunk['error']}"
                )

            choices = chunk.get("choices") or []
            delta = (choices[0].get("delta") if choices else {}) or {}

            content = delta.get("content")
            if content:
                yield {"type": "token", "text": content}

            reasoning = delta.get("reasoning_content")
            if reasoning:
                thought_parts.append(reasoning)

            for tc in delta.get("tool_calls") or []:
                index = tc.get("index", 0)
                slot = pending_calls.setdefault(
                    index, {"id": None, "name": "", "arguments": ""}
                )
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

            if chunk.get("usage"):
                usage = self.parse_usage(chunk["usage"]).model_dump()

        calls: List[Dict[str, Any]] = []
        for index in sorted(pending_calls):
            slot = pending_calls[index]
            arguments, args_error = self._parse_tool_call_arguments(slot["arguments"])
            calls.append(
                {
                    "id": slot["id"] or f"call_{index}",
                    "name": slot["name"] or "",
                    "arguments": arguments,
                    "args_error": args_error,
                }
            )
        if calls:
            yield {"type": "tool_calls", "calls": calls}

        yield {"type": "done", "usage": usage, "thought": "".join(thought_parts) or None}


    @staticmethod
    def _parse_tool_call_arguments(raw_args: Any) -> tuple:
        """Parse accumulated tool-call arguments; mirrors the non-streaming logic.

        Returns ``(arguments_dict, args_error_or_None)``.
        """
        if not raw_args:
            return {}, None
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return {}, f"{e}: {str(raw_args)[:200]}"
        return args, None
