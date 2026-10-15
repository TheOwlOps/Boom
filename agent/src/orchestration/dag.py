import asyncio
from typing import Any, Callable, Dict, List, Optional, Set
from pydantic import BaseModel

class TaskNode(BaseModel):
    id: str
    name: str
    dependencies: List[str] = []
    # run_fn is assigned at runtime

class DAGExecutionResult(BaseModel):
    success: bool
    outputs: Dict[str, Any]
    errors: Dict[str, str]
    # Nodes whose dependencies failed, so they never ran. Kept apart from `errors` so a
    # caller can tell "tried and broke" from "never attempted".
    blocked: List[str] = []

class DAGScheduler:
    def __init__(self, fail_fast: bool = False):
        self.nodes: Dict[str, TaskNode] = {}
        self.handlers: Dict[str, Callable] = {}
        # fail_fast stops work that has not started yet. A node already awaiting its
        # provider cannot be interrupted without tearing the connection, so it runs out.
        self.fail_fast = fail_fast
        self._abort: Optional[asyncio.Event] = None

    def add_task(self, node: TaskNode, handler: Callable):
        self.nodes[node.id] = node
        self.handlers[node.id] = handler

    def validate(self):
        """Topological validation: ensure no cycles."""
        visited: Set[str] = set()
        visiting: Set[str] = set()

        def dfs(node_id: str):
            if node_id in visiting:
                raise ValueError(f"Cycle detected at task '{node_id}'")
            if node_id in visited:
                return

            visiting.add(node_id)
            for dep in self.nodes[node_id].dependencies:
                if dep not in self.nodes:
                    raise ValueError(f"Missing dependency '{dep}' for task '{node_id}'")
                dfs(dep)
            visiting.remove(node_id)
            visited.add(node_id)

        for nid in self.nodes:
            if nid not in visited:
                dfs(nid)

    async def execute(self) -> DAGExecutionResult:
        self.validate()
        self._abort = asyncio.Event()

        outputs: Dict[str, Any] = {}
        errors: Dict[str, str] = {}
        blocked: List[str] = []
        tasks: Dict[str, asyncio.Task] = {}

        async def run_node(node_id: str):
            node = self.nodes[node_id]
            # Wait for all dependencies to complete
            for dep in node.dependencies:
                await tasks[dep]
                if dep in errors:
                    errors[node_id] = f"Dependency '{dep}' failed"
                    blocked.append(node_id)
                    return

            if self._abort.is_set():
                blocked.append(node_id)
                return

            try:
                fn = self.handlers[node_id]
                # Pass completed outputs of dependencies
                dep_outputs = {d: outputs[d] for d in node.dependencies if d in outputs}
                if asyncio.iscoroutinefunction(fn):
                    out = await fn(dep_outputs)
                else:
                    out = fn(dep_outputs)
                outputs[node_id] = out
            except Exception as e:
                errors[node_id] = str(e)
                if self.fail_fast:
                    self._abort.set()

        # Launch all tasks simultaneously; dependencies control execution order
        for nid in self.nodes:
            tasks[nid] = asyncio.create_task(run_node(nid))

        await asyncio.gather(*tasks.values())

        return DAGExecutionResult(
            success=len(errors) == 0,
            outputs=outputs,
            errors=errors,
            blocked=sorted(set(blocked)),
        )
