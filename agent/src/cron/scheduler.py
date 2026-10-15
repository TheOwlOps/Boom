import asyncio
import time
from typing import Any, Callable, Dict, List, Optional
from pydantic import BaseModel, Field

class CronJob(BaseModel):
    job_id: str
    name: str
    interval_seconds: int
    prompt: str
    target_channel: str = "telegram"
    last_run: float = 0.0
    enabled: bool = True

class CronScheduler:
    """Scheduled autonomous agent jobs with channel delivery."""

    def __init__(self, runner_callback: Optional[Callable] = None):
        self.runner_callback = runner_callback
        self.jobs: Dict[str, CronJob] = {}
        self.history: List[Dict[str, Any]] = []

    def add_job(self, job: CronJob):
        self.jobs[job.job_id] = job

    def list_jobs(self) -> List[Dict[str, Any]]:
        return [j.model_dump() for j in self.jobs.values()]

    async def tick(self) -> List[Dict[str, Any]]:
        """Check and execute due jobs."""
        now = time.time()
        executed = []

        for job in self.jobs.values():
            if not job.enabled:
                continue

            if now - job.last_run >= job.interval_seconds:
                job.last_run = now
                result_summary = "Triggered successfully"

                if self.runner_callback:
                    try:
                        res = await self.runner_callback(job.prompt, job.target_channel)
                        result_summary = str(res)
                    except Exception as e:
                        result_summary = f"Error: {e}"

                record = {
                    "job_id": job.job_id,
                    "name": job.name,
                    "target_channel": job.target_channel,
                    "timestamp": now,
                    "result": result_summary
                }
                self.history.append(record)
                executed.append(record)

        return executed
