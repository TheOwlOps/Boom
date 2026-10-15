"""The turn queue under real load: real asyncio, a real runner, a real restart.

There is no model to call in an offline test, so the runner is a stand-in — but it does real
work: it sleeps, it counts how many turns are inside it at once (per channel and overall), it
can be told to fail, and it records when it is cancelled. What is asserted is the dispatcher's
own behaviour: the pool cap and the per-channel cap hold, the round-robin serves a quiet channel
while a noisy one is flooded, a full queue is refused instead of growing, a running or waiting
turn can be cancelled, a failing turn is reported as failed, and a job that was accepted before
a restart is re-run rather than lost.
"""

import asyncio
import sys
import tempfile
import time
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))
sys.path.insert(0, str(root_dir / "agent" / "src"))

from gateway.dispatcher import JOBS_FILE, TurnDispatcher, new_job


class Runner:
    """Real async work, with an observable concurrency high-water mark."""

    def __init__(self, delay: float = 0.05, fail_on=(), fail_once=(), gated=()):
        self.delay = delay
        self.fail_on = set(fail_on)
        self.fail_once = set(fail_once)
        self.gated = set(gated)  # these texts wait on the gate, so a test can hold a slot open
        self.gate = asyncio.Event()
        self.active = 0
        self.peak = 0
        self.active_by: dict = {}
        self.peak_by: dict = {}
        self.order = []
        self.calls = []
        self.seen: dict = {}
        self.cancelled = 0

    async def __call__(self, text: str, channel: str, sender: str) -> str:
        self.calls.append((text, channel, sender))
        self.order.append((channel, text))
        self.seen[text] = self.seen.get(text, 0) + 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.active_by[channel] = self.active_by.get(channel, 0) + 1
        self.peak_by[channel] = max(self.peak_by.get(channel, 0), self.active_by[channel])
        try:
            if text in self.gated:
                await self.gate.wait()
            else:
                await asyncio.sleep(self.delay)
            if text in self.fail_on or (text in self.fail_once and self.seen[text] == 1):
                raise RuntimeError("runner exploded")
            return f"echo:{text}"
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1
            self.active_by[channel] -= 1


async def wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def wait_idle(dispatcher, timeout: float = 10.0) -> bool:
    st = dispatcher.status
    return await wait_until(
        lambda: st()["queued"] == 0 and st()["running"] == 0 and st()["delivering"] == 0
        and st()["retrying"] == 0, timeout
    )


def collector(sink: list):
    async def deliver(text: str):
        sink.append(text)
    return deliver


async def test_fairness_and_caps():
    """One flooded channel holds only its own slot; the quiet one is served early."""
    runner = Runner(delay=0.05)
    d = TurnDispatcher(runner, concurrency=2, max_queued=8, channel_limits={"noisy": 1})
    await d.start()
    try:
        tasks = [asyncio.create_task(d.submit(new_job("noisy", "u", "ryan", f"n{i}", f"m{i}"),
                                              wait=True)) for i in range(5)]
        tasks.append(asyncio.create_task(d.submit(new_job("quiet", "u", "ryan", "q1", "mq"),
                                                  wait=True)))
        # Limits are reported per channel while it has work: 1 for the capped one, the pool size
        # for an uncapped one.
        assert await wait_until(lambda: len(d.status()["channels"]) == 2), d.status()
        channels = d.status()["channels"]
        assert channels["noisy"]["limit"] == 1 and channels["quiet"]["limit"] == 2, channels

        outs = await asyncio.gather(*tasks)
        assert all(o["status"] == "done" for o in outs), outs
        assert [o["reply"] for o in outs[:5]] == [f"echo:n{i}" for i in range(5)], outs
        assert outs[5]["reply"] == "echo:q1", outs[5]

        assert runner.peak == 2, f"pool cap breached: peak {runner.peak}"
        assert runner.peak_by["noisy"] == 1, f"channel cap breached: {runner.peak_by}"
        # Fairness: the quiet turn runs second, not behind all five noisy ones.
        assert runner.order[0] == ("noisy", "n0"), runner.order
        assert runner.order[1] == ("quiet", "q1"), runner.order
    finally:
        await d.stop()
    print("[PASS] Pool cap 2 and per-channel cap 1 hold; the quiet channel ran second.")


