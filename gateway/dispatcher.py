"""Bounded, durable, per-channel queue for agent turns: a heavy task must not stall a channel.

What the channels need and did not have:

* A cap on how many turns run at once. A chat platform can deliver a burst, and every one of
  those would otherwise start its own ReAct loop against the same model and tools.
* Fairness. The pool picks round-robin across channels, and `BOOM_GATEWAY_CHANNEL_CONCURRENCY`
  caps how much of it one channel may hold, so one noisy chat cannot starve the others.
* Backpressure. A full queue is refused out loud, per channel and in total, rather than the
  process quietly swallowing the message or growing without bound.
* Durability. A job that was accepted is written to `memories/gateway_jobs.json` before it runs,
  so a restart re-queues it instead of losing it.
* Cancellation. `cancel(job_id)` drops a queued job or cancels a running one at its next await;
  `cancel_channel(channel)` stops everything waiting or running on one channel.
* A retry. A turn that raises is retried `BOOM_GATEWAY_RETRIES` times (default 1) after a
  doubling `BOOM_GATEWAY_RETRY_DELAY` backoff (default 2s, then 4s) before the channel is told
  it failed — a provider hiccup should not cost the user their message.
* Priority. A job may carry a higher priority (0-9) and then runs ahead of the lower-priority
  turns, on its channel and in the rotation.

A caller that can hold its request open (`wait=True`) gets the reply in-band; a channel that
must not be stalled (Telegram's poll loop, Discord's socket) is acked immediately and the reply
goes out later through the daemon's own route back to that channel.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel, Field

JOBS_FILE = "gateway_jobs.json"
DEFAULT_CONCURRENCY = 2
DEFAULT_MAX_QUEUED = 32          # per channel
DEFAULT_RETRIES = 1              # extra attempts after a turn raises
DEFAULT_RETRY_DELAY = 2.0        # seconds before the first retry, doubling each attempt
DEFAULT_PRIORITY = 0
MAX_PRIORITY = 9
TOTAL_QUEUE_FACTOR = 4           # total waiting cap = max_queued * this
MAX_KEPT = 200                   # finished records kept for the operator, dropped oldest-first


class TurnJob(BaseModel):
    job_id: str
    channel: str
    sender_id: str
    sender_name: str = ""
    text: str
    message_id: str = ""
    # Where the reply goes, when that is not the sender: a Discord guild message is answered in
    # the channel it arrived on, not to the author.
    target: str = ""
    priority: int = DEFAULT_PRIORITY
    created_at: float = Field(default_factory=time.time)
    attempts: int = 0
    status: str = "queued"  # queued | running | done | failed | cancelled

    def reply_target(self) -> str:
        return self.target or self.sender_id


class _Channel:
    """One channel's waiting line and its share of the pool."""

    __slots__ = ("name", "queue", "running", "limit")

    def __init__(self, name: str, limit: int):
        self.name = name
        self.queue: deque[TurnJob] = deque()
        self.running = 0
        self.limit = limit


