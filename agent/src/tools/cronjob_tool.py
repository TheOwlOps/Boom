"""Scheduled prompts that really run: 5-field cron parser, a jobs ledger, and an async runner.

Replaces the interval-timer stand-in, which reported runs it never performed. A job here is a
prompt plus a cron expression; the runner fires it through the live agent loop
(`await loop.step(prompt)`) and keeps the outcome on the job.

A job whose prompt raised is retried `BOOM_CRON_RETRIES` times (default 0) before the failure
is kept on the job. Occurrences missed while the process was down can be replayed:
`BOOM_CRON_CATCHUP` (default 1) is how many of them a due job runs again on restart, 0 restoring
the plain "run once" behaviour. Schedule times are the host's local time: a job set in another
timezone needs the host set to it, because a cron expression carries no zone of its own.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("boom.cron")

JOBS_FILE = "cron_jobs.json"
DEFAULT_CATCHUP = 1     # missed occurrences replayed when a due job is met after downtime
DEFAULT_RETRIES = 0     # extra attempts for a prompt that raised
DEFAULT_RETRY_DELAY = 2.0
# A minute-level job down for a year has 500k missed occurrences; past this the rest are dropped.
MAX_MISSED = 100

_FIELD_NAMES = ("minute", "hour", "day_of_month", "month", "day_of_week")
_FIELD_BOUNDS = {
    "minute": (0, 59),
    "hour": (0, 23),
    "day_of_month": (1, 31),
    "month": (1, 12),
    "day_of_week": (0, 7),  # 0 and 7 are both Sunday, as in vixie cron
}
_MONTH_NAMES = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_DAY_NAMES = "sun mon tue wed thu fri sat".split()
_NAMES = {name: i + 1 for i, name in enumerate(_MONTH_NAMES)}
_NAMES.update({name: i for i, name in enumerate(_DAY_NAMES)})
_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
}
# A day-of-week field is scanned day by day, so a match this far out means the expression is
# unsatisfiable in practice (e.g. "0 0 30 2 *") and must be reported, not looped on.
_SEARCH_LIMIT_DAYS = 366 * 5

# ------------------------------------------------------------------ parser


def _expand(expr: str) -> str:
    text = (expr or "").strip().lower()
    return _ALIASES.get(text, text)


def _value(field: str, text: str) -> int:
    text = text.strip()
    if text in _NAMES:
        number = _NAMES[text]
    elif text.isdigit():
        number = int(text)
    else:
        raise ValueError(f"{text!r} is not a number or a known name in the {field} field")
    low, high = _FIELD_BOUNDS[field]
    if not low <= number <= high:
        raise ValueError(f"{field} value {number} is outside {low}-{high}")
    return number


def _parse_field(field: str, raw: str) -> Set[int]:
    """One field: '*', '*/n', 'a-b', 'a-b/n', a single value, or any comma list of those."""
    low, high = _FIELD_BOUNDS[field]
    values: Set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty entry in the {field} field {raw!r}")
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) == 0:
                raise ValueError(f"'/{step_text}' is not a step in the {field} field (want /1, /5, ...)")
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_text, _, end_text = part.partition("-")
            start, end = _value(field, start_text), _value(field, end_text)
        else:
            start = end = _value(field, part)
        if start > end:
            raise ValueError(f"{field} range {start}-{end} runs backwards")
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError(f"{field} field {raw!r} matches no value")
    return values


def parse_cron(expr: str) -> Dict[str, Set[int]]:
    """A 5-field expression as {field name: allowed values}. ValueError if it is malformed."""
    parts = _expand(expr).split()
    if len(parts) != 5:
        raise ValueError(
            f"cron expression needs 5 fields "
            f"(minute hour day-of-month month day-of-week), got {len(parts)}: {expr!r}"
        )
    fields = {name: _parse_field(name, raw) for name, raw in zip(_FIELD_NAMES, parts)}
    if 7 in fields["day_of_week"]:
        fields["day_of_week"].discard(7)
        fields["day_of_week"].add(0)
    return fields


def next_run(expr: str, after: datetime) -> datetime:
    """First minute matching `expr` strictly after `after`. ValueError if there is none.

    Naive datetimes in, naive out; an aware `after` keeps its tzinfo (wall-clock arithmetic,
    so an expression does not survive a DST jump exactly as vixie cron would).
    """
    fields = parse_cron(expr)
    parts = _expand(expr).split()
    # Vixie cron: when both day fields are restricted, a day matching either one runs.
    either_day = parts[2] != "*" and parts[4] != "*"

    def day_matches(when: datetime) -> bool:
        by_day = when.day in fields["day_of_month"]
        by_weekday = ((when.weekday() + 1) % 7) in fields["day_of_week"]
        return (by_day or by_weekday) if either_day else (by_day and by_weekday)

    candidate = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    limit = after + timedelta(days=_SEARCH_LIMIT_DAYS)
    while candidate <= limit:
        if candidate.month not in fields["month"]:
            year = candidate.year + 1 if candidate.month == 12 else candidate.year
            month = 1 if candidate.month == 12 else candidate.month + 1
            # Jump a whole month instead of walking its minutes (the Feb 29 case needs this).
            candidate = candidate.replace(year=year, month=month, day=1, hour=0, minute=0)
        elif not day_matches(candidate):
            candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
        elif candidate.hour not in fields["hour"]:
            candidate = (candidate + timedelta(hours=1)).replace(minute=0)
        elif candidate.minute not in fields["minute"]:
            candidate = candidate + timedelta(minutes=1)
        else:
            return candidate
    raise ValueError(f"no run time for {expr!r} in the 5 years after {after.isoformat()}")


# ------------------------------------------------------------------ store


def _jobs_path(root_dir) -> Path:
    """Where jobs live, beside the other memories/ state (self_supervision._champion_path)."""
    return Path(root_dir) / "memories" / JOBS_FILE


class CronStore:
    """Job ledger at memories/cron_jobs.json. An absent or corrupt file reads as no jobs."""

    def __init__(self, jobs_file: Optional[Path] = None, clock: Callable[[], datetime] = datetime.now):
        self.jobs_file = Path(jobs_file) if jobs_file else (Path.cwd() / "memories" / JOBS_FILE)
        self.clock = clock

    def list(self) -> List[Dict[str, Any]]:
        """Every job in ledger order. Never raises: an unreadable file is an empty ledger."""
        if not self.jobs_file.exists():
            return []
        try:
            raw = json.loads(self.jobs_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("cron ledger %s unreadable (%s); starting empty", self.jobs_file, e)
            return []
        if not isinstance(raw, list):
            return []
        return [j for j in raw if isinstance(j, dict) and j.get("id")]

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        return next((j for j in self.list() if j["id"] == job_id), None)

    def _save(self, jobs: List[Dict[str, Any]]) -> None:
        self.jobs_file.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the ledger and swap it in: a crash mid-write leaves the old file whole.
        tmp = self.jobs_file.with_name(self.jobs_file.name + ".tmp")
        tmp.write_text(json.dumps(jobs, indent=2), encoding="utf-8")
        os.replace(tmp, self.jobs_file)

    def add(self, cron: str, prompt: str, job_id: Optional[str] = None) -> Dict[str, Any]:
        """Append a job and return it. ValueError if the expression or the id is unusable."""
        parse_cron(cron)
        if not (prompt or "").strip():
            raise ValueError("a cron job needs a prompt to run")
        jobs = self.list()
        jid = job_id or f"cron_{int(self.clock().timestamp() * 1000)}"
        if any(j["id"] == jid for j in jobs):
            raise ValueError(f"a cron job with id {jid!r} already exists")
        job = {
            "id": jid,
            "cron": cron.strip(),
            "prompt": prompt,
            "created_at": self.clock().isoformat(timespec="seconds"),
            "last_run": None,
            "runs": 0,
            "enabled": True,
            "last_error": "",
        }
        jobs.append(job)
        self._save(jobs)
        return job

    def remove(self, job_id: str) -> bool:
        jobs = self.list()
        kept = [j for j in jobs if j["id"] != job_id]
        if len(kept) == len(jobs):
            return False
        self._save(kept)
        return True

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        jobs = self.list()
        for job in jobs:
            if job["id"] == job_id:
                job["enabled"] = bool(enabled)
                self._save(jobs)
                return True
        return False

    def record_run(self, job_id: str, when: datetime, error: str = "") -> Optional[Dict[str, Any]]:
        """Count a run, move last_run to `when`, keep its error. None if the job is gone."""
        jobs = self.list()
        for job in jobs:
            if job["id"] == job_id:
                job["last_run"] = when.isoformat(timespec="seconds")
                job["runs"] = int(job.get("runs") or 0) + 1
                job["last_error"] = error
                self._save(jobs)
                return job
        return None


# ------------------------------------------------------------------ runner


def _anchor(job: Dict[str, Any]) -> Optional[datetime]:
    """When this job was last considered: its last run, else when it was created."""
    for key in ("last_run", "created_at"):
        raw = job.get(key)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            continue
    return None


class CronRunner:
    """Fires due jobs through the agent loop. Clock and sleep are injected, so a test can call
    `run_due(now)` and never wait; `serve()` is the standing loop."""

    def __init__(
        self,
        store: CronStore,
        get_loop: Optional[Callable[[], Any]] = None,
        clock: Callable[[], datetime] = datetime.now,
        sleep: Callable[[float], Any] = asyncio.sleep,
        poll_seconds: float = 60.0,
        catchup: Optional[int] = None,
        retries: Optional[int] = None,
        retry_delay: Optional[float] = None,
    ):
        self.store = store
        self.get_loop = get_loop or (lambda: None)
        self.clock = clock
        self.sleep = sleep
        self.poll_seconds = poll_seconds
        self.catchup = int(os.getenv("BOOM_CRON_CATCHUP", DEFAULT_CATCHUP)) if catchup is None else catchup
        self.retries = int(os.getenv("BOOM_CRON_RETRIES", DEFAULT_RETRIES)) if retries is None else retries
        self.retry_delay = (float(os.getenv("BOOM_CRON_RETRY_DELAY", DEFAULT_RETRY_DELAY))
                            if retry_delay is None else retry_delay)

    def _due(self, job: Dict[str, Any], now: datetime) -> bool:
        anchor = _anchor(job)
        if anchor is None or anchor > now:
            return anchor is None  # never-run job with no anchor fires once; a future one waits
        try:
            return next_run(job["cron"], anchor) <= now
        except ValueError as e:
            logger.warning("cron job %s cannot be scheduled: %s", job.get("id"), e)
            return False

    def next_due(self, now: Optional[datetime] = None) -> Optional[datetime]:
        """Earliest fire time across enabled jobs, or None when nothing is scheduled."""
        now = now or self.clock()
        times = []
        for job in self.store.list():
            if not job.get("enabled", True):
                continue
            try:
                times.append(next_run(job["cron"], now))
            except ValueError as e:
                logger.warning("cron job %s cannot be scheduled: %s", job.get("id"), e)
        return min(times) if times else None

    def missed(self, job: Dict[str, Any], now: datetime) -> List[datetime]:
        """Occurrences between the job's last run and *now* — the ones nobody was up to run.

        A job that never ran has no downtime behind it: its first run is now, not the whole
        stretch since it was created.
        """
        if not job.get("last_run"):
            return []
        anchor = _anchor(job)
        if anchor is None:
            return []
        out: List[datetime] = []
        try:
            when = next_run(job["cron"], anchor)
            while when <= now and len(out) < MAX_MISSED:
                out.append(when)
                when = next_run(job["cron"], when)
        except ValueError as e:
            logger.warning("cron job %s cannot be scheduled: %s", job.get("id"), e)
        return out

    async def run_due(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Run every enabled job whose next fire time has passed, replaying recent misses."""
        now = now or self.clock()
        records = []
        for job in self.store.list():
            if not job.get("enabled", True) or not self._due(job, now):
                continue
            # One run is this occurrence; the rest of the missed window is replayed only up to
            # catchup, so a job offline for a month does not fire a month of prompts at once.
            missed = max(0, len(self.missed(job, now)) - 1)
            for _ in range(1 + min(max(0, self.catchup), missed)):
                record = await self.run_job(job["id"], now)
                if missed:
                    record["missed"] = missed
                records.append(record)
        return records

    async def run_job(self, job_id: str, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Run one job now, whatever its schedule says. A failing prompt is recorded, not raised."""
        job = self.store.get(job_id)
        if job is None:
            return {"error": f"no cron job {job_id!r}"}
        loop = self.get_loop()
        if loop is None:
            return {"error": "no agent loop bound: set env.loop (or get_loop=) before running a cron prompt"}
        now = now or self.clock()
        error = ""
        result: Any = None
        attempts = 0
        while True:
            attempts += 1
            try:
                result = await loop.step(job["prompt"])
                error = ""
                break
            except Exception as e:  # one bad prompt must not stop the schedule
                error = f"{type(e).__name__}: {e}"
                logger.exception("cron job %s failed (attempt %d)", job_id, attempts)
                if attempts > self.retries:
                    break
                await self.sleep(self.retry_delay * (2 ** (attempts - 1)))
        self.store.record_run(job_id, now, error)
        record = {
            "id": job_id,
            "cron": job["cron"],
            "prompt": job["prompt"],
            "ran_at": now.isoformat(timespec="seconds"),
            "attempts": attempts,
            "error": error,
        }
        if not error:
            record["result"] = result
        return record

    async def serve(self):
        """Sleep until the next due job, run what is due, repeat."""
        while True:
            now = self.clock()
            await self.run_due(now)
            nxt = self.next_due(now)
            delay = self.poll_seconds if nxt is None else max(1.0, (nxt - now).total_seconds())
            await self.sleep(delay)


# ------------------------------------------------------------------ tools


def _schema(properties: Dict[str, Any] = None, required: List[str] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or []}


def build_tools(env) -> Dict[str, Dict[str, Any]]:
    """Tool dicts for the environment: cron_add, cron_list, cron_remove, cron_run.

    Jobs live at <env.root_dir>/memories/cron_jobs.json. Prompts run through `env.loop`
    (the ReAct loop); a missing loop is reported by cron_run, never faked.
    """
    root_dir = getattr(env, "root_dir", None)
    store = CronStore(_jobs_path(root_dir)) if root_dir is not None else None
    runner = CronRunner(store, get_loop=lambda: getattr(env, "loop", None)) if store else None

    def _add(cron: str, prompt: str, job_id: Optional[str] = None) -> Dict[str, Any]:
        if store is None:
            return {"error": "environment has no root_dir: nowhere to keep cron jobs"}
        try:
            return {"job": store.add(cron, prompt, job_id)}
        except ValueError as e:
            return {"error": str(e)}
        except OSError as e:
            return {"error": f"could not write the cron ledger: {e}"}

    def _list() -> Dict[str, Any]:
        if store is None:
            return {"error": "environment has no root_dir: nowhere to keep cron jobs"}
        jobs = store.list()
        return {"jobs": jobs, "total": len(jobs)}

    def _remove(job_id: str) -> Dict[str, Any]:
        if store is None:
            return {"error": "environment has no root_dir: nowhere to keep cron jobs"}
        if not store.remove(job_id):
            return {"error": f"no cron job {job_id!r}"}
        return {"removed": job_id}

    async def _run(job_id: str) -> Dict[str, Any]:
        if runner is None:
            return {"error": "environment has no root_dir: nowhere to keep cron jobs"}
        return await runner.run_job(job_id)

    return {
        "cron_add": {
            "name": "cron_add",
            "func": _add,
            "description": (
                "Schedule a prompt to run on a 5-field cron expression "
                "(minute hour day-of-month month day-of-week, '*' '*/n' 'a-b' 'a-b/n' and comma "
                "lists; @hourly/@daily/@weekly/@monthly/@yearly also work). Jobs persist in "
                "memories/cron_jobs.json and fire while the runner is up."
            ),
            "schema": _schema(
                {
                    "cron": {"type": "string", "description": "Cron expression, e.g. '0 9 * * 1-5'."},
                    "prompt": {"type": "string", "description": "Prompt to send to the agent when it fires."},
                    "job_id": {"type": "string", "description": "Optional id; generated if omitted."},
                },
                ["cron", "prompt"],
            ),
        },
        "cron_list": {
            "name": "cron_list",
            "func": _list,
            "description": "List scheduled cron jobs with their expression, last run, run count and state.",
            "schema": _schema(),
        },
        "cron_remove": {
            "name": "cron_remove",
            "func": _remove,
            "description": "Delete a scheduled cron job by id. Returns an error if there is no such job.",
            "schema": _schema(
                {"job_id": {"type": "string", "description": "Job id from cron_list."}},
                ["job_id"],
            ),
        },
        "cron_run": {
            "name": "cron_run",
            "func": _run,
            "description": (
                "Run one scheduled job now through the agent loop, whatever its schedule says, "
                "and return the run's result. The run is counted on the job like a scheduled one."
            ),
            "schema": _schema(
                {"job_id": {"type": "string", "description": "Job id from cron_list."}},
                ["job_id"],
            ),
        },
    }