async def test_backpressure():
    """A full queue is refused out loud, not absorbed."""
    runner = Runner(delay=0.15)
    failed_text = "Xử lý thất bại."
    d = TurnDispatcher(runner, concurrency=1, max_queued=2, failure_text=failed_text)
    await d.start()
    try:
        first = asyncio.create_task(d.submit(new_job("c", "u", "ryan", "j1", "m1"), wait=True))
        assert await wait_until(lambda: d.status()["running"] == 1), d.status()

        second = asyncio.create_task(d.submit(new_job("c", "u", "ryan", "j2", "m2"), wait=True))
        third = asyncio.create_task(d.submit(new_job("c", "u", "ryan", "j3", "m3"), wait=True))
        assert await wait_until(lambda: d.status()["queued"] == 2), d.status()

        refused = await d.submit(new_job("c", "u", "ryan", "j4", "m4"))
        assert refused["status"] == "rejected", refused
        assert "full" in refused["error"], refused
        assert d.status()["rejected"] == 1, d.status()

        assert (await first)["reply"] == "echo:j1"
        assert (await second)["reply"] == "echo:j2"
        assert (await third)["reply"] == "echo:j3"
        assert await wait_idle(d), d.status()
        assert "j4" not in [text for _, text in runner.order], runner.order

        # A turn that raises is reported as failed, and no reply is invented for it.
        runner.fail_on = {"boom"}
        outcome = await d.submit(new_job("c", "u", "ryan", "boom", "m5"), wait=True)
        assert outcome["status"] == "failed" and outcome["reply"] is None, outcome
        assert outcome["error"] and "RuntimeError" in outcome["error"], outcome
        assert d.status()["failed"] == 1, d.status()

        # A failing turn nobody is waiting for still answers its channel: the ack was already
        # sent, so silence here would leave the user with a promise and no answer.
        answers = []
        acked = await d.submit(new_job("c", "u", "ryan", "boom", "m6"), collector(answers))
        assert acked["status"] == "queued", acked
        assert await wait_until(lambda: d.status()["failed"] == 2), d.status()
        assert await wait_idle(d), d.status()
        assert answers == [failed_text], answers
    finally:
        await d.stop()
    print("[PASS] Full queue refused with an error; a failing turn is failed, not answered.")


async def test_cancel():
    """An operator can stop a turn, whether it is still waiting or already running."""
    runner = Runner(delay=30.0)
    d = TurnDispatcher(runner, concurrency=1, max_queued=4)
    await d.start()
    try:
        running = asyncio.create_task(d.submit(new_job("c", "u", "ryan", "long", "m1"), wait=True))
        assert await wait_until(lambda: d.status()["running"] == 1), d.status()
        waiting = asyncio.create_task(d.submit(new_job("c", "u", "ryan", "later", "m2"), wait=True))
        assert await wait_until(lambda: d.status()["queued"] == 1), d.status()
        ids = {job.text: job.job_id for job in d.jobs.values()}
        assert set(ids) == {"long", "later"}, ids

        stopped = d.cancel(ids["later"])
        assert stopped["status"] == "cancelled", stopped
        assert (await waiting)["status"] == "cancelled", "a cancelled waiter is told so"
        assert d.status()["queued"] == 0, d.status()

        stopping = d.cancel(ids["long"])
        assert stopping["status"] == "cancelling", stopping
        assert (await running)["status"] == "cancelled", "the running turn reports cancelled"
        assert await wait_until(lambda: runner.cancelled == 1), runner.cancelled
        assert await wait_idle(d), d.status()
        assert d.status()["cancelled"] == 2, d.status()
        assert "later" not in [text for _, text in runner.order], runner.order
        assert d.cancel("nosuchjob")["status"] == "unknown"
    finally:
        await d.stop()
    print("[PASS] A queued turn was dropped and a running one cancelled at its next await.")


