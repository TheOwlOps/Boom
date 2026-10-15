import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class TrajectoryStep(BaseModel):
    step_id: int
    thought: str
    action_tool: Optional[str] = None
    action_args: Optional[Dict[str, Any]] = None
    observation: Optional[str] = None
    timestamp: float = Field(default_factory=time.time)

class TrajectoryRecorder:
    """Records fine-grained agent reasoning trajectories for evals and dataset collection."""

    def __init__(self, session_id: str, log_dir: Optional[Path] = None):
        self.session_id = session_id
        self.log_dir = Path(log_dir) if log_dir else Path.cwd() / "trajectories"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.steps: List[TrajectoryStep] = []
        self._counter = 0

    def record_step(
        self,
        thought: str,
        tool: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        observation: Optional[str] = None
    ) -> TrajectoryStep:
        self._counter += 1
        step = TrajectoryStep(
            step_id=self._counter,
            thought=thought,
            action_tool=tool,
            action_args=args,
            observation=observation
        )
        self.steps.append(step)
        return step

    def export_json(self) -> Path:
        out_file = self.log_dir / f"trajectory_{self.session_id}_{int(time.time())}.json"
        data = {
            "session_id": self.session_id,
            "total_steps": len(self.steps),
            "steps": [s.model_dump() for s in self.steps]
        }
        out_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return out_file
