"""Brain — one execution path combining ReAct + DAG + MASS.

Before this module, Boom had three disconnected pieces: ReActLoop (the turn loop),
DAGScheduler (a real async DAG executor with cycle validation) and MASSSwarm (a
registry of specialist roles with *no* parallel engine). Each was reachable only
through its own call site, so a prompt could not flow through them as one brain.

Brain is that single path. It owns no provider and makes no network calls itself:
every model call goes through ``make_loop``, a factory the parent (``cli.py``)
closes over the real env + LLM. In tests the factory returns fakes.

Strategies
----------
- ``react`` — one ReActLoop turn. The default path; behaviour is identical to
  calling the loop directly.
- ``dag`` — a planner turn decomposes the goal into subtasks, the plan is parsed
  into TaskNodes, DAGScheduler runs each node as its own ReActLoop with its
  dependencies' outputs in context. Unparseable/invalid plans fall back to
  ``react`` (recorded in ``errors``, never raised).
- ``mass`` — every chosen specialist (PLANNER/CODER/REVIEWER/CRITIC/LEARNER)
  runs the same prompt in parallel, each with its role system prompt + contract
  and only its allowed tools. v1 synthesis is an honest labelled join, not a
  second LLM call.

Routing (``_choose_strategy``) is a rule-based heuristic v1 — it is *not* an AI
classifier and the docstring says so: an explicit strategy always wins; in
``auto`` mode dependency keywords pick DAG, an explicit multi-item list picks
MASS, everything else is REACT.
"""

import asyncio
import json
import re
import uuid
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from agent.src.loop.react import ReActLoop
from agent.src.orchestration.dag import DAGScheduler, TaskNode
from agent.src.orchestration.mass import SPECIALISTS, AgentRole, SpecialistAgent, MASSSwarm


class BrainStrategy(str, Enum):
    REACT = "react"
    DAG = "dag"
    MASS = "mass"


class BrainResult(BaseModel):
    strategy: str
    text: str = ""
    node_outputs: Dict[str, Any] = Field(default_factory=dict)   # DAG: output per node
    agent_outputs: Dict[str, Any] = Field(default_factory=dict)  # MASS: output per role
    errors: Dict[str, str] = Field(default_factory=dict)


# Dependency keywords for the auto router. Heuristic v1: cheap, transparent,
# wrong sometimes — an explicit strategy always overrides it.
_DEP_WORDS = re.compile(
    r"\b(rồi|sau đó|trước tiên|bước\s*\d+|step\s*\d+|\bthen\b|after that)\b",
    re.IGNORECASE,
)
_LIST_ITEM = re.compile(r"^\s*(?:\d+[\.\)\]]|[-*•])\s+\S", re.MULTILINE)
_NUMBERED_LINE = re.compile(r"^\s*(\d+)[\.\)\]]\s*(.+?)\s*$")
# "... (depends on 1, 2)" / "... (sau bước 1)" inside a planner line.
_DEP_PHRASE = re.compile(
    r"(?:depends?\s+on|waits?\s+for|after|phụ\s*thuộc|sau(?:\s+bước)?)\s*([\d][\d,\s]*)",
    re.IGNORECASE,
)
# Trailing "(...depends on 1...)" stripped from the display name.
_DEP_PAREN = re.compile(
    r"\s*[\(\[][^)\]]*(?:depends?\s+on|waits?\s+for|after|phụ\s*thuộc|sau)[^)\]]*[\)\]]\s*$",
    re.IGNORECASE,
)

_MASS_CONCURRENCY = 5