async def test_restart_recovers():
    """A job accepted before the process died is re-run by the next dispatcher, not lost."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        delivered = []
        runner = Runner(delay=30.0)

        first = TurnDispatcher(runner, root_dir=root, concurrency=1, max_queued=4)
        await first.start()
        await first.submit(new_job("telegram", "42", "ryan", "gate-1", "m1"),
                           collector(delivered))
        assert await wait_until(lambda: first.status()["running"] == 1), first.status()
        queued = await first.submit(new_job("telegram", "42", "ryan", "gate-2", "m2"),
                                    collector(delivered))
        assert queued["status"] == "queued", queued
        assert (root / "memories" / JOBS_FILE).exists(), "an accepted job must be on disk"
        await first.stop()

        # The next process has no callbacks left, so recovery delivers through the daemon's own
        # route back to the channel.
        recovered = []

        async def recover(job, reply):
            recovered.append((job.job_id, job.channel, job.reply_target(), reply))

        second = TurnDispatcher(Runner(), root_dir=root, concurrency=2, recover=recover)
        await second.start()
        try:
            assert await wait_until(lambda: len(recovered) >= 2, 10), recovered
            assert await wait_idle(second), second.status()
            channels = {(c, t) for _, c, t, _ in recovered}
            assert channels == {("telegram", "42")}, recovered
            assert all(r.startswith("echo:gate-") for *_, r in recovered), recovered
        finally:
            await second.stop()
    print(f"[PASS] Restart re-queued {len(recovered)} unfinished job(s) from {JOBS_FILE} and "
          f"delivered them through the recovery route.")


async def test_priority():
    """A high-priority turn jumps its own channel's line; the rest keep their order."""
    runner = Runner(delay=0.01, gated={"block"})
    d = TurnDispatcher(runner, concurrency=1, max_queued=8)
    await d.start()
    try:
        blocker = asyncio.create_task(d.submit(new_job("c", "u", "ryan", "block", "m0"), wait=True))
        assert await wait_until(lambda: d.status()["running"] == 1), d.status()

        low = asyncio.create_task(d.submit(new_job("c", "u", "ryan", "low", "m1"), wait=True))
        high = asyncio.create_task(d.submit(new_job("c", "u", "ryan", "high", "m2", priority=5),
                                          wait=True))
        mid = asyncio.create_task(d.submit(new_job("c", "u", "ryan", "mid", "m3"), wait=True))
        assert await wait_until(lambda: d.status()["queued"] == 3), d.status()

        ids = {job.text: job.job_id for job in d.jobs.values()}
        d.cancel(ids["block"])
        await blocker
        outs = await asyncio.gather(low, high, mid)
        assert all(o["status"] == "done" for o in outs), outs
        order = [text for _, text in runner.order if text != "block"]
        assert order == ["high", "low", "mid"], order
    finally:
        await d.stop()
    print("[PASS] Priority 5 ran before the two normal turns waiting on the same channel.")


async def test_retry():
    """A turn that fails once is retried; one that keeps failing is reported, not retried forever."""
    runner = Runner(delay=0.01, fail_once={"flaky"})
    delivered = []
    d = TurnDispatcher(runner, concurrency=1, max_queued=4, retries=1, failure_text="failed",
                       retry_delay=0.2)
    await d.start()
    try:
        acked = await d.submit(new_job("c", "u", "ryan", "flaky", "m1"), collector(delivered))
        assert acked["status"] == "queued", acked
        # The retry waits out its backoff instead of hammering the runner immediately.
        assert await wait_until(lambda: d.status()["retrying"] == 1), d.status()
        assert runner.seen["flaky"] == 1, runner.seen
        assert await wait_until(lambda: d.status()["delivered"] == 1), d.status()
        assert await wait_idle(d), d.status()
        assert delivered == ["echo:flaky"], delivered
        assert runner.seen["flaky"] == 2, runner.seen
        assert d.status()["retried"] == 1 and d.status()["failed"] == 0, d.status()
        done = [j for j in d.jobs.values() if j.text == "flaky"][0]
        assert done.attempts == 2 and done.status == "done", done

        # With the retries spent, the failure is reported and the channel is told.
        stubborn = TurnDispatcher(Runner(delay=0.01, fail_on={"always"}), concurrency=1,
                                  max_queued=4, retries=1, failure_text="boom-text",
                                  retry_delay=0.05)
        await stubborn.start()
        try:
            answers = []
            await stubborn.submit(new_job("c", "u", "ryan", "always", "m2"), collector(answers))
            assert await wait_until(lambda: stubborn.status()["failed"] == 1), stubborn.status()
            assert await wait_idle(stubborn), stubborn.status()
            assert answers == ["boom-text"], answers
            assert stubborn.status()["retried"] == 1, stubborn.status()
        finally:
            await stubborn.stop()
    finally:
        await d.stop()
    print("[PASS] One retry recovered the flaky turn; a permanent failure answered the channel.")


