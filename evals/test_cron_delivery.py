"""Cron delivery: a job with deliver_to pushes its result through the bound deliver hook.

Covers: delivery on success (channel/target/text), no delivery without deliver_to,
delivery failure not failing the job, deliver_to validation, cron_add passthrough,
and backward compatibility with jobs written before deliver_to existed.
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from tools.cronjob_tool import CronRunner, CronStore, build_tools

T0 = datetime(2026, 10, 15, 10, 0, 0)
LATER = datetime(2026, 10, 15, 10, 2, 0)  # past the next minute tick


class FakeLoop:
    def __init__(self, result=None):
        self.result = result if result is not None else {"action": "Bao cao: xong", "steps": 2}
        self.prompts = []

    async def step(self, prompt):
        self.prompts.append(prompt)
        return self.result


async def no_sleep(delay):
    pass


def make_store(tmpdir, clock=lambda: T0):
    return CronStore(Path(tmpdir) / "cron_jobs.json", clock=clock)


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(f"FAILED: {name}")


def test_delivery_on_success(tmpdir):
    store = make_store(tmpdir)
    store.add("* * * * *", "lam gi do", job_id="j1",
              deliver_to={"channel": "telegram", "target": "123"})
    calls = []

    async def deliver(channel, target, text):
        calls.append((channel, target, text))
        return True

    runner = CronRunner(store, get_loop=lambda: FakeLoop(), clock=lambda: T0,
                        sleep=no_sleep, deliver=deliver)
    records = asyncio.run(runner.run_due(LATER))
    check("one record", len(records) == 1)
    check("delivered True", records[0].get("delivered") is True)
    check("deliver called once", len(calls) == 1)
    check("channel", calls[0][0] == "telegram")
    check("target", calls[0][1] == "123")
    check("text from action", calls[0][2] == "Bao cao: xong")


def test_no_deliver_without_deliver_to(tmpdir):
    store = make_store(tmpdir)
    store.add("* * * * *", "lam gi do", job_id="j2")
    calls = []

    async def deliver(channel, target, text):
        calls.append((channel, target, text))
        return True

    runner = CronRunner(store, get_loop=lambda: FakeLoop(), clock=lambda: T0,
                        sleep=no_sleep, deliver=deliver)
    records = asyncio.run(runner.run_due(LATER))
    check("one record", len(records) == 1)
    check("delivered None", records[0].get("delivered") is None)
    check("deliver not called", calls == [])


def test_delivery_failure_does_not_fail_job(tmpdir):
    store = make_store(tmpdir)
    store.add("* * * * *", "lam gi do", job_id="j3",
              deliver_to={"channel": "telegram", "target": "123"})

    async def boom(channel, target, text):
        raise RuntimeError("network down")

    runner = CronRunner(store, get_loop=lambda: FakeLoop(), clock=lambda: T0,
                        sleep=no_sleep, deliver=boom)
    records = asyncio.run(runner.run_due(LATER))
    check("record kept", len(records) == 1 and not records[0]["error"])
    check("delivered False", records[0].get("delivered") is False)
    check("job counted", store.get("j3")["runs"] == 1)


def test_no_hook_bound_logs_and_skips(tmpdir):
    store = make_store(tmpdir)
    store.add("* * * * *", "lam gi do", job_id="j4",
              deliver_to={"channel": "telegram", "target": "123"})
    runner = CronRunner(store, get_loop=lambda: FakeLoop(), clock=lambda: T0,
                        sleep=no_sleep, deliver=None)
    records = asyncio.run(runner.run_due(LATER))
    check("delivered None without hook", records[0].get("delivered") is None)


def test_deliver_to_validation(tmpdir):
    store = make_store(tmpdir)
    for bad in ({"channel": "telegram"}, {"target": "123"},
                {"channel": "", "target": "123"}, "telegram"):
        try:
            store.add("* * * * *", "x", deliver_to=bad)
        except ValueError:
            continue
        raise SystemExit(f"FAILED: validation accepted {bad!r}")
    print("PASS deliver_to validation rejects bad mappings")
    job = store.add("* * * * *", "x", job_id="j5",
                    deliver_to={"channel": " telegram ", "target": " 123 "})
    check("deliver_to normalised", job["deliver_to"] == {"channel": "telegram", "target": "123"})


def test_cron_add_passthrough(tmpdir):
    class Env:
        root_dir = Path(tmpdir)
        loop = FakeLoop()

    tools = build_tools(Env())
    res = tools["cron_add"]["func"]("0 9 * * *", "bao cao sang",
                                    deliver_to={"channel": "telegram", "target": "123"})
    check("cron_add ok", "job" in res)
    check("cron_add keeps deliver_to",
          res["job"]["deliver_to"] == {"channel": "telegram", "target": "123"})


def test_backward_compat_job_without_key(tmpdir):
    store = make_store(tmpdir)
    # Simulate a job written before deliver_to existed.
    import json
    p = Path(tmpdir) / "cron_jobs.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([{
        "id": "old", "cron": "* * * * *", "prompt": "x",
        "created_at": T0.isoformat(), "last_run": None, "runs": 0,
        "enabled": True, "last_error": "",
    }]), encoding="utf-8")
    calls = []

    async def deliver(channel, target, text):
        calls.append((channel, target, text))
        return True

    runner = CronRunner(store, get_loop=lambda: FakeLoop(), clock=lambda: T0,
                        sleep=no_sleep, deliver=deliver)
    records = asyncio.run(runner.run_due(LATER))
    check("old job runs", len(records) == 1 and not records[0]["error"])
    check("old job not delivered", calls == [] and records[0].get("delivered") is None)


def test_result_text_variants(tmpdir):
    from tools.cronjob_tool import _result_text
    check("action picked", _result_text({"action": "a", "observation": "o"}) == "a")
    check("observation fallback", _result_text({"observation": "o"}) == "o")
    check("plain string", _result_text("hi") == "hi")
    check("none -> Done.", _result_text(None) == "Done.")
    check("empty dict -> Done.", _result_text({}) == "Done.")


if __name__ == "__main__":
    import tempfile
    print("== cron delivery ==")
    for fn in (test_delivery_on_success, test_no_deliver_without_deliver_to,
               test_delivery_failure_does_not_fail_job, test_no_hook_bound_logs_and_skips,
               test_deliver_to_validation, test_cron_add_passthrough,
               test_backward_compat_job_without_key, test_result_text_variants):
        with tempfile.TemporaryDirectory() as tmp:
            fn(tmp)
    print("ALL CRON DELIVERY TESTS PASSED")
