"""MASS: multi-agent self-supervision, in Boom's shape.

The mechanism (UC Berkeley + Sakana AI, arXiv:2610.12176) runs one shared model in three
legs — an executor team that does the work, an evaluator that judges the result, and a
workflow optimizer that searches for a better division of labour and judges the candidates
itself. Its outer loop distills the winning trajectories back into the weights.

Boom has no weights, so the outer loop lands where Boom already keeps things durable: the
champion workflow is written to `memories/workflows.json` and replayed from there.

The paper names its own failure mode — solver, evaluator and optimizer share weights, so
they share blind spots, and a wrong answer can be graded as progress. Boom can attack that
one way the paper cannot: run the evaluator on a different provider when one is configured
(`DelegationManager.evaluator_llm`). With no second provider the evaluator is the same
model, and the verdict is only as independent as that model is.

A workflow is a linear chain of hops; branching and parallelism belong to `DAGScheduler`.
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from orchestration.mass import AgentRole

CHAMPION_FILE = "workflows.json"
DEFAULT_CRITICS = 2
DEFAULT_MAX_ROUNDS = 2
MAX_OPTIMIZE_CANDIDATES = 3

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class Step(BaseModel):
    """One hop: who acts, on what, and what they owe back."""
    role: str
    goal: str = ""
    contract: str = ""


class Workflow(BaseModel):
    name: str
    steps: List[Step]
    runs: int = 0
    wins: int = 0
    best_score: float = 0.0


class Defect(BaseModel):
    claim: str
    evidence: str = ""


class Verdict(BaseModel):
    passed: bool = False
    score: float = 0.0
    defects: List[Defect] = Field(default_factory=list)
    # False when the critic did not answer in the contract's shape. An unparsed verdict is
    # never a pass: it is the absence of supervision, not evidence of correctness.
    parsed: bool = True
    error: str = ""


class WorkflowRun(BaseModel):
    workflow: str
    outputs: List[Dict[str, Any]] = Field(default_factory=list)
    deliverable: str = ""
    verdicts: List[Verdict] = Field(default_factory=list)
    score: float = 0.0
    passed: bool = False
    rounds: int = 0


def parse_verdict(text: str) -> Verdict:
    """A critic's reply as a verdict, or an explicit failure to produce one."""
    match = _JSON_BLOCK.search(text or "")
    if not match:
        return Verdict(parsed=False, error="critic did not answer with JSON")
    try:
        data = json.loads(match.group(0))
    except ValueError as e:
        return Verdict(parsed=False, error=f"critic JSON did not parse: {e}")
    if not isinstance(data, dict) or "passed" not in data:
        return Verdict(parsed=False, error="critic JSON has no 'passed' field")
    defects = [
        Defect(claim=str(d.get("claim")), evidence=str(d.get("evidence") or ""))
        for d in (data.get("defects") or [])
        if isinstance(d, dict) and d.get("claim")
    ]
    try:
        score = max(0.0, min(1.0, float(data.get("score", 0.0))))
    except (TypeError, ValueError):
        score = 0.0
    return Verdict(passed=bool(data["passed"]), score=score, defects=defects)


# ------------------------------------------------------------------ persistence

def _champion_path(root_dir: Path) -> Path:
    return Path(root_dir) / "memories" / CHAMPION_FILE


def list_workflows(root_dir: Path) -> List[Workflow]:
    path = _champion_path(root_dir)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [Workflow(**w) for w in raw.get("workflows", []) if isinstance(w, dict) and w.get("name")]


def load_champion(root_dir: Path) -> Optional[Workflow]:
    """The best workflow kept so far, or None when none was ever kept."""
    workflows = [w for w in list_workflows(root_dir) if w.wins > 0]
    if not workflows:
        return None
    return max(workflows, key=lambda w: (w.best_score, w.wins))


