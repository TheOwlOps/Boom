"""Subagent delegation: a DAG of subtasks, each one a real ReAct turn.

This is where the orchestration pieces meet. `DAGScheduler` owns the order — which subtask
waits on which — and `MASSSwarm` owns what each kind of subagent is (its system
instruction, its contract and the tools it may use). Each node's handler is one child
`ReActLoop`, so a node is a real turn with its own transcript, its own approval-gated tools
and its own token ceiling. A finished dependency's report is handed to its dependents as
context.

A subtask marked `verify` is then supervised adversarially by `SelfSupervisor`: critics try
to falsify the deliverable and must evidence every defect, and the producer revises against
those defects until the critics pass or the round limit is reached. `workflow` runs a named
champion workflow instead of a task list, and `optimize_workflow` searches for a better one.

A subagent is never handed `delegate_task` (recursion has no bound) nor `execute_code`
(whose kernel holds the parent's whole tool dict, so it would hand the withheld tools
straight back).
"""

import asyncio
import json
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field

from orchestration.dag import DAGScheduler, TaskNode
from orchestration.mass import MASSSwarm
from orchestration.self_supervision import (
    DEFAULT_CRITICS,
    DEFAULT_MAX_ROUNDS,
    MAX_OPTIMIZE_CANDIDATES,
    SelfSupervisor,
    Step,
    Workflow,
    list_workflows,
    load_champion,
)

MAX_PARALLEL_SUBAGENTS = 4
DEFAULT_SUBAGENT_TURNS = 8
DEFAULT_SUBAGENT_TOKEN_BUDGET = 60000

# Withheld from every subagent, whatever its role allows.
SUBTASK_HIDDEN_TOOLS = {"delegate_task", "execute_code"}


class SubTask(BaseModel):
    task_id: str = Field(default="")
    goal: str
    context: Union[Dict[str, Any], str] = ""
    role: str = ""
    depends_on: List[str] = Field(default_factory=list)
    # Overrides the role's contract for this one subtask.
    contract: str = ""
    # Adversarial review of this subtask's deliverable before it counts as done.
    verify: bool = False


class _SubsetEnv:
    """The parent environment with a few tools withheld; every other attribute passes through.

    `allowed` (a role's tool list) and `hidden` compose: a tool must be allowed, and not
    hidden. Deliberately no role lists the `mcp__*` tools, so a role-scoped subagent does
    not see them; a role-less subagent sees whatever the parent has.
    """

    def __init__(self, env: Any, hidden: Optional[set] = None, allowed: Optional[List[str]] = None):
        self._env = env
        self._hidden = set(hidden or ())
        self._allowed = set(allowed) if allowed is not None else None

    @property
    def delegation(self) -> None:
        """None, always: a child loop must not rebind (or borrow) the parent's manager."""
        return None

    def _keep(self, name: str) -> bool:
        if name in self._hidden:
            return False
        return self._allowed is None or name in self._allowed

    def get_tools(self) -> Dict[str, Any]:
        return {k: v for k, v in self._env.get_tools().items() if self._keep(k)}

    def get_tools_schema(self) -> List[Dict[str, Any]]:
        return [s for s in self._env.get_tools_schema() if self._keep(s["name"])]

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)  # never recurse back into __getattr__
        return getattr(self._env, name)


