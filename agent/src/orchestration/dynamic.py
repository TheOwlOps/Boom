"""DynamicOrchestrator — spawns subagents on the fly based on task content.

Unlike MASSSwarm (fixed planner/coder/reviewer/critic/learner roles), this module
does NOT hardcode a team: it analyzes the task with keyword heuristics (Vietnamese
+ English, no LLM needed), spawns exactly the roles required, collects results,
retires them, and summarizes.

Responsibilities
----------------
- ``DynamicOrchestrator.analyze_task(task)`` → decides which roles are needed.
- ``spawn(role, task)`` → creates + runs one SubAgent.
- ``retire(agent_id)`` → marks done, cleans up.
- ``orchestrate(task)`` → full lifecycle: analyze → spawn → collect → retire → summary.

Real vs mock
------------
- If ``delegation_manager`` is provided (an object with a method compatible with
  ``DelegationManager.execute_task(SubTask)``), ``spawn()`` calls it to run a REAL
  subagent. Adapter: the role's task is wrapped as ``SubTask(goal=subtask, role=role)``;
  if the delegation_manager is a mock/fake in tests rather than a real
  DelegationManager, spawn still calls it normally (duck-typing).
- If ``delegation_manager=None``: ``spawn()`` runs in **MOCK** mode — returns a
  template-generated sample result per role. This mode is for dev/test only, **NOT
  real output**, and spawn's docstring states this explicitly. The ``result`` in
  mock mode carries ``"mock": True`` so it can't be mistaken for real output.
"""

from __future__ import annotations

import inspect
import re
import secrets
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# SubAgent
# ---------------------------------------------------------------------------
class SubAgent(BaseModel):
    """A subagent in the DynamicOrchestrator lifecycle."""

    id: str = Field(default="")
    role: str
    task: str
    status: str = "pending"  # pending | running | done | failed
    result: Any = None

    def model_post_init(self, __context: Any) -> None:
        # Auto-generate a unique id if the caller didn't pass one: agent_<ms>_<hex8>.
        if not self.id:
            self.id = f"agent_{int(time.time() * 1000)}_{secrets.token_hex(4)}"


# ---------------------------------------------------------------------------
# Role catalogue + keyword heuristic
# ---------------------------------------------------------------------------
#: Roles the DynamicOrchestrator knows how to spawn.
ROLES = (
    "coder",
    "researcher",
    "data_scientist",
    "visual_analyst",
    "financial_modeler",
    "validator",
)

#: Short description per role, used when building each agent's subtask prompt.
ROLE_BRIEF = {
    "coder": "Write/fix code to fulfill the request.",
    "researcher": "Search and research related information.",
    "data_scientist": "Analyze numbers and data.",
    "visual_analyst": "Analyze images/pictures/video.",
    "financial_modeler": "Model finances, prices, profits.",
    "validator": "Verify and review the other agents' results.",
}

# Each entry: (role, compiled regex). Order preserves the roles' order of
# appearance in the task, and dicts keep insertion order → stable output order.
# NOTE: the regex bodies below are intentionally left as-is (Vietnamese keywords
# with/without diacritics + English) so tasks written in many spellings still match.
_ROLE_KEYWORDS: List[tuple] = [
    (
        "coder",
        re.compile(
            r"(code|coding|viết|soạn|sửa|fix|bug|lỗi|debug|chương trình|phần mềm|script)",
            re.IGNORECASE,
        ),
    ),
    (
        "researcher",
        re.compile(
            r"(tìm|tim|nghiên cứu|nghien cuu|nghi[eê]n c[uứ]u|search|tra cứu|tra cuu|tài liệu|tai lieu|thông tin|thong tin)",
            re.IGNORECASE,
        ),
    ),
    (
        "data_scientist",
        re.compile(
            r"(số liệu|so lieu|phân tích|phan tich|data|dữ liệu|du lieu|thống kê|thong ke|biểu đồ|bieu do|dataset)",
            re.IGNORECASE,
        ),
    ),
    (
        "visual_analyst",
        re.compile(
            r"(ảnh|anh|hình|hinh|video|clip|picture|image|photo|ocr|nhận diện|nhan dien)",
            re.IGNORECASE,
        ),
    ),
    (
        "financial_modeler",
        re.compile(
            r"(tài chính|tai chinh|giá|gia|price|lợi nhuận|loi nhuan|doanh thu|chi phí|chi phi|đầu tư|dau tu|budget|ngân sách|ngan sach)",
            re.IGNORECASE,
        ),
    ),
]