def save_workflow(root_dir: Path, workflow: Workflow) -> Path:
    """Merge one workflow into the champion file, keeping the best score per name."""
    path = _champion_path(root_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = {w.name: w for w in list_workflows(root_dir)}
    previous = kept.get(workflow.name)
    if previous is not None:
        workflow.runs += previous.runs
        workflow.wins += previous.wins
        workflow.best_score = max(workflow.best_score, previous.best_score)
    kept[workflow.name] = workflow
    ordered = sorted(kept.values(), key=lambda w: w.best_score, reverse=True)
    path.write_text(
        json.dumps({"workflows": [w.model_dump() for w in ordered]}, indent=2),
        encoding="utf-8",
    )
    return path


# -------------------------------------------------------------------- supervisor

class SelfSupervisor:
    """The three legs over one shared model: executor, evaluator, workflow optimizer."""

    def __init__(
        self,
        delegation: Any,
        critics: int = DEFAULT_CRITICS,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
    ):
        self.delegation = delegation
        self.critics = max(1, int(critics))
        self.max_rounds = max(1, int(max_rounds))

    # ------------------------------------------------------------------ evaluator

    async def review(self, goal: str, deliverable: str, round_no: int = 1) -> List[Verdict]:
        """Critics in parallel, each told to falsify the work rather than judge it."""
        from tools.delegation import SubTask

        context = f"The task was:\n{goal}\n\nWork to falsify:\n{deliverable}"

        async def one(n: int) -> Verdict:
            report = await self.delegation.execute_task(
                SubTask(
                    task_id=f"critic-{n}-r{round_no}",
                    goal="Falsify the work described in the context.",
                    context=context,
                    role=AgentRole.CRITIC.value,
                ),
                llm=self.delegation.evaluator_llm,
            )
            if report["status"] != "completed":
                return Verdict(parsed=False, error=report.get("error") or "critic did not finish")
            return parse_verdict(report["summary"])

        return list(await asyncio.gather(*(one(n) for n in range(1, self.critics + 1))))

    @staticmethod
    def aggregate(verdicts: List[Verdict]) -> float:
        """Mean score, counting an unparsed verdict as 0: no verdict is not a pass."""
        if not verdicts:
            return 0.0
        return sum(v.score if v.parsed else 0.0 for v in verdicts) / len(verdicts)

    @staticmethod
    def all_passed(verdicts: List[Verdict]) -> bool:
        """Every critic must have answered in shape, and none may report a defect."""
        return bool(verdicts) and all(v.parsed and v.passed for v in verdicts)

    async def supervise(
        self,
        goal: str,
        deliverable: str,
        role: str,
        contract: str = "",
        context: str = "",
        task_id: str = "task",
    ) -> Dict[str, Any]:
        """Adversarial rounds over one deliverable, revising it until it survives.

        Returns the revised deliverable with the evidence for and against it. The single
        place this loop lives: a subtask with `verify`, and a workflow's final hop, both
        come through here.
        """
        from tools.delegation import SubTask

        verdicts: List[Verdict] = []
        rounds = 0
        score = 0.0
        passed = False
        for round_no in range(1, self.max_rounds + 1):
            verdicts = await self.review(goal, deliverable, round_no)
            rounds = round_no
            score = self.aggregate(verdicts)
            passed = self.all_passed(verdicts)
            if passed or round_no == self.max_rounds:
                break

            # A revision, not a fresh attempt: the producer is handed the defects.
            defects = [d.model_dump() for v in verdicts for d in v.defects]
            revision = await self.delegation.execute_task(
                SubTask(
                    task_id=f"{task_id}-revise-{round_no}",
                    goal=goal,
                    context=(context + "\n\n" if context else "")
                    + "Defects the verifier evidenced:\n"
                    + json.dumps(defects, indent=2),
                    role=role,
                    contract=contract,
                )
            )
            if revision["status"] == "completed" and revision["summary"]:
                deliverable = revision["summary"]

        return {
            "deliverable": deliverable,
            "verdicts": verdicts,
            "rounds": rounds,
            "score": score,
            "passed": passed,
        }

    # ------------------------------------------------------------------- executor

    async def run_workflow(
        self, workflow: Workflow, goal: str, verify: bool = True
    ) -> WorkflowRun:
        """Run the hops in order, then supervise the deliverable adversarially."""
        from tools.delegation import SubTask

        run = WorkflowRun(workflow=workflow.name)
        if not workflow.steps:
            run.deliverable = ""
            return run

        context = ""
        for index, step in enumerate(workflow.steps, start=1):
            report = await self.delegation.execute_task(
                SubTask(
                    task_id=f"{workflow.name}-{index}",
                    goal=step.goal or goal,
                    context=context,
                    role=step.role,
                    contract=step.contract,
                )
            )
            run.outputs.append(report)
            if report["status"] != "completed":
                break
            run.deliverable = report["summary"]
            # Hop chain: each hop sees what the previous one produced.
            context = "Results from the previous hop:\n" + json.dumps(report, indent=2, default=str)

        if not verify or not run.deliverable:
            return run

        last = workflow.steps[len(run.outputs) - 1]
        outcome = await self.supervise(
            goal, run.deliverable, last.role, last.contract, context, workflow.name
        )
        run.deliverable = outcome["deliverable"]
        run.verdicts = outcome["verdicts"]
        run.rounds = outcome["rounds"]
        run.score = outcome["score"]
        run.passed = outcome["passed"]
        return run

    # ------------------------------------------------------------------ optimizer

    async def propose(self, goal: str, prior: Optional[WorkflowRun], count: int) -> List[Workflow]:
        """The workflow-optimizer leg: candidate divisions of labour, as JSON."""
        llm = self.delegation.optimizer_llm or self.delegation.llm
        if llm is None:
            return []

        system = (
            "You design multi-agent workflows. Answer with JSON only, no prose:\n"
            '{"workflows": [{"name": "short-name", "steps": '
            '[{"role": "<role>", "goal": "what this hop does", "contract": "what it owes back"}]}]}\n'
            f"Roles available: {', '.join(self.delegation.swarm.role_names())}.\n"
            "Each workflow is a linear chain, 1-4 hops. Use a critic hop only when the work "
            "it would supervise is something a verifier can actually check."
        )
        prompt = f"Goal:\n{goal}"
        if prior is not None and prior.verdicts:
            defects = [d.model_dump() for v in prior.verdicts for d in v.defects]
            prompt += (
                f"\n\nThe previous workflow '{prior.workflow}' scored {prior.score:.2f} "
                f"and drew these evidenced defects:\n{json.dumps(defects, indent=2)}\n"
                "Propose workflows that would avoid them."
            )

        try:
            resp = await llm.chat_complete(
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
            )
        except Exception:
            return []  # an optimizer that cannot be reached proposes nothing, it does not invent

        match = _JSON_BLOCK.search(getattr(resp, "content", "") or "")
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return []

        known = set(self.delegation.swarm.role_names())
        workflows: List[Workflow] = []
        for raw in (data.get("workflows") or [])[: max(1, count)]:
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            steps = [
                Step(role=s["role"], goal=s.get("goal") or "", contract=s.get("contract") or "")
                for s in (raw.get("steps") or [])
                if isinstance(s, dict) and s.get("role") in known
            ]
            if steps:
                workflows.append(Workflow(name=str(raw["name"]), steps=steps))
        return workflows

    async def optimize(
        self,
        goal: str,
        root_dir: Path,
        candidates: Optional[List[Workflow]] = None,
        max_candidates: int = MAX_OPTIMIZE_CANDIDATES,
    ) -> Dict[str, Any]:
        """Run candidate workflows, keep the best one if it beats the standing champion."""
        champion = load_champion(root_dir)
        pool = list(candidates or [])
        if not pool:
            pool = await self.propose(goal, None, max_candidates)
        if not pool:
            return {
                "error": "no candidate workflows: the optimizer proposed nothing",
                "champion": champion.name if champion else None,
                "improved": False,
            }

        scored: List[Dict[str, Any]] = []
        for workflow in pool[:max_candidates]:
            run = await self.run_workflow(workflow, goal)
            workflow.runs += 1
            workflow.best_score = max(workflow.best_score, run.score)
            scored.append({
                "name": workflow.name,
                "score": run.score,
                "passed": run.passed,
                "rounds": run.rounds,
                "steps": [s.role for s in workflow.steps],
                "defects": [d.claim for v in run.verdicts for d in v.defects],
                "error": next((o.get("error") for o in run.outputs if o.get("error")), None),
            })

        best = max(scored, key=lambda s: s["score"])
        best_workflow = next(w for w in pool if w.name == best["name"])
        improved = champion is None or best["score"] > champion.best_score
        if improved:
            best_workflow.wins += 1
            save_workflow(root_dir, best_workflow)
            champion_name = best_workflow.name
        else:
            champion_name = champion.name if champion else None

        return {
            "champion": champion_name,
            "previous_champion": champion.name if champion else None,
            "improved": improved,
            "best_score": best["score"],
            "candidates": scored,
            # Same weights in every leg unless a second provider is configured; see module docstring.
            "evaluator_model": getattr(self.delegation.evaluator_llm, "model", None)
            or getattr(self.delegation.llm, "model", None),
        }