class DelegationManager:
    """Delegates subtasks to parallel subagents."""

    def __init__(
        self,
        max_turns: int = DEFAULT_SUBAGENT_TURNS,
        token_budget: int = DEFAULT_SUBAGENT_TOKEN_BUDGET,
        critics: int = DEFAULT_CRITICS,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
    ):
        self.max_turns = max_turns
        self.token_budget = token_budget
        self.swarm = MASSSwarm()
        self.supervisor = SelfSupervisor(self, critics=critics, max_rounds=max_rounds)
        # Same provider as the executor unless a second one is configured: the shared blind
        # spot MASS's own paper warns about is only reduced by a genuinely different model.
        self.evaluator_llm: Any = None
        self.optimizer_llm: Any = None
        self.llm: Any = None
        self.parent: Any = None

    def bind(self, parent_loop: Any):
        """Attach the parent loop. Unbound, delegation reports an error instead of faking one."""
        self.llm = getattr(parent_loop, "llm", None)
        self.parent = parent_loop

    @property
    def available(self) -> bool:
        return self.llm is not None and self.parent is not None

    @property
    def root_dir(self):
        """Where champion workflows live: the parent environment's project root."""
        return getattr(getattr(self.parent, "env", None), "root_dir", None)

    def _failure(self, task_id: str, message: str, role: str = "") -> Dict[str, Any]:
        return {
            "task_id": task_id, "role": role, "status": "failed",
            "summary": "", "steps": 0, "tools": [], "error": message,
        }

    async def execute_task(
        self,
        task: SubTask,
        dep_outputs: Optional[Dict[str, Any]] = None,
        llm: Any = None,
    ) -> Dict[str, Any]:
        """Run one subtask to completion and report what actually happened.

        `llm` overrides the provider for this subtask — the evaluator leg uses it to run
        critics on a different model from the one that produced the work.
        """
        from loop.react import ReActLoop

        if not task.goal.strip():
            return self._failure(task.task_id, "subtask has no goal", task.role)
        if not self.available:
            return self._failure(
                task.task_id,
                "no subagent runtime: DelegationManager is not bound to a ReActLoop",
                task.role,
            )

        agent = self.swarm.resolve(task.role)
        if task.role and agent is None:
            return self._failure(
                task.task_id,
                f"unknown role '{task.role}'; known roles: {', '.join(self.swarm.role_names())}",
                task.role,
            )

        parent = self.parent
        if parent.env is not None:
            tools: Any = _SubsetEnv(
                parent.env,
                SUBTASK_HIDDEN_TOOLS,
                agent.allowed_tools if agent else None,
            )
        else:
            tools = parent.tools

        role = agent.role.value if agent else ""
        contract = task.contract or (agent.contract if agent else "")
        system_prompt = None
        if agent:
            system_prompt = (
                f"{agent.system_instruction}\n\n"
                f"You are the '{role}' subagent working for a parent agent. Report your result "
                f"as your final answer. You cannot delegate to further subagents."
            )
            if contract:
                system_prompt += f"\n\nYour contract:\n{contract}"

        child = ReActLoop(
            tools,
            llm=llm or self.llm,
            max_turns=self.max_turns,
            accountant=getattr(parent, "accountant", None),
            # A nested id keeps the child's transcript attributable and searchable.
            session_id=f"{parent.session_id}/{task.task_id}",
            prompt_builder=getattr(parent, "prompt_builder", None),
            channel=getattr(parent, "channel", "tui"),
            system_prompt=system_prompt,
            token_budget=self.token_budget,
        )

        sections = [task.goal]
        if dep_outputs:
            sections.append(
                "Results from the subtasks this one depends on:\n"
                + json.dumps(dep_outputs, indent=2, default=str)
            )
        if task.context:
            context = task.context if isinstance(task.context, str) else json.dumps(task.context)
            sections.append(f"Context from the parent agent:\n{context}")

        result = await child.step("\n\n".join(sections))
        trace = result.get("trace") or []
        report = {
            "task_id": task.task_id,
            "role": role,
            "status": "completed" if result.get("success") else "failed",
            "summary": result.get("action") or "",
            "steps": result.get("steps", 0),
            "tools": [s["action"]["name"] for s in trace if s.get("action")],
            "error": result.get("error"),
        }

        if task.verify and report["status"] == "completed" and report["summary"]:
            outcome = await self.supervisor.supervise(
                task.goal, report["summary"], role, contract, task_id=task.task_id
            )
            report["summary"] = outcome["deliverable"]
            report["verified"] = outcome["passed"]
            report["score"] = outcome["score"]
            report["rounds"] = outcome["rounds"]
            report["defects"] = [d.model_dump() for v in outcome["verdicts"] for d in v.defects]
            report["unparsed_verdicts"] = [v.error for v in outcome["verdicts"] if not v.parsed]

        return report

    def _parse(self, tasks: List[Dict[str, Any]]) -> List[SubTask]:
        subtasks: List[SubTask] = []
        for idx, raw in enumerate(tasks or []):
            if not isinstance(raw, dict):
                continue
            depends_on = raw.get("depends_on") or []
            if isinstance(depends_on, str):
                depends_on = [depends_on]
            subtasks.append(
                SubTask(
                    task_id=str(raw.get("task_id") or f"subagent-{idx + 1}"),
                    goal=str(raw.get("goal") or raw.get("task") or ""),
                    context=raw.get("context") or "",
                    role=str(raw.get("role") or ""),
                    depends_on=[str(d) for d in depends_on],
                    contract=str(raw.get("contract") or ""),
                    verify=bool(raw.get("verify")),
                )
            )
        return subtasks

    async def delegate_task(
        self,
        tasks: List[Dict[str, Any]] = None,
        fail_fast: bool = False,
        workflow: str = "",
        goal: str = "",
    ) -> Dict[str, Any]:
        if not self.available:
            return {"error": "delegate_task has no subagent runtime: no LLM bound to the loop."}

        if workflow:
            return await self.run_workflow(workflow, goal)

        subtasks = self._parse(tasks)
        if not subtasks:
            return {"error": "delegate_task needs a non-empty 'tasks' list."}

        known = {t.task_id for t in subtasks}
        ids = [t.task_id for t in subtasks]
        duplicates = sorted({tid for tid in ids if ids.count(tid) > 1})
        if duplicates:
            return {"error": f"duplicate task_id(s): {', '.join(duplicates)}"}

        # A dependency the plan never defines is dropped by DAGScheduler.validate(), which
        # would raise and lose every other subtask's work. Report it against its own task.
        bad_deps: Dict[str, List[str]] = {
            t.task_id: [d for d in t.depends_on if d not in known]
            for t in subtasks
            if any(d not in known for d in t.depends_on)
        }

        # Bounded: each subagent is a full LLM conversation loop against the same provider.
        limit = asyncio.Semaphore(MAX_PARALLEL_SUBAGENTS)
        dag = DAGScheduler(fail_fast=fail_fast)

        for task in subtasks:
            async def handler(dep_outputs, task=task):
                if task.task_id in bad_deps:
                    return self._failure(
                        task.task_id,
                        f"unknown dependency/dependencies: {', '.join(bad_deps[task.task_id])}",
                        task.role,
                    )
                async with limit:
                    try:
                        return await self.execute_task(task, dep_outputs)
                    except Exception as e:
                        # One subagent crashing must not discard the other subagents' work.
                        return self._failure(task.task_id, f"{type(e).__name__}: {e}", task.role)

            dag.add_task(
                TaskNode(
                    id=task.task_id,
                    name=task.goal[:60] or task.task_id,
                    dependencies=[d for d in task.depends_on if d in known],
                ),
                handler,
            )

        outcome = await dag.execute()

        results: List[Dict[str, Any]] = []
        for task in subtasks:
            if task.task_id in outcome.outputs:
                results.append(outcome.outputs[task.task_id])
            else:
                reason = outcome.errors.get(task.task_id) or (
                    "not run: a subtask it depends on did not finish"
                )
                results.append(self._failure(task.task_id, reason, task.role))

        return {
            "total_subagents": len(results),
            "completed": sum(1 for r in results if r["status"] == "completed"),
            "blocked": outcome.blocked,
            "results": results,
        }

    async def run_workflow(self, name: str, goal: str) -> Dict[str, Any]:
        """Replay a kept workflow: hops in order, then adversarial supervision."""
        if not goal.strip():
            return {"error": "running a workflow needs a 'goal'."}

        root = self.root_dir
        available = list_workflows(root) if root is not None else []
        chosen = next((w for w in available if w.name == name), None)
        if chosen is None:
            names = ", ".join(w.name for w in available) or "none kept yet"
            return {"error": f"unknown workflow '{name}'; kept workflows: {names}"}

        run = await self.supervisor.run_workflow(chosen, goal)
        return {
            "workflow": run.workflow,
            "total_subagents": len(run.outputs),
            "completed": sum(1 for o in run.outputs if o["status"] == "completed"),
            "deliverable": run.deliverable,
            "verified": run.passed,
            "score": run.score,
            "rounds": run.rounds,
            "defects": [d.model_dump() for v in run.verdicts for d in v.defects],
            "unparsed_verdicts": [v.error for v in run.verdicts if not v.parsed],
            "results": run.outputs,
        }

    async def optimize_workflow(
        self,
        goal: str,
        candidates: List[Dict[str, Any]] = None,
        max_candidates: int = MAX_OPTIMIZE_CANDIDATES,
    ) -> Dict[str, Any]:
        """Search for a better workflow: the optimizer leg of MASS, kept in memories/."""
        if not self.available:
            return {"error": "optimize_workflow has no subagent runtime: no LLM bound to the loop."}
        if not goal.strip():
            return {"error": "optimize_workflow needs a goal."}
        root = self.root_dir
        if root is None:
            return {"error": "no project root to keep the champion workflow in."}

        pool: Optional[List[Workflow]] = None
        if candidates:
            known = set(self.swarm.role_names())
            pool = []
            for raw in candidates:
                if not isinstance(raw, dict) or not raw.get("name"):
                    continue
                steps = [
                    Step(role=s["role"], goal=s.get("goal") or "", contract=s.get("contract") or "")
                    for s in (raw.get("steps") or [])
                    if isinstance(s, dict) and s.get("role") in known
                ]
                if steps:
                    pool.append(Workflow(name=str(raw["name"]), steps=steps))
            if not pool:
                return {"error": "none of the candidate workflows had a usable step."}

        return await self.supervisor.optimize(goal, root, pool, max_candidates)

    def champion(self) -> Optional[Workflow]:
        root = self.root_dir
        return load_champion(root) if root is not None else None
