"""Check the MASS self-supervision layer: findings, revisions, and the kept champion.

Two halves. The first runs a real DelegationManager with a scripted provider so a subtask
marked `verify` is produced, falsified, revised against the evidenced defects, and cleared —
on a second provider, which is the only defence against the shared blind spot the MASS paper
names. The second drives the optimizer leg through a stub delegation, because what is under
test there is the scoring and the persistence, not the subagents again.
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from environment import AgentEnvironment
from loop.react import ReActLoop
from orchestration.mass import MASSSwarm
from orchestration.self_supervision import (
    SelfSupervisor,
    Step,
    Verdict,
    Workflow,
    list_workflows,
    load_champion,
    parse_verdict,
    save_workflow,
)
from providers.llm import LLMResponse, LLMUsage
from tools.delegation import DelegationManager

PRODUCE_GOAL = "Write the report"
PLAIN_GOAL = "Write the summary"
CRITIC_GOAL = "Falsify the work described in the context."

FAILED_VERDICT = (
    '{"passed": false, "score": 0.4, "defects": '
    '[{"claim": "answer unverified", "evidence": "no command was run"}]}'
)
PASSED_VERDICT = '{"passed": true, "score": 1.0, "defects": []}'
VERDICT_LOG = '{"passed": false, "score": 5.0, "defects": [{"claim": "x"}, {"no_claim": 1}]}'


class ProducerLLM:
    """The executor leg. Revises when it is handed defects, otherwise answers fresh."""

    def __init__(self):
        self.model = "producer"
        self.calls = []

    async def chat_complete(self, messages, tools_schema=None):
        last_user = [m["content"] for m in messages if m.get("role") == "user"][-1]
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        self.calls.append({"prompt": last_user, "system": system})
        if "Defects the verifier evidenced" in last_user:
            answer = "draft v2 (fixed)"
        elif last_user.startswith(PRODUCE_GOAL):
            answer = "draft v1"
        else:
            answer = "summary"
        return LLMResponse(
            content=answer, usage=LLMUsage(input_tokens=3, output_tokens=2), finish_reason="stop"
        )


class CriticLLM:
    """The evaluator leg: fails the draft, clears the revision."""

    def __init__(self):
        self.model = "critic"
        self.calls = []

    async def chat_complete(self, messages, tools_schema=None):
        last_user = [m["content"] for m in messages if m.get("role") == "user"][-1]
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        self.calls.append({"prompt": last_user, "system": system})
        answer = PASSED_VERDICT if "draft v2" in last_user else FAILED_VERDICT
        return LLMResponse(
            content=answer, usage=LLMUsage(input_tokens=4, output_tokens=4), finish_reason="stop"
        )


def test_verdicts():
    ok = parse_verdict('Here it is: {"passed": true, "score": 0.8} — done')
    assert ok.parsed and ok.passed and ok.score == 0.8 and ok.defects == [], ok

    assert parse_verdict("I could not check it.").parsed is False
    assert parse_verdict("").parsed is False
    assert parse_verdict('{"passed": tru}').parsed is False
    assert parse_verdict('{"score": 0.5}').parsed is False

    clamped = parse_verdict(VERDICT_LOG)
    assert clamped.parsed and clamped.score == 1.0, clamped
    assert [d.claim for d in clamped.defects] == ["x"], clamped  # a defect with no claim is dropped
    print("[PASS] parse_verdict: unparsed is never a pass; score clamped; defects need a claim.")

    assert SelfSupervisor.aggregate([ok, parse_verdict("nonsense")]) == 0.4
    assert SelfSupervisor.aggregate([]) == 0.0
    assert SelfSupervisor.all_passed([ok]) is True
    assert SelfSupervisor.all_passed([ok, parse_verdict("nonsense")]) is False
    assert SelfSupervisor.all_passed([]) is False
    assert SelfSupervisor.all_passed([Verdict(passed=True, score=1.0, parsed=True)]) is True
    print("[PASS] aggregate counts an unparsed verdict as 0; an empty panel never passes.")


def test_persistence():
    tmp = Path(tempfile.mkdtemp(prefix="boom-workflows-"))
    try:
        assert list_workflows(tmp) == [] and load_champion(tmp) is None

        weak = Workflow(name="weak", steps=[Step(role="coder", goal="draft")], best_score=0.3, wins=1)
        strong = Workflow(name="strong", steps=[Step(role="coder", goal="draft")], best_score=0.9, wins=1)
        save_workflow(tmp, weak)
        path = save_workflow(tmp, strong)
        assert path == tmp / "memories" / "workflows.json", path
        assert load_champion(tmp).name == "strong"

        # Same name again: runs accumulate, the best score is kept, a lower score cannot demote it.
        save_workflow(tmp, Workflow(name="strong", steps=strong.steps, best_score=0.1, runs=2))
        champion = load_champion(tmp)
        assert champion.best_score == 0.9 and champion.runs == 2 and champion.wins == 1, champion

        # A workflow that never won is not a champion.
        save_workflow(tmp, Workflow(name="untried", steps=strong.steps))
        assert load_champion(tmp).name == "strong", load_champion(tmp)
        assert {w.name for w in list_workflows(tmp)} == {"weak", "strong", "untried"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("[PASS] champion persistence: best score per name, runs accumulate, no win is no champion.")


async def test_verified_subtask():
    test_db = root_dir / "evals" / "self_supervision_test.db"
    if test_db.exists():
        test_db.unlink()

    env = AgentEnvironment(root_dir=root_dir, db_path=test_db, yolo=True)
    producer, critic = ProducerLLM(), CriticLLM()
    loop = ReActLoop(env, producer, accountant=env.token_accountant, session_id="supervised")
    manager = env.delegation
    assert manager.available is True, "the loop must bind the delegation manager"
    # The second provider is the point: critics must not be the model that produced the work.
    manager.evaluator_llm = critic

    try:
        result = await manager.delegate_task([
            {"task_id": "report", "goal": PRODUCE_GOAL, "role": "coder", "verify": True},
            {"task_id": "plain", "goal": PLAIN_GOAL, "role": "coder"},
        ])
        assert "error" not in result, result
        reports = {r["task_id"]: r for r in result["results"]}

        report = reports["report"]
        assert report["status"] == "completed", report
        assert report["summary"] == "draft v2 (fixed)", report
        assert report["verified"] is True and report["score"] == 1.0 and report["rounds"] == 2, report
        assert report["defects"] == [] and report["unparsed_verdicts"] == [], report

        # Supervision is opt-in: the unsupervised subtask reports none of these keys.
        assert "verified" not in reports["plain"], reports["plain"]
        assert reports["plain"]["summary"] == "summary", reports["plain"]

        assert len(critic.calls) == 4, [c["prompt"][:40] for c in critic.calls]
        assert all(c["prompt"].startswith(CRITIC_GOAL) for c in critic.calls)
        assert all("adversarial verifier" in c["system"] for c in critic.calls), critic.calls[0]["system"]
        assert all('"passed"' in c["system"] for c in critic.calls), "critic must be told the verdict shape"

        rounds = [c["prompt"] for c in critic.calls]
        assert "draft v1" in rounds[0] and "draft v1" in rounds[1], rounds[:2]
        assert "draft v2 (fixed)" in rounds[2] and "draft v2 (fixed)" in rounds[3], rounds[2:]

        # The revision is a revision: it was handed the defect the critics evidenced.
        revisions = [c for c in producer.calls if "Defects the verifier evidenced" in c["prompt"]]
        assert len(revisions) == 1, producer.calls
        assert "answer unverified" in revisions[0]["prompt"], revisions[0]["prompt"]
        producers = [c for c in producer.calls if c["prompt"].startswith(PRODUCE_GOAL)]
        assert len(producers) == 2, [c["prompt"][:40] for c in producer.calls]
        assert all("Report the exact command you ran" in c["system"] for c in producers), producers[0]["system"]

        # Replaying a kept workflow: hops in order, then the same supervision over the last
        # hop. root_dir is overridden so the test keeps its champion out of the project's.
        tmp = Path(tempfile.mkdtemp(prefix="boom-replay-"))
        try:
            save_workflow(tmp, Workflow(name="wf", steps=[Step(role="coder", goal=PRODUCE_GOAL)]))
            replay = type(
                "ReplayManager", (DelegationManager,), {"root_dir": property(lambda self: tmp)}
            )(critics=1)
            replay.bind(loop)
            replay.evaluator_llm = critic

            unknown = await replay.delegate_task(workflow="nope", goal=PRODUCE_GOAL)
            assert "unknown workflow 'nope'" in unknown["error"], unknown

            out = await replay.delegate_task(workflow="wf", goal=PRODUCE_GOAL)
            assert [r["task_id"] for r in out["results"]] == ["wf-1"], out["results"]
            assert out["deliverable"] == "draft v2 (fixed)" and out["verified"] is True, out
            assert out["rounds"] == 2 and out["score"] == 1.0 and out["defects"] == [], out
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        for closer in ("memory_store", "state_db", "delivery_ledger"):
            if hasattr(env, closer):
                getattr(env, closer).close()
        if test_db.exists():
            test_db.unlink()
    print("[PASS] verified subtask: fail, revise against the evidence, clear — critics on the 2nd provider.")


class StubDelegation:
    """Executor stands in for the optimizer test; the scoring and persistence are the real code."""

    def __init__(self, proposer=None):
        self.swarm = MASSSwarm()
        self.llm = proposer
        self.evaluator_llm = None
        self.optimizer_llm = None
        self.llm_calls = []

    async def execute_task(self, task, dep_outputs=None, llm=None):
        report = {
            "task_id": task.task_id, "role": task.role, "status": "completed",
            "summary": "", "steps": 1, "tools": [], "error": None,
        }
        if task.role == "critic":
            good = "good work" in (task.context or "")
            report["summary"] = PASSED_VERDICT.replace("1.0", "0.9") if good else FAILED_VERDICT
        else:
            report["summary"] = "good work" if "good" in task.goal else "bad work"
        return report


class ProposerLLM:
    def __init__(self, payload):
        self.model = "proposer"
        self.payload = payload

    async def chat_complete(self, messages, tools_schema=None):
        if isinstance(self.payload, Exception):
            raise self.payload
        return LLMResponse(
            content=self.payload, usage=LLMUsage(input_tokens=5, output_tokens=5), finish_reason="stop"
        )


async def test_optimizer():
    tmp = Path(tempfile.mkdtemp(prefix="boom-optimize-"))
    try:
        delegation = StubDelegation()
        supervisor = SelfSupervisor(delegation, critics=1, max_rounds=2)
        goal = "Summarise a change to the codebase."
        good = Workflow(name="good", steps=[Step(role="coder", goal="produce good work")])
        bad = Workflow(name="bad", steps=[Step(role="coder", goal="produce bad work")])

        out = await supervisor.optimize(goal, tmp, [good, bad], 3)
        assert out["champion"] == "good" and out["improved"] is True, out
        assert out["previous_champion"] is None and out["best_score"] == 0.9, out
        assert [c["name"] for c in out["candidates"]] == ["good", "bad"], out["candidates"]
        assert out["candidates"][1]["error"] is None, out["candidates"][1]
        assert out["candidates"][1]["defects"] == ["answer unverified"], out["candidates"][1]
        assert load_champion(tmp).name == "good"
        assert [w.name for w in list_workflows(tmp)] == ["good"], "a losing candidate is not kept"

        # Same candidates again: nothing beats the standing champion, so nothing is rewritten.
        again = await supervisor.optimize(goal, tmp, [good, bad], 3)
        assert again["improved"] is False and again["champion"] == "good", again
        assert again["previous_champion"] == "good", again
        assert load_champion(tmp).runs == 1, load_champion(tmp)  # the second run must not re-win

        # The optimizer leg itself, both when it answers and when it cannot be reached.
        proposed = SelfSupervisor(
            StubDelegation(ProposerLLM(
                '{"workflows": [{"name": "w1", "steps": [{"role": "coder", "goal": "g"}]}, '
                '{"name": "w2", "steps": [{"role": "WIZARD", "goal": "g"}]}]}'
            )),
            critics=1,
        )
        workflows = await proposed.propose(goal, None, 3)
        assert [w.name for w in workflows] == ["w1"], workflows  # an unknown role is not a step

        unreachable = SelfSupervisor(StubDelegation(ProposerLLM(RuntimeError("no provider"))), critics=1)
        assert await unreachable.propose(goal, None, 3) == []
        empty = await SelfSupervisor(StubDelegation(ProposerLLM("no json at all")), critics=1).optimize(
            goal, tmp, None, 3
        )
        assert empty["error"] and empty["improved"] is False, empty
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("[PASS] optimizer: keeps a better workflow, rejects a worse one, proposes nothing when it cannot.")


async def main():
    test_verdicts()
    test_persistence()
    await test_verified_subtask()
    await test_optimizer()
    print("ALL SELF-SUPERVISION TESTS PASSED.")


if __name__ == "__main__":
    asyncio.run(main())
