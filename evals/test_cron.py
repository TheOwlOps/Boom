"""Offline, deterministic tests for the cron tool: parser table, ledger, runner, tool dicts.

Nothing here sleeps and nothing calls a model: the runner's clock is injected and its loop is
a stub with an async step(), so run_due(now) is driven directly.
"""

import asyncio
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from tools.cronjob_tool import JOBS_FILE, CronRunner, CronStore, build_tools, next_run, parse_cron


class StubLoop:
    """Stands in for the ReAct loop: records the prompts it is given."""

    def __init__(self):
        self.prompts = []
        self.fail_on = set()
        self.fail_once = set()  # raises on the first call only, so a retry can recover

    async def step(self, prompt):
        self.prompts.append(prompt)
        if prompt in self.fail_on or (prompt in self.fail_once
                                      and self.prompts.count(prompt) == 1):
            raise RuntimeError("boom")
        return {"action": f"ran {prompt}", "success": True}


def _tmpdir():
    return Path(tempfile.mkdtemp(prefix="cron_eval_"))


def test_parser_and_next_run():
    # every minute, */5, and the strict "after" rule
    assert next_run("* * * * *", datetime(2024, 3, 1, 10, 30)) == datetime(2024, 3, 1, 10, 31)
    assert next_run("*/5 * * * *", datetime(2024, 3, 1, 10, 31)) == datetime(2024, 3, 1, 10, 35)
    assert next_run("*/5 * * * *", datetime(2024, 3, 1, 10, 30)) == datetime(2024, 3, 1, 10, 35)
    assert next_run("*/15 2 1,15 * *", datetime(2024, 3, 1, 2, 14)) == datetime(2024, 3, 1, 2, 15)

    # 09:00 Mon-Fri: Friday -> Monday, and Saturday -> Monday (weekend skipped)
    assert datetime(2024, 3, 1).weekday() == 4 and datetime(2024, 3, 4).weekday() == 0
    assert next_run("0 9 * * 1-5", datetime(2024, 3, 1, 9, 0)) == datetime(2024, 3, 4, 9, 0)
    assert next_run("0 9 * * 1-5", datetime(2024, 3, 2, 9, 0)) == datetime(2024, 3, 4, 9, 0)
    assert next_run("0 9 * * 1-5", datetime(2024, 3, 2, 8, 59)) == datetime(2024, 3, 4, 9, 0)

    # month rollover, including the year boundary
    assert next_run("0 0 1 * *", datetime(2024, 3, 15, 12, 0)) == datetime(2024, 4, 1, 0, 0)
    assert next_run("0 0 1 * *", datetime(2024, 12, 31, 23, 59)) == datetime(2025, 1, 1, 0, 0)
    assert next_run("0 0 1 * *", datetime(2024, 12, 15, 0, 0)) == datetime(2025, 1, 1, 0, 0)

    # leap year Feb 29: this year's, then the next one four years out
    assert next_run("0 0 29 2 *", datetime(2024, 2, 28, 23, 0)) == datetime(2024, 2, 29, 0, 0)
    assert next_run("0 0 29 2 *", datetime(2024, 3, 1, 0, 0)) == datetime(2028, 2, 29, 0, 0)
    assert next_run("0 0 29 2 *", datetime(2025, 1, 1, 0, 0)) == datetime(2028, 2, 29, 0, 0)

    # names, ranges with steps, and aliases
    assert next_run("0 9 * jan mon", datetime(2024, 1, 2, 0, 0)) == datetime(2024, 1, 8, 9, 0)
    assert next_run("0 8-17/4 * * *", datetime(2024, 3, 1, 9, 0)) == datetime(2024, 3, 1, 12, 0)
    assert next_run("@hourly", datetime(2024, 3, 1, 10, 30)) == datetime(2024, 3, 1, 11, 0)
    assert next_run("0 0 * * 7", datetime(2024, 3, 1, 0, 0)) == datetime(2024, 3, 3, 0, 0)

    # fields in the shape next_run works from
    fields = parse_cron("*/15 0 1,15 * 1-5")
    assert fields["minute"] == {0, 15, 30, 45}
    assert fields["hour"] == {0} and fields["day_of_month"] == {1, 15}
    assert fields["day_of_week"] == {1, 2, 3, 4, 5}
    assert parse_cron("0 0 * * 7")["day_of_week"] == {0}
    print("[PASS] Cron parser and next_run table verified.")

    # an unsupported or malformed expression raises rather than guessing a time
    for bad in ("", "not a cron", "* * * *", "* * * * * *", "60 * * * *", "*/0 * * * *",
                "* * 32 * *", "* * * 13 *", "* * * * 8", "* 9-1 * * *", "a-b * * * *"):
        try:
            next_run(bad, datetime(2024, 3, 1, 0, 0))
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should have raised ValueError")
    print("[PASS] Invalid expressions raise ValueError.")