class Brain:
    """Single execution path over ReActLoop, DAGScheduler and MASS specialists.

    ``make_loop`` is a factory ``(system_prompt=None, tools=None, session_id=None)
    -> ReActLoop``. Convention: ``tools=None`` means "the full toolset" — Brain
    probes it once to filter per-role tool dicts. Brain never imports or builds a
    provider; ``llm`` is kept only for introspection.
    """

    def __init__(self, make_loop: Callable[..., ReActLoop], llm: Any = None):
        self.make_loop = make_loop
        self.llm = llm  # introspection only; all model calls go through make_loop
        self.swarm = MASSSwarm()
        self._tools_cache: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ router

    def _choose_strategy(self, prompt: str, strategy: str) -> Tuple[BrainStrategy, str]:
        """Pick a strategy. Returns (strategy, note); note is "" when clean.

        Heuristic v1, rule-based — not an AI classifier. Explicit strategy always
        wins; in auto mode dependency keywords -> DAG, an explicit multi-item
        list -> MASS, otherwise REACT.
        """
        s = (strategy or "auto").strip().lower()
        if s != "auto":
            try:
                return BrainStrategy(s), ""
            except ValueError:
                return BrainStrategy.REACT, f"unknown strategy '{strategy}'; using 'react'"
        text = prompt or ""
        if _DEP_WORDS.search(text):
            return BrainStrategy.DAG, ""
        if len(_LIST_ITEM.findall(text)) >= 2:
            return BrainStrategy.MASS, ""
        return BrainStrategy.REACT, ""

    async def run(
        self,
        prompt: str,
        strategy: str = "auto",
        session_id: Optional[str] = None,
        roles: Optional[List[Any]] = None,
        **kw: Any,
    ) -> BrainResult:
        """Run one prompt through the chosen strategy. Never raises: every
        failure is returned as BrainResult(errors=...). ``kw`` is accepted for
        forward compatibility and currently unused."""
        chosen, note = self._choose_strategy(prompt, strategy)
        errors: Dict[str, str] = {}
        if note:
            errors["strategy"] = note
        try:
            if chosen is BrainStrategy.DAG:
                result = await self.run_dag(prompt, session_id=session_id)
            elif chosen is BrainStrategy.MASS:
                result = await self.run_mass(prompt, session_id=session_id, roles=roles)
            else:
                result = await self.run_react(prompt, session_id=session_id)
        except Exception as e:  # last-resort net: sub-runners already guard internally
            return BrainResult(
                strategy=chosen.value, text="",
                errors={**errors, "brain": f"{type(e).__name__}: {e}"},
            )
        result.errors = {**errors, **result.errors}
        return result

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _extract_text(res: Any) -> str:
        if isinstance(res, dict):
            return str(res.get("action") or res.get("observation") or "")
        return "" if res is None else str(res)

    def _all_tools(self) -> Dict[str, Any]:
        """Full tool dict, via a probe loop. Cached; the factory convention is
        that make_loop(tools=None) returns a loop carrying the whole toolset."""
        if self._tools_cache is None:
            probe = self.make_loop()
            self._tools_cache = dict(getattr(probe, "tools", None) or {})
        return self._tools_cache

    def _filter_tools(self, allowed_tools: List[str]) -> Dict[str, Any]:
        """Keep only tools that exist. A role naming a nonexistent tool silently
        loses that tool (mass.py's documented caveat) — never a crash."""
        all_tools = self._all_tools()
        return {name: all_tools[name] for name in allowed_tools if name in all_tools}

    # ------------------------------------------------------------------ REACT

    async def run_react(
        self, prompt: str, session_id: Optional[str] = None
    ) -> BrainResult:
        """One ReActLoop turn. The default path — identical behaviour to calling
        the loop directly, because that is literally what it does."""
        try:
            loop = self.make_loop(session_id=session_id)
            res = await loop.step(prompt)
        except Exception as e:
            return BrainResult(
                strategy=BrainStrategy.REACT.value, text="",
                errors={"react": f"{type(e).__name__}: {e}"},
            )
        errors: Dict[str, str] = {}
        if isinstance(res, dict) and res.get("error"):
            errors["react"] = str(res["error"])
        return BrainResult(
            strategy=BrainStrategy.REACT.value,
            text=self._extract_text(res),
            errors=errors,
        )

    # ------------------------------------------------------------------ DAG

    def _parse_plan(self, text: str) -> List[TaskNode]:
        """Parse the planner's numbered list into TaskNodes.

        Expected per PLANNER.contract: "1. <what it produces> (waits for 2)".
        Raises ValueError when the plan is unusable — the caller falls back to
        REACT instead of executing a hallucinated graph.
        """
        nodes: List[TaskNode] = []
        for line in (text or "").splitlines():
            m = _NUMBERED_LINE.match(line)
            if not m:
                continue
            idx, rest = m.group(1), m.group(2)
            deps: List[str] = []
            for dm in _DEP_PHRASE.finditer(rest):
                for n in re.findall(r"\d+", dm.group(1)):
                    if n != idx and n not in deps:
                        deps.append(n)
            name = _DEP_PAREN.sub("", rest).strip() or rest
            nodes.append(TaskNode(id=idx, name=name, dependencies=deps))
        if not nodes:
            raise ValueError("planner returned no numbered subtasks")
        ids = [n.id for n in nodes]
        if len(set(ids)) != len(ids):
            raise ValueError("planner reused a subtask number")
        for n in nodes:
            unknown = [d for d in n.dependencies if d not in ids]
            if unknown:
                raise ValueError(
                    f"subtask {n.id} depends on unknown subtask(s): {', '.join(unknown)}"
                )
        return nodes

    def _node_handler(self, node: TaskNode, session_id: str) -> Callable:
        """A DAGScheduler handler: one ReActLoop per node, deps' outputs in context."""

        async def handler(dep_outputs: Dict[str, Any]) -> str:
            loop = self.make_loop(session_id=f"{session_id}/dag/{node.id}")
            ctx = f"Subtask [{node.id}]: {node.name}"
            if dep_outputs:
                ctx += (
                    "\n\nOutputs from prerequisite subtasks:\n"
                    + json.dumps(dep_outputs, ensure_ascii=False, default=str)
                )
            res = await loop.step(ctx)
            if isinstance(res, dict) and res.get("error"):
                # A failed node raises so the scheduler marks dependents blocked
                # instead of running them on garbage.
                raise RuntimeError(str(res["error"]))
            return self._extract_text(res)

        return handler

    async def _dag_fallback(
        self, prompt: str, session_id: Optional[str], reason: str,
        extra: Optional[Dict[str, str]] = None,
    ) -> BrainResult:
        fb = await self.run_react(prompt, session_id=session_id)
        fb.errors = {**(extra or {}), **fb.errors, "fallback": reason}
        return fb

    async def run_dag(
        self, prompt: str, session_id: Optional[str] = None
    ) -> BrainResult:
        """Planner -> parse -> DAGScheduler. Any planning failure falls back to
        REACT and says so in errors."""
        sid = session_id or f"brain_{uuid.uuid4().hex[:8]}"
        planner = SPECIALISTS[AgentRole.PLANNER]
        try:
            ploop = self.make_loop(
                system_prompt=(
                    planner.system_instruction + "\n\nContract:\n" + planner.contract
                ),
                tools=self._filter_tools(planner.allowed_tools),
                session_id=f"{sid}/planner",
            )
            pres = await ploop.step(
                prompt
                + "\n\nBreak the goal above into subtasks with dependencies. "
                "Follow your contract exactly: a numbered list; for each subtask "
                "say what it produces and which other subtask number (if any) "
                "it must wait for, e.g. '2. Analyse (waits for 1)'."
            )
        except Exception as e:
            return await self._dag_fallback(
                prompt, session_id, f"dag->react: planner turn failed: {type(e).__name__}: {e}"
            )
        if isinstance(pres, dict) and pres.get("error"):
            return await self._dag_fallback(
                prompt, session_id,
                f"dag->react: planner turn failed: {pres['error']}",
            )
        try:
            nodes = self._parse_plan(self._extract_text(pres))
        except ValueError as e:
            return await self._dag_fallback(
                prompt, session_id, f"dag->react: planner output unparseable: {e}"
            )

        sched = DAGScheduler()
        for node in nodes:
            sched.add_task(node, self._node_handler(node, sid))
        try:
            result = await sched.execute()
        except Exception as e:  # validate() rejects cycles / missing deps
            return await self._dag_fallback(
                prompt, session_id,
                f"dag->react: invalid plan: {type(e).__name__}: {e}",
            )

        errors: Dict[str, str] = dict(result.errors)
        for b in result.blocked:
            errors.setdefault(f"blocked:{b}", "never ran: a dependency failed or was blocked")

        # Synthesize from terminal nodes (nothing depends on them); if none
        # produced output, fall back to whatever outputs exist.
        depended = set()
        for n in nodes:
            depended.update(n.dependencies)
        terminals = [n.id for n in nodes if n.id not in depended]
        parts = [f"[{nid}] {result.outputs[nid]}" for nid in terminals if nid in result.outputs]
        if not parts:
            parts = [f"[{nid}] {out}" for nid, out in result.outputs.items()]

        return BrainResult(
            strategy=BrainStrategy.DAG.value,
            text="\n\n".join(parts),
            node_outputs=dict(result.outputs),
            errors=errors,
        )

    # ------------------------------------------------------------------ MASS

    async def _run_specialist(
        self,
        spec: SpecialistAgent,
        prompt: str,
        session_id: str,
        sem: asyncio.Semaphore,
    ) -> Tuple[str, str, str]:
        """Run one specialist. Returns (role_name, text, error); error "" on success."""
        async with sem:
            try:
                sys_prompt = spec.system_instruction
                if spec.contract:
                    sys_prompt += "\n\nContract:\n" + spec.contract
                loop = self.make_loop(
                    system_prompt=sys_prompt,
                    tools=self._filter_tools(spec.allowed_tools),
                    session_id=f"{session_id}/mass/{spec.role.value}",
                )
                res = await loop.step(prompt)
            except Exception as e:
                return spec.role.value, "", f"{type(e).__name__}: {e}"
            if isinstance(res, dict) and res.get("error"):
                return spec.role.value, "", str(res["error"])
            return spec.role.value, self._extract_text(res), ""

    async def run_mass(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        roles: Optional[List[Any]] = None,
    ) -> BrainResult:
        """Fan-out: every chosen specialist runs the prompt in parallel, each
        with its role prompt + contract and only its allowed tools. One role
        failing never stops the others. v1 synthesis is a labelled join —
        honest, no extra LLM call."""
        sid = session_id or f"brain_{uuid.uuid4().hex[:8]}"
        errors: Dict[str, str] = {}
        specs: List[SpecialistAgent] = []
        if roles is None:
            specs = list(SPECIALISTS.values())
        else:
            for r in roles:
                spec = self.swarm.resolve(r)
                if spec is None:
                    errors[f"role:{r}"] = f"unknown role '{r}'"
                elif spec not in specs:
                    specs.append(spec)
        if not specs:
            errors.setdefault("mass", "no valid roles to run")
            return BrainResult(strategy=BrainStrategy.MASS.value, text="", errors=errors)

        sem = asyncio.Semaphore(_MASS_CONCURRENCY)
        results = await asyncio.gather(
            *(self._run_specialist(spec, prompt, sid, sem) for spec in specs),
            return_exceptions=True,
        )
        agent_outputs: Dict[str, Any] = {}
        parts: List[str] = []
        for spec, res in zip(specs, results):
            name = spec.role.value
            if isinstance(res, Exception):
                errors[name] = f"{type(res).__name__}: {res}"
                agent_outputs[name] = {"error": errors[name]}
                continue
            role_name, text, err = res
            if err:
                errors[role_name] = err
                agent_outputs[role_name] = {"error": err}
            else:
                agent_outputs[role_name] = {"text": text}
                parts.append(f"[{role_name}]\n{text}")
        return BrainResult(
            strategy=BrainStrategy.MASS.value,
            text="\n\n".join(parts),
            agent_outputs=agent_outputs,
            errors=errors,
        )
