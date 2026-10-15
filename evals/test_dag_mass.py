import asyncio
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from orchestration.dag import DAGScheduler, TaskNode
from orchestration.mass import MASSSwarm, AgentRole
from loop.react import ReActLoop, ToolCall

async def test_react_and_dag_mass():
    swarm = MASSSwarm()
    assert swarm.get_agent(AgentRole.CODER).role == AgentRole.CODER

    # Mock tools
    tools = {
        "echo": {
            "name": "echo",
            "func": lambda text: f"echoed: {text}"
        }
    }
    react = ReActLoop(tools=tools)
    step = await react.step(
        thought="I should echo test",
        action=ToolCall(name="echo", arguments={"text": "harness"}),
    )
    assert step.observation == "echoed: harness"
    print("[PASS] ReAct step verified.")

    # DAG test
    dag = DAGScheduler()
    execution_order = []

    async def plan_task(_):
        await asyncio.sleep(0.01)
        execution_order.append("plan")
        return {"plan": "2 modules to build"}

    async def worker_a(deps):
        await asyncio.sleep(0.02)
        execution_order.append("worker_a")
        return f"module_a built based on {deps['plan']['plan']}"

    async def worker_b(deps):
        await asyncio.sleep(0.02)
        execution_order.append("worker_b")
        return f"module_b built based on {deps['plan']['plan']}"

    async def review_task(deps):
        execution_order.append("review")
        assert "worker_a" in deps and "worker_b" in deps
        return "approved"

    dag.add_task(TaskNode(id="plan", name="Plan Tasks"), plan_task)
    dag.add_task(TaskNode(id="worker_a", name="Worker A", dependencies=["plan"]), worker_a)
    dag.add_task(TaskNode(id="worker_b", name="Worker B", dependencies=["plan"]), worker_b)
    dag.add_task(TaskNode(id="review", name="Review", dependencies=["worker_a", "worker_b"]), review_task)

    result = await dag.execute()
    assert result.success is True, f"DAG failed: {result.errors}"
    assert execution_order[0] == "plan"
    assert execution_order[-1] == "review"
    assert "worker_a" in execution_order and "worker_b" in execution_order
    print("[PASS] DAG Parallel Scheduler verified. Execution trace:", execution_order)

    # fail_fast must stop work that has not started. A sync handler that raises finishes
    # before the event loop schedules the next node, so the abort is seen deterministically.
    ff = DAGScheduler(fail_fast=True)
    ff_order = []

    def boom(_):
        raise RuntimeError("planner blew up")

    def later(_):
        ff_order.append("later")
        return "should not have run"

    ff.add_task(TaskNode(id="boom", name="Boom"), boom)
    ff.add_task(TaskNode(id="later", name="Later"), later)
    ff_result = await ff.execute()
    assert ff_result.success is False
    assert "planner blew up" in ff_result.errors["boom"], ff_result.errors
    assert ff_result.blocked == ["later"], ff_result.blocked
    assert ff_order == [], ff_order
    print("[PASS] fail_fast aborts unstarted nodes and reports them as blocked.")

    # A role's allow-list must name tools that exist: a wrong name silently strips the
    # subagent's toolset, and the role then does nothing.
    from environment import AgentEnvironment

    db = root_dir / "evals" / "dag_mass_test.db"
    if db.exists():
        db.unlink()
    env = AgentEnvironment(root_dir=root_dir, db_path=db)
    try:
        real = set(env.get_tools())
        for role in AgentRole:
            agent = swarm.get_agent(role)
            missing = [t for t in agent.allowed_tools if t not in real]
            assert not missing, f"{role.value} lists unknown tools: {missing}"
            assert agent.allowed_tools, f"{role.value} has no tools"
        assert swarm.resolve("coder").role is AgentRole.CODER
        assert swarm.resolve("WIZARD") is None
        assert swarm.resolve(None) is None
    finally:
        for closer in ("memory_store", "state_db", "delivery_ledger"):
            if hasattr(env, closer):
                getattr(env, closer).close()
        if db.exists():
            db.unlink()
    print("[PASS] MASS role tools all exist in AgentEnvironment; unknown roles resolve to None.")
    print("ALL DAG/MASS TESTS PASSED.")

if __name__ == "__main__":
    asyncio.run(test_react_and_dag_mass())
