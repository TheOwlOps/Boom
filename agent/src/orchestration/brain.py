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

Verification (``verify=True``)
------------------------------
Passing ``verify=True`` to ``run()`` does not change execution at all: the same
``BrainResult`` is produced first, then the optional ``verify.verifier`` module
— built by another coordinator, *not* part of this file — is imported lazily
and its ``verify(text, kind="text", llm=..., search_fn=...)`` called on
``result.text``. When the module is missing or verification crashes, ``run()``
degrades honestly with ``verification={"status": "skipped", "reason": ...}`` —
never raised, never a faked verification.
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


_PLAN_SYSTEM = (
    "You are a planner for an autonomous coding agent. "
    "You output only JSON."
)

# file paths like agent/src/foo.py, ./x.js, /abs/y.ts
_FILE_RE = re.compile(
    r"(?:^|[\s\"\'`(\[])([\w\-.\\/]+?\."
    r"(?:py|js|ts|tsx|jsx|md|json|yaml|yml|txt|sh|rs|go|java|c|cpp|"
    r"toml|cfg|ini))\b"
)

# intent classification for the heuristic planner (order matters).
_INTENT_PATTERNS = [
    ("test", re.compile(
        r"pytest|unittest|\btest\b|ki\u1ec3m tra|ch\u1ea1y test|"
        r"ki\u1ec3m th\u1eed", re.IGNORECASE)),
    ("fix", re.compile(
        r"\bfix\b|\bbug\b|\bdebug\b|l\u1ed7i|s\u1eeda l\u1ed7i|"
        r"kh\u1eafc ph\u1ee5c|g\u1eb7p l\u1ed7i|b\u1ecb l\u1ed7i",
        re.IGNORECASE)),
    ("run", re.compile(
        r"\b(ch\u1ea1y|run|th\u1ef1c thi|execute)\b", re.IGNORECASE)),
    ("read", re.compile(
        r"\b(\u0111\u1ecdc|read|xem|m\u1edf)\b",
        re.IGNORECASE)),
    ("search", re.compile(
        r"\b(t\xecm|search|t\xecm ki\u1ebfm|grep)\b",
        re.IGNORECASE)),
    ("edit", re.compile(
        r"\b(s\u1eeda|edit|patch|thay \u0111\u1ed5i|c\u1eadp nh\u1eadt|"
        r"update)\b", re.IGNORECASE)),
    ("write", re.compile(
        r"\b(t\u1ea1o|create|vi\u1ebft|write)\b", re.IGNORECASE)),
]