async def test_priority_across_channels():
    """A marked turn beats the rotation: round-robin is the default, urgency is the exception."""
    runner = Runner(delay=0.01, gated={"block"})
    d = TurnDispatcher(runner, concurrency=1, max_queued=8)
    await d.start()
    try:
        blocker = asyncio.create_task(d.submit(new_job("a", "u", "ryan", "block", "m0"), wait=True))
        assert await wait_until(lambda: d.status()["running"] == 1), d.status()
        # b is next in the rotation and would normally go first.
        normal = [asyncio.create_task(d.submit(new_job("b", "u", "ryan", t, f"mb{i}"), wait=True))
                  for i, t in enumerate(("n1", "n2"))]
        urgent = asyncio.create_task(d.submit(new_job("c", "u", "ryan", "urgent", "mc",
                                                      priority=7), wait=True))
        assert await wait_until(lambda: d.status()["queued"] == 3), d.status()

        ids = {job.text: job.job_id for job in d.jobs.values()}
        d.cancel(ids["block"])
        await blocker
        outs = await asyncio.gather(urgent, *normal)
        assert all(o["status"] == "done" for o in outs), outs
        order = [text for _, text in runner.order if text != "block"]
        assert order == ["urgent", "n1", "n2"], order
    finally:
        await d.stop()
    print("[PASS] Priority 7 on a third channel ran before the rotation's next channel.")


async def test_cancel_channel():
    """One call stops a whole channel, and the other channels keep running."""
    runner = Runner(delay=0.01, gated={"n0", "n1", "w0", "w1"})
    d = TurnDispatcher(runner, concurrency=2, max_queued=4)
    await d.start()
    try:
        busy = [asyncio.create_task(d.submit(new_job("noisy", "u", "ryan", f"n{i}", f"m{i}"),
                                             wait=True)) for i in range(2)]
        assert await wait_until(lambda: d.status()["running"] == 2), d.status()
        waiting = [asyncio.create_task(d.submit(new_job("noisy", "u", "ryan", f"w{i}", f"x{i}"),
                                                wait=True)) for i in range(2)]
        other = asyncio.create_task(d.submit(new_job("quiet", "u", "ryan", "q1", "mq"), wait=True))
        assert await wait_until(lambda: d.status()["queued"] == 3), d.status()

        stopped = d.cancel_channel("noisy")
        assert stopped == {"channel": "noisy", "queued": 2, "running": 2}, stopped
        outs = await asyncio.gather(*(busy + waiting))
        assert all(o["status"] == "cancelled" for o in outs), outs
        assert (await other)["reply"] == "echo:q1", "the other channel kept running"
        assert await wait_idle(d), d.status()
        assert d.status()["cancelled"] == 4, d.status()
        assert d.cancel_channel("nosuchchannel")["queued"] == 0
    finally:
        await d.stop()
    print("[PASS] cancel_channel stopped 2 waiting and 2 running turns, and only that channel.")