def test_store_round_trip():
    tmp = _tmpdir()
    try:
        path = tmp / "memories" / JOBS_FILE
        store = CronStore(path, clock=lambda: datetime(2024, 3, 1, 10, 0))
        assert store.list() == []  # nothing on disk yet

        job = store.add("*/5 * * * *", "say hi")
        assert path.exists() and job["id"].startswith("cron_")
        assert job["cron"] == "*/5 * * * *" and job["prompt"] == "say hi"
        assert job["runs"] == 0 and job["last_run"] is None and job["enabled"] is True
        assert job["created_at"] == "2024-03-01T10:00:00"

        assert store.add("0 9 * * 1-5", "standup", job_id="standup")["id"] == "standup"
        assert [j["id"] for j in store.list()] == [job["id"], "standup"]
        assert store.get("standup")["cron"] == "0 9 * * 1-5"
        assert store.get("nope") is None

        assert store.remove("standup") is True
        assert store.remove("standup") is False
        assert [j["id"] for j in store.list()] == [job["id"]]
        assert store.set_enabled(job["id"], False) is True
        assert store.get(job["id"])["enabled"] is False
        assert store.set_enabled("nope", True) is False

        # a bad expression (or a taken id) never reaches the ledger
        for cron, jid in (("nope", None), ("* * * * *", job["id"])):
            try:
                store.add(cron, "x", job_id=jid)
            except ValueError:
                continue
            raise AssertionError(f"{cron!r} should not have been stored")
        assert len(store.list()) == 1
        print("[PASS] Cron store round trip: add, list, remove, no bad writes.")

        # a corrupt ledger reads as empty instead of raising, and stays writable
        path.write_text("{not json at all", encoding="utf-8")
        assert store.list() == []
        path.write_text('{"jobs": {}}', encoding="utf-8")  # valid JSON, wrong shape
        assert store.list() == []
        path.write_text("[1, 2, 3]", encoding="utf-8")  # right shape, wrong entries
        assert store.list() == []
        store.add("* * * * *", "after corruption", job_id="later")
        assert [j["id"] for j in store.list()] == ["later"]
        print("[PASS] Corrupt cron ledger starts empty and stays writable.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_runner():
    tmp = _tmpdir()
    try:
        path = tmp / "memories" / JOBS_FILE
        store = CronStore(path, clock=lambda: datetime(2024, 3, 1, 8, 0))
        loop = StubLoop()
        runner = CronRunner(store, get_loop=lambda: loop)

        store.add("*/5 * * * *", "explode", job_id="boom")
        store.add("*/5 * * * *", "say hi", job_id="frequent")
        store.add("0 12 * * *", "noon report", job_id="noon")
        store.add("* * * * *", "muted", job_id="muted")
        store.set_enabled("muted", False)

        now = datetime(2024, 3, 1, 9, 0)
        records = await runner.run_due(now)
        assert [r["id"] for r in records] == ["boom", "frequent"], records
        assert loop.prompts == ["explode", "say hi"], loop.prompts  # the prompt reached the loop
        assert records[0]["result"]["success"] is True
        assert store.get("frequent")["runs"] == 1
        assert store.get("frequent")["last_run"] == now.isoformat(timespec="seconds")
        print("[PASS] run_due runs exactly the due jobs and passes their prompts to the loop.")

        # last_run advanced, so the same minute cannot fire twice
        assert await runner.run_due(now) == []
        assert loop.prompts == ["explode", "say hi"]
        assert store.get("noon")["runs"] == 0 and store.get("muted")["runs"] == 0
        assert await runner.run_due(datetime(2024, 3, 1, 9, 5)) != []
        assert store.get("frequent")["runs"] == 2
        print("[PASS] A job does not fire twice for one minute; the next slot does fire.")

        # a failing prompt is recorded on the job and does not stop the jobs behind it
        loop.fail_on.add("explode")
        records = await runner.run_due(datetime(2024, 3, 1, 9, 10))
        assert [r["id"] for r in records] == ["boom", "frequent"], records
        boom = store.get("boom")
        assert boom["runs"] == 3 and "RuntimeError: boom" in boom["last_error"], boom
        assert records[0]["error"].startswith("RuntimeError")
        assert store.get("frequent")["runs"] == 3 and store.get("frequent")["last_error"] == ""
        print("[PASS] A job that raises is recorded and the remaining jobs still run.")

        # no loop bound is an error, not a fake run
        bare = CronRunner(store)
        res = await bare.run_job("frequent", datetime(2024, 3, 1, 9, 15))
        assert "error" in res and store.get("frequent")["runs"] == 3, res
        assert "error" in await bare.run_job("ghost")
        # "muted" would be next at 09:01 and "noon" at 12:00; only the enabled every-5 job counts
        assert runner.next_due(now) == datetime(2024, 3, 1, 9, 5), runner.next_due(now)
        print("[PASS] No loop bound reports an error and records no run.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_catchup_and_retry():
    """Downtime is replayed once, and a prompt that raises is retried before it is called failed."""
    tmp = _tmpdir()
    try:
        store = CronStore(tmp / "memories" / JOBS_FILE, clock=lambda: datetime(2024, 3, 1, 9, 0))
        loop = StubLoop()
        runner = CronRunner(store, get_loop=lambda: loop)
        store.add("*/5 * * * *", "tick", job_id="tick")
        await runner.run_due(datetime(2024, 3, 1, 9, 5))
        assert store.get("tick")["runs"] == 1

        # Down from 09:05 to 09:20: three occurrences passed, one of them is replayed.
        records = await runner.run_due(datetime(2024, 3, 1, 9, 20))
        assert len(records) == 2 and records[0]["missed"] == 2, records
        assert store.get("tick")["runs"] == 3, store.get("tick")
        print("[PASS] Cron downtime replayed one missed occurrence, not the whole window.")

        # catchup 0 is the old behaviour: run this occurrence and move on.
        plain_store = CronStore(tmp / "plain" / JOBS_FILE, clock=lambda: datetime(2024, 3, 1, 9, 0))
        plain = CronRunner(plain_store, get_loop=lambda: loop, catchup=0)
        plain_store.add("*/5 * * * *", "tick", job_id="tick")
        await plain.run_due(datetime(2024, 3, 1, 9, 5))
        records = await plain.run_due(datetime(2024, 3, 1, 9, 20))
        # The record still says how many were missed — it is what happened — but only one run.
        assert len(records) == 1 and records[0]["missed"] == 2, records
        assert plain_store.get("tick")["runs"] == 2
        print("[PASS] BOOM_CRON_CATCHUP=0 keeps the run-once behaviour.")

        # A prompt that raises is retried, with a doubling delay, before the failure is kept.
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        retrying = CronRunner(plain_store, get_loop=lambda: loop, retries=1, retry_delay=0.5,
                              sleep=fake_sleep)
        plain_store.add("0 12 * * *", "flaky", job_id="flaky")
        loop.fail_once.add("flaky")
        res = await retrying.run_job("flaky", datetime(2024, 3, 1, 12, 0))
        assert res["attempts"] == 2 and res["error"] == "", res
        assert slept == [0.5], slept

        plain_store.add("0 12 * * *", "broken", job_id="broken")
        loop.fail_on.add("broken")
        res = await retrying.run_job("broken", datetime(2024, 3, 1, 12, 0))
        assert res["attempts"] == 2 and res["error"].startswith("RuntimeError"), res
        assert slept == [0.5, 0.5], slept
        assert "RuntimeError: boom" in plain_store.get("broken")["last_error"]
        print("[PASS] Cron retried a failing prompt once, then recorded the failure.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_missed_window_is_bounded():
    """A long outage costs a bounded number of runs, and a future last_run is not downtime."""
    tmp = _tmpdir()
    try:
        store = CronStore(tmp / "memories" / JOBS_FILE, clock=lambda: datetime(2024, 3, 1, 8, 0))
        loop = StubLoop()
        runner = CronRunner(store, get_loop=lambda: loop, catchup=1)

        # A minutely job last seen ten days ago missed ~14400 occurrences. The window is scanned
        # up to MAX_MISSED (100) and only `catchup` of the misses are replayed, so the outage
        # costs 2 prompts, not ten days of them.
        store.add("* * * * *", "minutely", job_id="minutely")
        store.record_run("minutely", datetime(2024, 2, 20, 0, 0))
        ran_before = store.get("minutely")["runs"]
        now = datetime(2024, 3, 1, 9, 0)
        missed = runner.missed(store.get("minutely"), now)
        assert len(missed) == 100, len(missed)  # MAX_MISSED, not the real 14400
        assert missed[0] == datetime(2024, 2, 20, 0, 1), missed[0]

        records = await runner.run_due(now)
        assert [r["id"] for r in records] == ["minutely", "minutely"], records
        assert loop.prompts == ["minutely", "minutely"], loop.prompts
        assert records[0]["missed"] == 99, records[0]
        assert store.get("minutely")["runs"] == ran_before + 2, store.get("minutely")
        print("[PASS] Ten days of downtime replayed 2 prompts, the window capped at 100.")

        # A job that never ran has no downtime behind it, however old the schedule is.
        assert runner.missed({"cron": "* * * * *", "last_run": None}, now) == []

        # A last_run in the future (a clock that moved back) is not downtime either: the job is
        # simply not due yet, and must not be fired to "catch up" to a time that has not come.
        store.add("*/5 * * * *", "skewed", job_id="skewed")
        future = datetime(2024, 3, 3, 9, 0)
        store.record_run("skewed", future)
        assert runner.missed(store.get("skewed"), now) == []
        assert await runner.run_due(now) == []
        assert store.get("skewed")["last_run"] == future.isoformat(timespec="seconds")
        assert store.get("skewed")["runs"] == 1, store.get("skewed")
        print("[PASS] A future last_run waited; a never-run job replayed nothing.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_build_tools():
    tmp = _tmpdir()
    try:
        loop = StubLoop()
        env = SimpleNamespace(root_dir=tmp, loop=loop)
        tools = build_tools(env)
        assert set(tools) == {"cron_add", "cron_list", "cron_remove", "cron_run"}, set(tools)
        for name, tool in tools.items():
            assert tool["name"] == name and callable(tool["func"])
            assert isinstance(tool["description"], str) and tool["description"]
            schema = tool["schema"]
            assert schema["type"] == "object"
            assert isinstance(schema["properties"], dict) and isinstance(schema["required"], list)
        assert tools["cron_add"]["schema"]["required"] == ["cron", "prompt"]

        added = tools["cron_add"]["func"](cron="*/5 * * * *", prompt="ping")
        assert added["job"]["id"] and added["job"]["runs"] == 0, added
        listed = tools["cron_list"]["func"]()
        assert listed["total"] == 1 and listed["jobs"][0]["prompt"] == "ping"

        bad = tools["cron_add"]["func"](cron="nope", prompt="ping")
        assert "error" in bad and tools["cron_list"]["func"]()["total"] == 1
        assert "error" in tools["cron_add"]["func"](cron="* * * * *", prompt="   ")
        print("[PASS] build_tools shapes match the environment and a bad add writes nothing.")

        ran = await tools["cron_run"]["func"](job_id=added["job"]["id"])
        assert ran["id"] == added["job"]["id"] and loop.prompts == ["ping"], ran
        assert tools["cron_list"]["func"]()["jobs"][0]["runs"] == 1
        assert "error" in await tools["cron_run"]["func"](job_id="ghost")
        assert "error" in tools["cron_remove"]["func"](job_id="ghost")
        assert tools["cron_remove"]["func"](job_id=added["job"]["id"])["removed"] == added["job"]["id"]
        assert tools["cron_list"]["func"]()["total"] == 0
        print("[PASS] cron_run drives the loop and the tool layer never reports a fake run.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def main():
    test_parser_and_next_run()
    test_store_round_trip()
    await test_runner()
    await test_catchup_and_retry()
    await test_missed_window_is_bounded()
    await test_build_tools()
    print("ALL CRON TESTS PASSED.")


if __name__ == "__main__":
    asyncio.run(main())