_RUN_CANDIDATES = ["run_command", "shell", "execute", "exec", "bash"]
_READ_CANDIDATES = ["read_file", "file_read", "read", "cat"]
_TEST_CANDIDATES = ["pytest", "run_tests", "run_pytest", "npm_test", "test"]
_SEARCH_CANDIDATES = ["grep", "search", "search_files", "find", "rg"]


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
        verify: bool = False,
        **kw: Any,
    ) -> Any:
        """Run one prompt through the chosen strategy. Never raises: every
        failure is returned as BrainResult(errors=...). ``kw`` is accepted for
        forward compatibility and currently unused.

        ``verify=False`` (default) returns the ``BrainResult`` exactly as
        before — execution is untouched, byte-for-byte the same behaviour.
        ``verify=True`` returns a dict ``{"result": <BrainResult>,
        "verification": <dict>}`` where ``verification`` comes from the
        optional ``verify.verifier`` module (lazy import; a missing module or
        any error degrades to ``{"status": "skipped", "reason": ...}``).
        """
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
            result = BrainResult(
                strategy=chosen.value, text="",
                errors={**errors, "brain": f"{type(e).__name__}: {e}"},
            )
        else:
            result.errors = {**errors, **result.errors}
        if not verify:
            return result
        return {"result": result, "verification": self._verify_result(result)}

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

    def _verify_result(self, result: BrainResult) -> Dict[str, Any]:
        """Run optional post-hoc verification over ``result.text``.

        The ``verify.verifier`` module is built by another coordinator and may
        not exist on this machine, so it is imported lazily *inside* this call —
        a top-level import would make ``brain.py`` unimportable until the
        verifier lands. Any ImportError or runtime error degrades honestly to
        ``{"status": "skipped", "reason": ...}``; never raised, never faked.
        """
        try:
            from importlib import import_module
            mod = import_module("verify.verifier")
            t = self._all_tools().get("web_search")
            search_fn = (
                t["func"]
                if isinstance(t, dict) and callable(t.get("func"))
                else None
            )
            return mod.verify(
                result.text, kind="text", llm=self.llm, search_fn=search_fn
            )
        except Exception as e:
            return {"status": "skipped", "reason": f"{type(e).__name__}: {e}"}

    # ------------------------------------------------------------------ PLAN
    # Autonomous-controller support: turn a goal into tool-call steps.
    # The autonomous controller calls ``brain.plan(goal, history)`` via
    # duck-typing and falls back to a 1-step default when the method is
    # missing, raises, or returns nothing usable. This implementation never
    # raises: LLM planning failure degrades to a heuristic intent-based plan,
    # and that degrades to [] (the controller's own default then applies).

    def plan(
        self,
        goal: str = "",
        history: Optional[List[Dict[str, Any]]] = None,
        available_tools: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Turn ``goal`` into a list of ``{"tool", "args", "desc"}`` steps.

        Sync by design (the autonomous controller calls it without await).
        Never raises — worst case returns []. Every returned step names a
        tool from ``available_tools`` (or the probed full toolset), so the
        controller's ``tname not in tools`` guard never trips on our output.

        Strategy: try the LLM planner first (via a throwaway ReActLoop, run
        to completion in a fresh event loop); on any failure — no loop
        running-allowed, bad JSON, unknown tools — fall back to the
        heuristic intent classifier below.
        """
        try:
            return self._plan_inner(goal, history, available_tools)
        except Exception:
            return []

    def _plan_inner(
        self,
        goal: Any,
        history: Optional[List[Dict[str, Any]]],
        available_tools: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        hist = history or []
        if available_tools is not None:
            tool_names = [t for t in available_tools
                          if isinstance(t, str) and t.strip()]
        else:
            try:
                tool_names = list(self._all_tools().keys())
            except Exception:
                tool_names = []
        if not tool_names:
            return []
        steps = self._llm_plan(goal, hist, tool_names)
        if steps:
            return steps
        return self._heuristic_plan(goal, hist, tool_names)

    # ---------------------------------------------------------- LLM planner

    async def _plan_turn(
        self, system_prompt: str, prompt: str,
        tool_dict: Dict[str, Any], session_id: str,
    ) -> str:
        """One planner turn. Tolerates factories whose step() is sync."""
        ploop = self.make_loop(
            system_prompt=system_prompt, tools=tool_dict, session_id=session_id
        )
        step = getattr(ploop, "step", None)
        if not callable(step):
            return ""
        res = step(prompt)
        if asyncio.iscoroutine(res):
            res = await res
        return self._extract_text(res)

    def _plan_prompt(
        self, goal: Any, history: List[Dict[str, Any]], tool_names: List[str]
    ) -> str:
        lines = [
            "Break the GOAL below into a short ordered list of concrete tool calls.",
            "",
            "GOAL: " + str(goal or ""),
            "",
            "AVAILABLE TOOLS: " + (
                ", ".join(tool_names) if tool_names else "(none)"),
        ]
        if history:
            lines.append("")
            lines.append(
                "HISTORY (previous iterations; do not repeat completed work):")
            for h in history[-10:]:
                if isinstance(h, dict):
                    lines.append("- " + json.dumps(
                        h, ensure_ascii=False, default=str)[:200])
                else:
                    lines.append("- " + str(h)[:200])
        lines += [
            "",
            "RULES:",
            "- Reply with ONLY a JSON array, no prose, no markdown fences.",
            '- Each element: {"tool": "<exact tool name>", "args": {...}, '
            '"desc": "<short description>"}.',
            "- Use only tools from AVAILABLE TOOLS.",
            "- Keep it short (1-6 steps), ordered by dependency.",
            "- If the GOAL looks already done per HISTORY, return [].",
        ]
        return "\n".join(lines)

    def _parse_plan_json(
        self, text: Any, tool_names: List[str]
    ) -> List[Dict[str, Any]]:
        """Parse the planner's JSON into canonical steps. Unknown tools are
        dropped; anything unparseable yields []."""
        known = set(tool_names or [])
        try:
            t = str(text or "").strip()
            if t.startswith("```"):
                t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
                t = re.sub(r"\s*```\s*$", "", t)
            s, e = t.find("["), t.rfind("]")
            if s < 0 or e <= s:
                return []
            raw = json.loads(t[s:e + 1])
        except Exception:
            return []
        if isinstance(raw, dict) and isinstance(raw.get("steps"), list):
            raw = raw["steps"]
        if not isinstance(raw, list):
            return []
        steps: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            tool = (item.get("tool") or item.get("action")
                    or item.get("name"))
            args = (item.get("args") or item.get("arguments")
                    or item.get("params") or {})
            desc = item.get("desc") or item.get("description") or ""
            if isinstance(tool, str) and tool in known \
                    and isinstance(args, dict):
                steps.append({"tool": tool, "args": args,
                              "desc": str(desc)})
        return steps

    def _llm_plan(
        self, goal: Any, history: List[Dict[str, Any]], tool_names: List[str]
    ) -> List[Dict[str, Any]]:
        """Ask the model for a plan. Returns [] on any failure — including
        being called from inside a running event loop, where blocking on
        asyncio.run() would deadlock."""
        try:
            asyncio.get_running_loop()
            return []  # async context: cannot block; heuristic takes over
        except RuntimeError:
            pass  # no running loop: safe to drive one ourselves
        try:
            tool_dict = self._filter_tools(list(tool_names))
            prompt = self._plan_prompt(goal, history, tool_names)
            text = asyncio.run(self._plan_turn(
                _PLAN_SYSTEM, prompt, tool_dict,
                f"brain_{uuid.uuid4().hex[:8]}/plan",
            ))
            return self._parse_plan_json(text, tool_names)
        except Exception:
            return []

    # ------------------------------------------------------ heuristic fallback

    @staticmethod
    def _classify_intent(goal: str) -> str:
        g = (goal or "").lower()
        for intent, rx in _INTENT_PATTERNS:
            if rx.search(g):
                return intent
        return "generic"

    @staticmethod
    def _extract_files(text: str) -> List[str]:
        found: List[str] = []
        for m in _FILE_RE.finditer(text or ""):
            p = m.group(1)
            if p not in found:
                found.append(p)
        return found

    @staticmethod
    def _pick_tool(
        candidates: List[str], tool_names: List[str]
    ) -> Optional[str]:
        names = list(tool_names or [])
        for c in candidates:
            if c in names:
                return c
        lower = {t.lower(): t for t in names}
        for c in candidates:
            cl = c.lower()
            for tl, orig in lower.items():
                if cl in tl or tl in cl:
                    return orig
        return None

    @staticmethod
    def _strip_verb(text: str, verbs: List[str]) -> str:
        t = (text or "").strip()
        for v in verbs:
            if t.lower().startswith(v.lower()):
                t = t[len(v):].lstrip(" :—-").strip()
                break
        return t or (text or "").strip()

    @staticmethod
    def _is_explicit_test_cmd(text: str) -> bool:
        """True when ``text`` is a real test invocation, not prose.

        "pytest -x" / "pytest tests/a.py" -> True (run verbatim, keep flags).
        "pytest cho module x" -> False ("cho module x" is Vietnamese prose;
        running it verbatim would fail, so fall back to the test tool).
        """
        m = re.match(
            r"(python3?\s+-m\s+pytest|pytest|npm\s+(?:test|run))\s*(.*)$",
            (text or "").strip(), re.IGNORECASE)
        if not m:
            return False
        rest = m.group(2).strip()
        if not rest:
            return True
        for tok in rest.split():
            if tok.startswith("-"):      # flag
                continue
            if "/" in tok or "\\" in tok:  # path
                continue
            if re.match(r"^[\w\-]+\.[\w]+$", tok):      # file.ext
                continue
            if re.match(r"^[\w]+(\.[\w]+)+$", tok):      # dotted.module
                continue
            return False
        return True

    @staticmethod
    def _test_command(goal: str, files: List[str]) -> str:
        gl = (goal or "").lower()
        if files and files[0].endswith(".py"):
            return "python3 -m pytest " + files[0]
        if "pytest" in gl:
            return "python3 -m pytest"
        if files and files[0].endswith((".js", ".ts", ".tsx", ".jsx")):
            return "npm test"
        if "npm" in gl:
            return "npm test"
        return "python3 -m pytest"

    def _heuristic_plan(
        self, goal: Any, history: List[Dict[str, Any]], tool_names: List[str]
    ) -> List[Dict[str, Any]]:
        """Intent-based plan without a model. Every step uses a real tool
        from ``tool_names``; returns [] only when no usable tool exists."""
        tools = list(tool_names or [])
        g = str(goal or "").strip()
        if not g or not tools:
            return []
        intent = self._classify_intent(g)
        files = self._extract_files(g)
        run = self._pick_tool(_RUN_CANDIDATES, tools)
        read = self._pick_tool(_READ_CANDIDATES, tools)

        if intent == "test":
            # Goal already spells out the test command (with flags) -> run
            # it verbatim so flags/paths are preserved.
            cmd_candidate = self._strip_verb(
                g, ["chạy", "run", "thực thi", "execute"])
            if run and self._is_explicit_test_cmd(cmd_candidate):
                return [{"tool": run, "args": {"command": cmd_candidate},
                         "desc": "chạy test: %s" % cmd_candidate}]
            t = self._pick_tool(_TEST_CANDIDATES, tools)
            if t:
                args = {"path": files[0]} if files else {}
                return [{"tool": t, "args": args,
                         "desc": "chạy test: %s" % g}]
            if run:
                cmd = self._test_command(g, files)
                return [{"tool": run, "args": {"command": cmd},
                         "desc": "chạy test: %s" % cmd}]

        elif intent == "fix":
            steps: List[Dict[str, Any]] = []
            if read and files:
                steps.append({"tool": read, "args": {"path": files[0]},
                              "desc": "đọc file lỗi: %s" % files[0]})
            t = self._pick_tool(_TEST_CANDIDATES, tools)
            if t:
                steps.append({"tool": t, "args": {},
                              "desc": "chạy lại test để tái hiện lỗi"})
            elif run:
                steps.append({"tool": run,
                              "args": {"command": "python3 -m pytest"},
                              "desc": "chạy lại để tái hiện lỗi"})
            if steps:
                return steps

        elif intent == "run":
            if run:
                cmd = self._strip_verb(
                    g, ["chạy", "run", "thực thi", "execute"])
                return [{"tool": run, "args": {"command": cmd},
                         "desc": "thực thi: %s" % cmd}]

        elif intent == "read":
            if read and files:
                return [{"tool": read, "args": {"path": files[0]},
                         "desc": "đọc file: %s" % files[0]}]

        elif intent == "search":
            s = self._pick_tool(_SEARCH_CANDIDATES, tools)
            if s:
                kw = self._strip_verb(
                    g, ["tìm kiếm", "tìm", "search", "grep"])
                return [{"tool": s, "args": {"pattern": kw},
                         "desc": "tìm kiếm: %s" % kw}]

        elif intent == "edit":
            # Without a model we cannot invent the new content; the useful
            # first step is reading the file so the next iteration (with a
            # model, or a human) has context.
            if read and files:
                return [{"tool": read, "args": {"path": files[0]},
                         "desc": "đọc file trước khi sửa: %s" % files[0]}]

        # generic / anything unhandled: run the goal as a command, mirroring
        # the autonomous controller's own default plan.
        if run:
            return [{"tool": run, "args": {"command": g},
                     "desc": "thực hiện: %s" % g}]
        return []

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