async def test_waiter_survives_retry():
    """A caller holding the request open must be answered after the backoff, and the worker
    must not be held during it."""
    runner = Runner(delay=0.01, fail_once={"flaky"})
    d = TurnDispatcher(runner, concurrency=1, max_queued=4, retries=1, failure_text="failed",
                       retry_delay=0.3)
    await d.start()
    try:
        waiting = asyncio.create_task(d.submit(new_job("c", "u", "ryan", "flaky", "m1"),
                                               wait=True))
        assert await wait_until(lambda: d.status()["retrying"] == 1), d.status()
        # The backoff must not hold the only worker: with one slot, a second channel's turn
        # has to run while the failed one waits.
        other = await d.submit(new_job("other", "u", "ryan", "during-backoff", "m2"), wait=True)
        assert other["reply"] == "echo:during-backoff", other
        assert d.status()["running"] == 0, d.status()

        outcome = await asyncio.wait_for(waiting, timeout=5.0)
        assert outcome["status"] == "done" and outcome["reply"] == "echo:flaky", outcome
        assert runner.seen["flaky"] == 2, runner.seen
        assert await wait_idle(d), d.status()
    finally:
        await d.stop()
    print("[PASS] A waiting caller was answered after the backoff, which held no worker.")


async def test_stop_during_backoff_keeps_job():
    """A restart landing inside a backoff must leave the job recoverable, not lose it."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        runner = Runner(delay=0.01, fail_once={"flaky"})
        delivered = []

        first = TurnDispatcher(runner, root_dir=root, concurrency=1, max_queued=4, retries=1,
                               retry_delay=30.0)
        await first.start()
        await first.submit(new_job("telegram", "42", "ryan", "flaky", "m1"),
                           collector(delivered))
        assert await wait_until(lambda: first.status()["retrying"] == 1), first.status()
        assert delivered == [], delivered
        await first.stop()  # the process dies mid-backoff

        recovered = []

        async def recover(job, reply):
            recovered.append((job.channel, job.reply_target(), reply))

        second = TurnDispatcher(Runner(delay=0.01), root_dir=root, concurrency=1, recover=recover)
        await second.start()
        try:
            assert await wait_until(lambda: recovered, 10), recovered
            assert await wait_idle(second), second.status()
            assert recovered == [("telegram", "42", "echo:flaky")], recovered
        finally:
            await second.stop()
    print("[PASS] A stop inside the backoff left the job unfinished on disk; the next process ran it.")


async def test_priority_respects_channel_limit():
    """Urgency may reorder the rotation, but it cannot push past a channel's own cap."""
    runner = Runner(delay=0.01, gated={"hold", "free-1"})
    d = TurnDispatcher(runner, concurrency=3, max_queued=8, channel_limits={"capped": 1})
    await d.start()
    try:
        hold = asyncio.create_task(d.submit(new_job("capped", "u", "ryan", "hold", "m0"),
                                            wait=True))
        assert await wait_until(lambda: d.status()["running"] == 1), d.status()
        urgent = asyncio.create_task(d.submit(new_job("capped", "u", "ryan", "urgent", "m1",
                                                      priority=9), wait=True))
        free = asyncio.create_task(d.submit(new_job("free", "u", "ryan", "free-1", "m2"),
                                            wait=True))

        # The capped channel is at its limit, so its priority-9 head cannot start; the other
        # channel's ordinary turn takes the free slot instead, and the cap is never breached.
        assert await wait_until(lambda: d.status()["running"] == 2), d.status()
        assert "urgent" not in [t for _, t in runner.order], runner.order
        assert runner.peak_by["capped"] == 1, runner.peak_by
        assert d.status()["channels"]["capped"]["limit"] == 1, d.status()

        runner.gate.set()
        assert (await free)["reply"] == "echo:free-1"
        assert (await hold)["reply"] == "echo:hold"
        # Only once the capped channel's slot frees does its urgent head run.
        assert (await urgent)["reply"] == "echo:urgent"
        order = [t for _, t in runner.order]
        assert order.index("urgent") > order.index("hold"), order
        assert runner.peak_by["capped"] == 1, runner.peak_by
    finally:
        await d.stop()
    print("[PASS] A priority-9 head waited behind its channel's cap; the cap held at 1.")


async def main():
    await test_fairness_and_caps()
    await test_backpressure()
    await test_cancel()
    await test_priority()
    await test_priority_across_channels()
    await test_priority_respects_channel_limit()
    await test_retry()
    await test_waiter_survives_retry()
    await test_stop_during_backoff_keeps_job()
    await test_cancel_channel()
    await test_restart_recovers()
    print("ALL GATEWAY DISPATCHER TESTS PASSED.")


if __name__ == "__main__":
    asyncio.run(main())
