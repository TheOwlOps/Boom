import asyncio
import json
import re
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from pydantic import BaseModel

from .streaming import StreamHandler
from .smart_turns import TurnController

# How many transcript messages a loop rebuilds when it takes over a session another process was
# serving: enough for the model to keep the thread, small enough to stay a fresh context.
HYDRATE_MESSAGES = 40

class ToolCall(BaseModel):
    name: str
    arguments: Dict[str, Any]

class ReActStep(BaseModel):
    thought: str
    action: Optional[ToolCall] = None
    observation: Optional[str] = None

class ReActResult(BaseModel):
    success: bool
    final_answer: str
    steps: List[ReActStep]
    error: Optional[str] = None

class ReActLoop:
    def __init__(
        self,
        tools: Any,
        llm: Any = None,
        max_turns: int = 6,
        task_type: str = "chat",
        smart_exit: bool = True,
        accountant: Any = None,
        session_id: str = "default",
        prompt_builder: Any = None,
        channel: str = "tui",
        state_db: Any = None,
        system_prompt: Optional[str] = None,
        token_budget: Optional[int] = None,
        parallel_tools: bool = True,
        on_token: Optional[Callable[[str], None]] = None,
        tool_selector: Any = None,
    ):
        if hasattr(tools, "get_tools"):
            self.tools = tools.get_tools()
            self.env = tools
        elif isinstance(tools, dict):
            self.tools = tools
            self.env = None
        else:
            self.tools = {}
            self.env = None

        self.tool_schemas: List[Dict[str, Any]] = (
            tools.get_tools_schema() if hasattr(tools, "get_tools_schema") else []
        )

        self.llm = llm
        self.max_turns = max_turns
        self.task_type = task_type
        if task_type == "coding":
            # Coding tasks need more room for multi-step edits/tests.
            self.max_turns = 10
        self.smart_exit = smart_exit
        # One controller per loop instance; reset per prompt in _run_live_turn.
        self.turn_controller = (
            TurnController(max_turns=self.max_turns) if smart_exit else None
        )
        # Token callback fired as streamed text arrives (None = no streaming UI).
        self.on_token = on_token
        # Optional ToolSelector to prune schemas before each LLM call.
        self.tool_selector = tool_selector
        self.accountant = accountant
        self.session_id = session_id
        self.prompt_builder = prompt_builder or (getattr(self.env, "prompt_builder", None) if self.env else None)
        self.channel = channel
        self.state_db = state_db or (getattr(self.env, "state_db", None) if self.env else None)
        self.compressor = getattr(self.env, "compressor", None) if self.env else None
        self._session_created = False
        # A fixed role prompt (a subagent's) replaces the shared one built from the env.
        self.system_prompt = system_prompt
        # Optional spend ceiling for one turn, in tokens in+out. None means unbounded.
        self.token_budget = token_budget
        self.budget_exceeded = False
        # When True, one assistant turn's tool calls run through asyncio.gather
        # instead of one at a time. Recording into trace/history/messages stays
        # strictly sequential in the original tool_calls order either way.
        self.parallel_tools = parallel_tools

        # Subagents run through this loop's provider, and inherit its environment, so
        # delegation only works once a loop exists to delegate from.
        delegation = getattr(self.env, "delegation", None) if self.env else None
        if delegation is not None:
            delegation.bind(self)

        # So the rewind tool can find this session's snapshots without the model knowing its id.
        self.checkpoints = getattr(self.env, "checkpoints", None) if self.env else None
        if self.checkpoints is not None:
            self.checkpoints.session_id = (session_id or "session").split("/")[0]

        self.history: List[Dict[str, Any]] = []
        self.messages: List[Dict[str, Any]] = []
        self._system_message: Optional[Dict[str, Any]] = None

    def reset(self):
        """Start a fresh conversation without rebuilding the environment."""
        self.history.clear()
        self.messages.clear()
        self._system_message = None
        self._session_created = False

    def load_session(self, session_id: str, channel: str = "",
                     limit: int = HYDRATE_MESSAGES) -> bool:
        """Point this loop at a session, rebuilding a context another process left behind.

        The live context lives in memory, so a loop that never served this session starts empty
        and rebuilds it from the transcript. Tool messages are skipped on purpose: a
        `role: tool` message has to follow the assistant call it answers, and `tool_call_id` is
        not persisted, so a paired-up guess would be rejected by the provider. Returns whether a
        context was rebuilt.
        """
        if session_id == self.session_id and self.messages:
            # Same session: keep the fuller in-memory context, but the platform tone is part of
            # the system prompt, so a different channel rebuilds that one message.
            if channel and channel != self.channel:
                self.channel = channel
                self._system_message = None
            return False

        self.reset()
        self.session_id = session_id
        if channel:
            self.channel = channel
        if self.checkpoints is not None:
            self.checkpoints.session_id = (session_id or "session").split("/")[0]
        if self.state_db is None:
            return False

        rows = self.state_db.get_session_messages(session_id, limit=limit, newest=True)
        for row in rows:
            role = row.get("role")
            content = row.get("content") or ""
            if role in ("user", "assistant") and content:
                self.messages.append({"role": role, "content": content})
        # The session row already exists (that is where these messages came from), so it must not
        # be created again as if this were a new conversation.
        self._session_created = bool(self.messages)
        return bool(self.messages)

    def _refusal(self, name: str, arguments: Dict[str, Any]) -> Optional[str]:
        """Guardrail and approval verdict for a call, or None when it may proceed.

        The guardrail screens only tools that reach the OS shell: file tools are scoped by
        whatever paths the user considers theirs. The approval gate covers everything
        outside its SAFE_TOOLS list.
        """
        if self.env is None:
            return None

        guard = getattr(self.env, "guardrail", None)
        if guard is not None:
            if name == "sandbox_exec":
                ok, reason = guard.command_allowed(arguments.get("cmd", ""), *(arguments.get("args") or []))
            elif name == "process_start":
                ok, reason = guard.command_allowed(*(arguments.get("cmd") or []))
            else:
                ok, reason = True, ""
            if not ok:
                return f"Error: {reason}"

        gate = getattr(self.env, "approval_gate", None)
        if gate is not None and not gate.check_approval(name, arguments):
            return f"Error: '{name}' was denied at the approval prompt."

        return None

    async def execute_step(self, tool_call: ToolCall) -> str:
        """Execute a tool and capture observation."""
        tool_meta = self.tools.get(tool_call.name)
        if not tool_meta:
            return f"Error: Tool '{tool_call.name}' not found"

        refusal = self._refusal(tool_call.name, tool_call.arguments or {})
        if refusal:
            return refusal

        # Snapshot before the write, so the session's file damage can be rewound. Keyed by the
        # root session: a subagent's edits must be undoable by the parent that delegated them.
        store = getattr(self.env, "checkpoints", None)
        if store is not None:
            try:
                store.snapshot_for(
                    (self.session_id or "session").split("/")[0],
                    tool_call.name,
                    tool_call.arguments or {},
                )
            except Exception as e:
                # A failed snapshot costs the ability to rewind, not the write itself.
                print(f"[checkpoint] could not snapshot: {e}")

        func = tool_meta["func"]
        try:
            if asyncio.iscoroutinefunction(func):
                res = await func(**tool_call.arguments)
            else:
                res = func(**tool_call.arguments)
            return str(res)
        except Exception as e:
            return f"Error executing '{tool_call.name}': {e}"

    async def _run_tool_calls_parallel(self, resp: Any, trace: List[ReActStep]) -> None:
        """Execute one assistant turn's tool calls concurrently, recording in order.

        `args_error` cases never reach a tool: their observations are built
        synchronously. All other calls run through `asyncio.gather` so one slow
        or failing tool cannot block the rest. `return_exceptions=True` keeps the
        gather alive if a tool somehow escapes `execute_step`'s own exception
        handling; such an escape becomes an error observation, not a broken
        turn. Recording into `trace`, `self.history` and `self.messages` stays
        strictly sequential, in the original `resp.tool_calls` order.
        """
        tool_calls = resp.tool_calls
        thought = resp.thought or ""
        observations: List[Optional[str]] = [None] * len(tool_calls)
        pending: List[int] = []
        for i, tc in enumerate(tool_calls):
            if tc.args_error:
                observations[i] = (
                    f"Error: tool '{tc.name}' was called with malformed JSON arguments "
                    f"({tc.args_error}). Retry with valid JSON."
                )
            else:
                pending.append(i)

        if pending:
            coros = (
                self.execute_step(
                    ToolCall(name=tool_calls[i].name, arguments=tool_calls[i].arguments)
                )
                for i in pending
            )
            results = await asyncio.gather(*coros, return_exceptions=True)
            for i, result in zip(pending, results):
                if isinstance(result, BaseException):
                    observations[i] = f"Error executing '{tool_calls[i].name}': {result}"
                else:
                    observations[i] = result

        for i, tc in enumerate(tool_calls):
            call = ToolCall(name=tc.name, arguments=tc.arguments)
            observation = observations[i]
            trace.append(ReActStep(thought=thought, action=call, observation=observation))
            self.history.append({
                "thought": thought,
                "action": tc.name,
                "observation": observation,
            })
            self._append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": observation,
            })

    def _build_system_message(self) -> Optional[Dict[str, Any]]:
        if self._system_message is not None:
            return self._system_message

        if self.system_prompt:
            text = self.system_prompt
        elif self.prompt_builder is not None:
            skills = self.env.skill_loader.list_skills() if self.env else []
            memory_notes = None
            if self.env:
                snapshot = self.env.curated_memory.get_snapshot()
                joined = "\n\n".join(
                    part for part in (snapshot.get("memory"), snapshot.get("user")) if part
                )
                memory_notes = joined or None
            text = self.prompt_builder.build_system_prompt(
                channel=self.channel,
                available_skills=skills,
                memory_notes=memory_notes,
            )
        else:
            text = ""

        self._system_message = {"role": "system", "content": text} if text else False
        return self._system_message

    def _ensure_session(self):
        """Register the session row so append_message can persist against it."""
        if self.state_db is None or self._session_created:
            return
        self.state_db.create_session(self.session_id, getattr(self.llm, "model", "default") or "default")
        self._session_created = True

    def _append(self, message: Dict[str, Any]):
        """Add a message to the live context and to the durable transcript."""
        self.messages.append(message)
        if self.state_db is not None:
            try:
                self.state_db.append_message(
                    self.session_id,
                    message.get("role", ""),
                    message.get("content") or "",
                    message.get("tool_calls"),
                )
            except Exception:
                # A transcript write must never break the turn it is recording.
                pass

    async def _run_live_turn(
        self, prompt: str, on_token: Optional[Callable[[str], None]] = None
    ) -> Dict[str, Any]:
        """Drive the model/tool loop until the model answers without requesting a tool."""
        if self.llm is None:
            raise RuntimeError(
                "ReActLoop has no LLM provider. Pass llm=<LLMProvider> to run a live turn."
            )

        system_message = self._build_system_message()
        self._ensure_session()
        self._append({"role": "user", "content": prompt})

        trace: List[ReActStep] = []
        final_text = ""
        error: Optional[str] = None
        stop_reason: str = "ok"
        turn_in = 0
        turn_out = 0
        if self.turn_controller is not None:
            self.turn_controller.reset()
            self.turn_controller.max_turns = self.max_turns

        for _ in range(self.max_turns):
            if self.token_budget is not None and (turn_in + turn_out) >= self.token_budget:
                # Stop spending, but keep whatever the last tool call observed.
                error = (
                    f"Token budget of {self.token_budget} exhausted after "
                    f"{turn_in + turn_out} tokens."
                )
                self.budget_exceeded = True
                final_text = trace[-1].observation if trace else ""
                break
            if self.compressor is not None:
                # The live context is trimmed; the durable transcript in StateDB keeps every
                # message, so session_search can still reach what compaction dropped here.
                self.messages = self.compressor.compact_messages(self.messages)
            outgoing = ([system_message] if system_message else []) + self.messages
            resp = await self._complete(outgoing, on_token)

            if resp.usage is not None:
                if self.accountant is not None:
                    self.accountant.record_response_usage(self.session_id, resp.usage)
                turn_in += resp.usage.input_tokens or 0
                turn_out += resp.usage.output_tokens or 0

            if not resp.tool_calls:
                final_text = resp.content or ""
                self._append({"role": "assistant", "content": final_text})
                break

            self._append({
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in resp.tool_calls
                ],
            })

            if self.parallel_tools:
                await self._run_tool_calls_parallel(resp, trace)
            else:
                for tc in resp.tool_calls:
                    call = ToolCall(name=tc.name, arguments=tc.arguments)
                    if tc.args_error:
                        observation = (
                            f"Error: tool '{tc.name}' was called with malformed JSON arguments "
                            f"({tc.args_error}). Retry with valid JSON."
                        )
                    else:
                        observation = await self.execute_step(call)

                    trace.append(ReActStep(thought=resp.thought or "", action=call, observation=observation))
                    self.history.append({
                        "thought": resp.thought or "",
                        "action": tc.name,
                        "observation": observation,
                    })
                    self._append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": observation,
                    })
            # --- smart early exit ------------------------------------
            if self.turn_controller is not None:
                turn_calls = [
                    {"name": tc.name, "arguments": tc.arguments}
                    for tc in resp.tool_calls
                ]
                turn_obs = [s.observation or "" for s in trace[-len(resp.tool_calls):]]
                self.turn_controller.record_turn(turn_calls, turn_obs)
                keep_going, stop_reason = self.turn_controller.should_continue(
                    turn_calls, turn_obs
                )
                if not keep_going:
                    # Deliberate early stop, not a failure: keep last
                    # observation as the answer without flagging an error.
                    final_text = trace[-1].observation if trace else ""
                    break
            # ----------------------------------------------------------
        else:
            error = f"Reached max_turns={self.max_turns} without a final answer."
            final_text = trace[-1].observation if trace else ""

        if self.state_db is not None:
            try:
                self.state_db.record_session_usage(self.session_id, turn_in, turn_out)
                self.state_db.record_timeline_event(
                    self.session_id, "turn", {"steps": len(trace), "error": error}
                )
            except Exception:
                pass  # bookkeeping must not break the turn it describes

        result: Dict[str, Any] = {
            "action": final_text or "No response.",
            "success": error is None,
            "steps": len(trace),
            "trace": [s.model_dump() for s in trace],
            "error": error,
        }
        result["stop_reason"] = stop_reason
        if trace:
            result["observation"] = "\n".join(s.observation or "" for s in trace)

        self.history.append({"prompt": prompt, "result": {"success": result["success"], "steps": len(trace)}})
        return result

    async def _complete(
        self, outgoing: List[Dict[str, Any]], on_token: Optional[Callable[[str], None]]
    ) -> Any:
        """One model call: streamed when the provider supports it.

        Returns an object shaped like ``LLMResponse`` (``content``,
        ``thought``, ``tool_calls``, ``usage``, ``finish_reason``), so the
        rest of the loop is untouched. Tool schemas are pruned via
        ``self.tool_selector`` when one is configured.
        """
        if self.tool_selector is not None:
            try:
                from .tool_selector import select_or_all
                prompt_text = outgoing[-1].get("content", "") if outgoing else ""
                tools_schema = select_or_all(
                    self.tool_selector, prompt_text, self.tool_schemas or []
                ) or None
            except Exception:
                tools_schema = self.tool_schemas or None
        else:
            tools_schema = self.tool_schemas or None
        if hasattr(self.llm, "chat_complete_stream"):
            handler = StreamHandler(on_token=on_token)
            stream = self.llm.chat_complete_stream(outgoing, tools_schema=tools_schema)
            full_text, tool_calls = await handler.consume(stream)
            thought, content = self._scrub_streamed_text(full_text)
            return SimpleNamespace(
                content=content,
                thought=thought,
                tool_calls=[
                    SimpleNamespace(
                        id=tc.get("id") or f"call_{i}",
                        name=tc.get("name") or "",
                        arguments=tc.get("arguments") or {},
                        args_error=tc.get("args_error"),
                    )
                    for i, tc in enumerate(tool_calls)
                ],
                usage=SimpleNamespace(
                    input_tokens=(handler.last_usage or {}).get("input_tokens", 0),
                    output_tokens=(handler.last_usage or {}).get("output_tokens", 0),
                ),
                finish_reason="stop" if not tool_calls else "tool_calls",
            )
        return await self.llm.chat_complete(outgoing, tools_schema=tools_schema)

    @staticmethod
    def _scrub_streamed_text(text: str):
        """Strip <thought>...</thought> blocks from streamed text (same rule as
        LLMProvider.scrub_thought, without importing the provider module)."""
        thought_match = re.search(r"<thought>(.*?)</thought>", text, re.DOTALL)
        if thought_match:
            thought = thought_match.group(1).strip()
            clean = re.sub(r"<thought>.*?</thought>", "", text, flags=re.DOTALL).strip()
            return thought, clean
        return None, text

    async def step(
        self,
        prompt: Optional[str] = None,
        thought: str = "",
        action: Optional[ToolCall] = None,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> Any:
        """Run one turn.

        With `action`, executes that single tool and returns a ReActStep (programmatic callers).
        Otherwise runs the model/tool loop and returns a result dict.
        """
        if action is not None:
            observation = await self.execute_step(action)
            step_obj = ReActStep(thought=thought or "", action=action, observation=observation)
            self.history.append({"thought": thought, "action": str(action), "observation": observation})
            return step_obj

        return await self._run_live_turn(
            prompt or thought or "",
            on_token=on_token if on_token is not None else self.on_token,
        )