#: Sample results for mock mode (delegation_manager=None). "mock": True marks them.
_MOCK_RESULTS = {
    "coder": "Mock coder: wrote/fixed code per request (simulated result).",
    "researcher": "Mock researcher: summarized found information (simulated result).",
    "data_scientist": "Mock data_scientist: analyzed the numbers (simulated result).",
    "visual_analyst": "Mock visual_analyst: analyzed the image/video (simulated result).",
    "financial_modeler": "Mock financial_modeler: built a financial model (simulated result).",
    "validator": "Mock validator: verified, no issues found (simulated result).",
}


# ---------------------------------------------------------------------------
# DynamicOrchestrator
# ---------------------------------------------------------------------------
class DynamicOrchestrator:
    """Spawns subagents dynamically based on task content.

    Args:
        delegation_manager: object with an ``execute_task(subtask)`` method
            compatible with the real ``DelegationManager``. If None → mock mode
            (see module docstring).
    """

    def __init__(self, delegation_manager: Any = None):
        self.delegation_manager = delegation_manager
        # Registry of live agents: agent_id -> SubAgent. retire() removes from here.
        self._agents: Dict[str, SubAgent] = {}

    # -- task analysis ------------------------------------------------------
    def analyze_task(self, task: str) -> List[str]:
        """Analyze the task → list of required roles (deduped, ordered).

        Keyword heuristics, no LLM. Rules:
        - each matching keyword group → adds the corresponding role;
        - no role matches → falls back to ``["researcher"]`` (ambiguous task?
          research first);
        - if >1 agent → always appends ``validator`` last for cross-checking.
        """
        text = task or ""
        roles: List[str] = []
        for role, pattern in _ROLE_KEYWORDS:
            if pattern.search(text) and role not in roles:
                roles.append(role)
        if not roles:
            roles.append("researcher")
        if len(roles) > 1 and "validator" not in roles:
            roles.append("validator")
        return roles

    # -- lifecycle ----------------------------------------------------------
    def _subtask_for(self, role: str, task: str) -> str:
        """Build a subtask prompt for one role: the role's brief + original task."""
        brief = ROLE_BRIEF.get(role, "")
        return f"[role={role}] {brief}\nTask: {task}".strip()

    def spawn(self, role: str, task: str) -> SubAgent:
        """Create and run a SubAgent for ``role``.

        - With ``delegation_manager``: wraps the subtask into an object with
          ``goal``/``role`` (compatible with delegation.py's ``SubTask``) and
          calls ``delegation_manager.execute_task(...)``. The real result lands
          in ``agent.result``; status ``done`` on success, ``failed`` on error.
          NOTE: DelegationManager.execute_task is ``async`` — here it's invoked
          synchronously via ``asyncio`` only when needed; callers that already
          have an event loop should await themselves. To keep a simple sync API,
          spawn runs the coroutine with asyncio.run when no loop is running,
          otherwise returns the agent in "running" state and leaves result
          collection to the caller (see orchestrate).
        - Without ``delegation_manager``: **MOCK** — immediately assigns the
          sample result ``{"mock": True, "role": ..., "summary": ...}`` with
          status ``done``. This is NOT real output, dev/test only.
        """
        agent = SubAgent(role=role, task=task, status="running")
        self._agents[agent.id] = agent

        if self.delegation_manager is None:
            # --- MOCK mode: no real runtime, return simulated results ---
            agent.result = {
                "mock": True,
                "role": role,
                "summary": _MOCK_RESULTS.get(role, f"Mock {role}: done (simulated result)."),
            }
            agent.status = "done"
            return agent

        # --- REAL mode: delegate through delegation_manager (duck-typing) ---
        subtask = self._wrap_subtask(role, task)
        try:
            outcome = self._run_maybe_async(
                self.delegation_manager.execute_task, subtask
            )
        except Exception as e:  # one failing subagent must not kill orchestrate
            agent.status = "failed"
            agent.result = {"error": f"{type(e).__name__}: {e}"}
            return agent

        if inspect.isawaitable(outcome):
            # Inside an event loop: can't block. Keep the agent "running",
            # stash the coroutine in result for the caller to await.
            agent.result = outcome
            return agent

        # DelegationManager.execute_task returns a dict with "status": "completed"/"failed".
        ok = isinstance(outcome, dict) and outcome.get("status") == "completed"
        agent.status = "done" if ok else "failed"
        agent.result = outcome
        return agent

    def _wrap_subtask(self, role: str, task: str) -> Any:
        """Wrap (role, task) into a SubTask-like object for the delegation_manager.

        Prefers importing the real SubTask from agent.src.tools.delegation; falls
        back to a compatible dataclass when the import fails (standalone tests).
        """
        try:
            from agent.src.tools.delegation import SubTask  # type: ignore

            return SubTask(goal=self._subtask_for(role, task), role=role)
        except Exception:
            from dataclasses import dataclass

            @dataclass
            class _FakeSubTask:
                goal: str
                role: str = ""

            return _FakeSubTask(goal=self._subtask_for(role, task), role=role)

    @staticmethod
    def _run_maybe_async(fn: Any, *args: Any) -> Any:
        """Call fn(*args); drive the coroutine to completion if it returns one.

        - No running event loop → ``asyncio.run`` (simple, synchronous).
        - Already inside a loop (async caller) → can't block; returns the
          coroutine itself so the caller can await it (spawn keeps the agent
          "running" in that case).
        """
        import asyncio
        import inspect

        res = fn(*args)
        if not inspect.isawaitable(res):
            return res
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(res)  # no loop → run synchronously right away
        return res  # inside a loop → return coroutine, caller awaits it

    def retire(self, agent_id: str) -> bool:
        """Mark the agent done and remove it from the registry. True if found.

        Preserves an existing terminal status (done/failed) for honest reporting;
        only agents still pending/running are flipped to done on retire.
        """
        agent = self._agents.get(agent_id)
        if agent is None:
            return False
        if agent.status in ("pending", "running"):
            agent.status = "done"
        del self._agents[agent_id]
        return True

    def get_agent(self, agent_id: str) -> Optional[SubAgent]:
        """Fetch a live agent by id (None if retired / nonexistent)."""
        return self._agents.get(agent_id)

    def live_agents(self) -> List[SubAgent]:
        """List of agents not yet retired."""
        return list(self._agents.values())

    # -- full pipeline ------------------------------------------------------
    def orchestrate(self, task: str) -> Dict[str, Any]:
        """Run the full lifecycle: analyze → spawn → collect → retire → summarize.

        Returns a dict::
            {
              "task": task,
              "roles": [...],
              "agents": [{"id":..., "role":..., "status":...}, ...],
              "results": {role: result, ...},
              "summary": "...",
            }

        In mock mode, spawn completes immediately so "collecting results" is just
        reading ``agent.result``. Same for a synchronous delegation_manager.
        If the delegation_manager returns coroutines (caller inside an event loop),
        orchestrate leaves those agents "running" and notes it in the summary —
        the async caller awaits and retires them afterwards.
        """
        roles = self.analyze_task(task)
        agents = [self.spawn(role, self._subtask_for(role, task)) for role in roles]

        results: Dict[str, Any] = {}
        pending: List[str] = []
        snapshots: List[Dict[str, str]] = []
        for agent in agents:
            # Snapshot status BEFORE retiring: retire removes the agent from the
            # registry, and we want honest reporting (including failures).
            snapshots.append({"id": agent.id, "role": agent.role, "status": agent.status})
            if agent.status in ("done", "failed"):
                results[agent.role] = agent.result
            else:
                # Async case: a coroutine is still pending in agent.result.
                pending.append(agent.id)

        # Retire every finished agent; pending ones are the caller's business.
        for agent in agents:
            if agent.id not in pending:
                self.retire(agent.id)

        summary = self._summarize(task, roles, results, pending)
        return {
            "task": task,
            "roles": roles,
            "agents": snapshots,
            "results": results,
            "summary": summary,
        }

    @staticmethod
    def _summarize(
        task: str, roles: List[str], results: Dict[str, Any], pending: List[str]
    ) -> str:
        """Build a short text summary from the agents' results (no LLM)."""
        lines = [f"Task: {task}", f"Roles: {', '.join(roles)}"]
        for role in roles:
            if role in results:
                r = results[role]
                if isinstance(r, dict):
                    s = r.get("summary") or r.get("error") or str(r)
                else:
                    s = str(r)
                lines.append(f"- {role}: {s}")
        if pending:
            lines.append(
                f"({len(pending)} agent(s) still running async — caller awaits and retires them)"
            )
        return "\n".join(lines)