class TurnDispatcher:
    def __init__(
        self,
        runner: Callable[[str, str, str], Awaitable[str]],
        root_dir: Optional[Path] = None,
        concurrency: Optional[int] = None,
        max_queued: Optional[int] = None,
        channel_concurrency: Optional[int] = None,
        channel_limits: Optional[dict] = None,
        recover: Optional[Callable[[TurnJob, str], Awaitable[Any]]] = None,
        failure_text: str = "",
        retries: Optional[int] = None,
        retry_delay: Optional[float] = None,
    ):
        self.runner = runner
        self.root_dir = Path(root_dir) if root_dir else None
        self.concurrency = concurrency or int(os.getenv("BOOM_GATEWAY_CONCURRENCY", DEFAULT_CONCURRENCY))
        self.max_queued = max_queued or int(os.getenv("BOOM_GATEWAY_QUEUE", DEFAULT_MAX_QUEUED))
        # A channel may use the whole pool unless it is capped; the cap is what makes one busy
        # chat unable to hold every slot.
        self.channel_concurrency = channel_concurrency or int(
            os.getenv("BOOM_GATEWAY_CHANNEL_CONCURRENCY", self.concurrency)
        )
        self.channel_limits: dict = dict(channel_limits or {})
        # Delivery for jobs whose caller is gone: the original connection died with the process.
        self.recover = recover
        # What a channel is told when a turn fails and there is no caller left to report it to.
        # Without it a queued turn would fail and the user would hear nothing at all.
        self.failure_text = failure_text
        self.retries = int(os.getenv("BOOM_GATEWAY_RETRIES", DEFAULT_RETRIES)) if retries is None else retries
        self.retry_delay = (float(os.getenv("BOOM_GATEWAY_RETRY_DELAY", DEFAULT_RETRY_DELAY))
                            if retry_delay is None else retry_delay)

        self.jobs: dict[str, TurnJob] = {}
        self.delivered = 0
        self.failed = 0
        self.rejected = 0
        self.cancelled = 0
        self.retried = 0
        self.queued = 0  # waiting, all channels together

        self._channels: dict[str, _Channel] = {}
        self._cursor = 0  # where the round-robin picks up next
        self._workers: list[asyncio.Task] = []
        self._tasks: dict[str, asyncio.Task] = {}
        self._deliveries: set[asyncio.Task] = set()
        self._backoffs: set[asyncio.Task] = set()
        self._cancel_requested: set[str] = set()
        self._waiters: dict[str, asyncio.Future] = {}
        self._callbacks: dict[str, Callable[[str], Awaitable[None]]] = {}
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------ persistence
    def _path(self) -> Optional[Path]:
        if self.root_dir is None:
            return None
        return self.root_dir / "memories" / JOBS_FILE

    def _save(self) -> None:
        path = self._path()
        if path is None:
            return
        unfinished = [j.model_dump() for j in self.jobs.values() if j.status in ("queued", "running")]
        finished = [j.model_dump() for j in self.jobs.values() if j.status in ("done", "failed", "cancelled")]
        blob = {"unfinished": unfinished, "finished": finished[-MAX_KEPT:]}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)  # a killed daemon must never leave a half-written queue

    def _load(self) -> list[TurnJob]:
        path = self._path()
        if path is None or not path.exists():
            return []
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        recovered = []
        for raw in blob.get("unfinished", []):
            try:
                job = TurnJob(**raw)
            except Exception:
                continue
            # A job that was mid-flight when the process died is retried, not assumed done.
            job.status = "queued"
            recovered.append(job)
        return recovered

    # ------------------------------------------------------------------ lifecycle
    async def start(self):
        self._workers = [asyncio.create_task(self._worker()) for _ in range(self.concurrency)]
        recovered = 0
        for job in self._load():
            self._enqueue(job)
            recovered += 1
            print(f"[dispatcher] re-queued {job.job_id} from a previous run "
                  f"({job.channel} {job.sender_id})", flush=True)
        if recovered:
            self._wake.set()

    async def stop(self):
        workers, self._workers = self._workers, []
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        for task in list(self._tasks.values()):
            task.cancel()
        # A job waiting out its backoff is still queued, and _save writes it as unfinished, so
        # the next start picks it back up from the jobs file.
        for task in list(self._backoffs):
            task.cancel()
        self._save()
        if self._deliveries:
            # A reply that was already on its way should still land.
            await asyncio.gather(*self._deliveries, return_exceptions=True)

    # ------------------------------------------------------------------ channels
    def _limit_for(self, channel: str) -> int:
        return max(1, min(self.channel_limits.get(channel, self.channel_concurrency), self.concurrency))

    def _channel(self, channel: str) -> _Channel:
        state = self._channels.get(channel)
        if state is None:
            state = _Channel(channel, self._limit_for(channel))
            self._channels[channel] = state
        return state

    def _enqueue(self, job: TurnJob) -> None:
        """Add to the channel's line. Priority orders it; equal priority stays first-in-first-out."""
        state = self._channel(job.channel)
        self.jobs[job.job_id] = job
        job.status = "queued"
        if job.priority:
            state.queue.insert(
                next((i for i, waiting in enumerate(state.queue) if waiting.priority < job.priority),
                     len(state.queue)),
                job,
            )
        else:
            state.queue.append(job)
        self.queued += 1

    # ------------------------------------------------------------------ submit
    async def submit(
        self,
        job: TurnJob,
        deliver: Optional[Callable[[str], Awaitable[None]]] = None,
        wait: bool = False,
    ) -> dict:
        """Queue the turn. `wait` holds the caller until it has run; otherwise the caller is acked.

        Nobody runs in-band here: the pool is the only thing that runs turns, so the cap is the
        cap and two paths cannot each use it.
        """
        state = self._channel(job.channel)
        if len(state.queue) >= self.max_queued or self.queued >= self.max_queued * TOTAL_QUEUE_FACTOR:
            self.rejected += 1
            return {"status": "rejected", "job_id": job.job_id,
                    "error": f"queue is full ({len(state.queue)}/{self.max_queued} on {job.channel})"}
        self._enqueue(job)
        if deliver is not None:
            self._callbacks[job.job_id] = deliver
        if not wait:
            self._save()
            self._wake.set()
            return {"status": "queued", "job_id": job.job_id, "position": len(state.queue) - 1}

        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters[job.job_id] = waiter
        self._save()
        self._wake.set()
        return await waiter

    def _drop_queued(self, job: TurnJob, reason: str) -> bool:
        """Take a job out of its channel's line. False if it was not waiting there."""
        state = self._channels.get(job.channel)
        if state is None or not any(j.job_id == job.job_id for j in state.queue):
            return False
        state.queue = deque(j for j in state.queue if j.job_id != job.job_id)
        self.queued -= 1
        job.status = "cancelled"
        self.cancelled += 1
        self._settle(job, None, reason)
        return True

    def cancel(self, job_id: str) -> dict:
        """Stop a job: a queued one is dropped, a running one is cancelled at its next await."""
        job = self.jobs.get(job_id)
        if job is None:
            return {"status": "unknown", "job_id": job_id}
        task = self._tasks.get(job_id)
        if task is not None:
            self._cancel_requested.add(job_id)
            task.cancel()
            return {"status": "cancelling", "job_id": job_id, "channel": job.channel}
        if self._drop_queued(job, "cancelled before it ran"):
            self._save()
            return {"status": "cancelled", "job_id": job_id, "channel": job.channel}
        return {"status": "not running", "job_id": job_id, "channel": job.channel}

    def cancel_channel(self, channel: str) -> dict:
        """Stop everything on one channel: waiting jobs are dropped, running ones cancelled."""
        state = self._channels.get(channel)
        stopped = {"channel": channel, "queued": 0, "running": 0}
        if state is None:
            return stopped
        for job in list(state.queue):
            if self._drop_queued(job, "cancelled with the channel"):
                stopped["queued"] += 1
        for job_id, task in list(self._tasks.items()):
            job = self.jobs.get(job_id)
            if job is not None and job.channel == channel:
                self._cancel_requested.add(job_id)
                task.cancel()
                stopped["running"] += 1
        self._save()
        return stopped

    # ------------------------------------------------------------------ the pool
    def _pick(self) -> Optional[tuple[TurnJob, _Channel]]:
        """Next job: the most urgent head if one is waiting, else the next channel in rotation.

        Round-robin is the fairness between channels; priority is the explicit exception to it,
        so a job has to be marked to jump the rotation.
        """
        eligible = [(name, state) for name, state in self._channels.items()
                    if state.queue and state.running < state.limit]
        if not eligible:
            return None
        urgent = max(state.queue[0].priority for _, state in eligible)
        if urgent > 0:
            name, state = next((n, s) for n, s in eligible if s.queue[0].priority == urgent)
        else:
            names = list(self._channels)
            for i in range(len(names)):
                name = names[(self._cursor + i) % len(names)]
                state = self._channels[name]
                if state.queue and state.running < state.limit:
                    self._cursor = (self._cursor + i + 1) % len(names)
                    break
            else:
                return None
        job = state.queue.popleft()
        self.queued -= 1
        state.running += 1
        return job, state

    async def _worker(self):
        while True:
            # Clear before picking: a submit that lands after the clear is then either seen by
            # this pick or sets the event again, so no job is left waiting unseen.
            self._wake.clear()
            picked = self._pick()
            if picked is None:
                await self._wake.wait()
                continue
            job, state = picked
            try:
                await self._execute(job)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # one bad job must not take the pool down
                print(f"[dispatcher] job {job.job_id} crashed: {type(e).__name__}: {e}", flush=True)
            finally:
                state.running -= 1
                if not state.queue and state.running == 0:
                    self._channels.pop(state.name, None)
                self._wake.set()
                self._save()

    async def _execute(self, job: TurnJob) -> None:
        task = asyncio.create_task(self._run(job))
        self._tasks[job.job_id] = task
        reply: Optional[str] = None
        error: Optional[str] = None
        try:
            reply = await task
        except asyncio.CancelledError:
            if job.job_id not in self._cancel_requested:
                raise  # the pool is stopping: the job stays unfinished on disk and is re-queued
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            print(f"[dispatcher] {job.job_id} attempt {job.attempts} failed: {error}", flush=True)
        finally:
            self._tasks.pop(job.job_id, None)

        if job.job_id in self._cancel_requested:
            self._cancel_requested.discard(job.job_id)
            job.status = "cancelled"
            self.cancelled += 1
            print(f"[dispatcher] {job.job_id} cancelled while running", flush=True)
            self._settle(job, None, "cancelled while running")
            return
        if error is not None:
            if job.attempts <= self.retries:
                self.retried += 1
                # It is waiting, not running: the worker is free for another job meanwhile.
                job.status = "queued"
                task = asyncio.create_task(self._retry_later(job))
                self._backoffs.add(task)
                task.add_done_callback(self._backoffs.discard)
                return
            job.status = "failed"
            self.failed += 1
            self._settle(job, None, error)
            return
        self.delivered += 1
        self._settle(job, reply, None)

    def _requeue(self, job: TurnJob) -> None:
        """A retry goes to the front: it has already waited its turn once."""
        state = self._channel(job.channel)
        job.status = "queued"
        state.queue.appendleft(job)
        self.queued += 1
        self._wake.set()

    async def _retry_later(self, job: TurnJob) -> None:
        """Wait out the backoff, then put the job back. The job stays unfinished on disk meanwhile."""
        await asyncio.sleep(self.retry_delay * (2 ** (job.attempts - 1)))
        self._requeue(job)

    async def _run(self, job: TurnJob) -> str:
        job.status = "running"
        job.attempts += 1
        reply = str(await self.runner(job.text, job.channel, job.sender_name))
        job.status = "done"
        return reply

    def _settle(self, job: TurnJob, reply: Optional[str], error: Optional[str]) -> None:
        """Hand the outcome to whoever is waiting for it, then deliver it if nobody is."""
        outcome = {"status": job.status, "job_id": job.job_id, "reply": reply, "error": error}
        deliver = self._callbacks.pop(job.job_id, None)
        waiter = self._waiters.pop(job.job_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(outcome)
            return
        if reply is None:
            if job.status != "failed" or not self.failure_text:
                return  # nothing to deliver, and inventing a reply is not an option
            reply = self.failure_text
        task = asyncio.create_task(self._deliver(job, reply, deliver))
        self._deliveries.add(task)
        task.add_done_callback(self._deliveries.discard)

    async def _deliver(self, job: TurnJob, reply: str,
                       deliver: Optional[Callable[[str], Awaitable[None]]]) -> None:
        try:
            if deliver is not None:
                await deliver(reply)
            elif self.recover is not None:
                # No caller left: the job came back from disk, or the channel is one the daemon
                # reaches on its own.
                await self.recover(job, reply)
            else:
                print(f"[dispatcher] {job.job_id} finished with no way to deliver the reply",
                      flush=True)
        except Exception as e:
            print(f"[dispatcher] delivery of {job.job_id} failed: {type(e).__name__}: {e}",
                  flush=True)

    def status(self) -> dict:
        """What the operator needs to see: what is running, what is waiting, what went wrong."""
        return {
            "concurrency": self.concurrency,
            "channel_concurrency": self.channel_concurrency,
            "running": sum(s.running for s in self._channels.values()),
            "queued": self.queued,
            "max_queued": self.max_queued,
            "delivering": len(self._deliveries),
            "retrying": len(self._backoffs),
            "delivered": self.delivered,
            "failed": self.failed,
            "rejected": self.rejected,
            "cancelled": self.cancelled,
            "retries": self.retries,
            "retried": self.retried,
            "running_jobs": [{"job_id": jid, "channel": self.jobs[jid].channel}
                             for jid in self._tasks if jid in self.jobs],
            "channels": {n: {"running": s.running, "queued": len(s.queue), "limit": s.limit}
                         for n, s in self._channels.items()},
        }


def new_job(channel: str, sender_id: str, sender_name: str, text: str, message_id: str,
            target: str = "", priority: int = DEFAULT_PRIORITY) -> TurnJob:
    return TurnJob(
        job_id=uuid.uuid4().hex[:12], channel=channel, sender_id=sender_id,
        sender_name=sender_name, text=text, message_id=message_id, target=target,
        priority=max(0, min(int(priority), MAX_PRIORITY)),
    )
