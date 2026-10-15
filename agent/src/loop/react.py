import asyncio
import json
from typing import Any, Callable, Dict, List, Optional
from pydantic import BaseModel

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
        max_turns: int = 10,
        accountant: Any = None,
        session_id: str = "default",
        prompt_builder: Any = None,
        channel: str = "tui",
        state_db: Any = None,
        system_prompt: Optional[str] = None,
        token_budget: Optional[int] = None,
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

    async def _run_live_turn(self, prompt: str) -> Dict[str, Any]:
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
        turn_in = 0
        turn_out = 0

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
            resp = await self.llm.chat_complete(outgoing, tools_schema=self.tool_schemas or None)

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
        if trace:
            result["observation"] = "\n".join(s.observation or "" for s in trace)

        self.history.append({"prompt": prompt, "result": {"success": result["success"], "steps": len(trace)}})
        return result

    async def step(
        self,
        prompt: Optional[str] = None,
        thought: str = "",
        action: Optional[ToolCall] = None
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

        return await self._run_live_turn(prompt or thought or "")
