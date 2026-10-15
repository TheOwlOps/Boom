"""Wire triggers/ into cron: real ctx, a periodic check job, registration.

What this module does
---------------------
The triggers/ module (models, store, checkers, runner, tools) can evaluate
event triggers, but nothing calls it on a schedule. This module closes that
gap:

- ``build_ctx(env)`` — builds the real callable ctx that ``run_checks`` needs
  (gmail_search, news_search, get_price), from the repo's connectors.
- ``check_triggers_job(env)`` — one full check cycle: load store, build ctx,
  run checks, return the report. Never raises.
- ``build_check_tool(env)`` — exposes ``check_triggers_job`` as the agent tool
  ``triggers_check_now`` (repo tool-dict convention).
- ``register_trigger_cron(env)`` — idempotently creates the 5-minute cron job
  (via tools/cronjob_tool) whose prompt tells the agent to call
  ``triggers_check_now``.

What the user must configure for real operation
----------------------------------------------
- Gmail triggers (``gmail_new``): Gmail OAuth2 credentials. Without them
  ``gmail_search`` returns no messages (safe empty), so the trigger simply
  never fires. Set up via ``boom setup`` or the Gmail connector's token cache
  (``~/.boom/gmail_token.json``).
- News triggers (``keyword_news``): no credentials needed (VnExpress RSS).
- Price triggers (``price_below``): no credentials needed (Tiki public API).
  Only Tiki product URLs/ids are supported by ``get_price``.
- Notify actions: ``env`` must carry a ``daemon`` with an async
  ``send_to(channel, target, text)`` (the gateway daemon), and the channel
  itself must be configured (e.g. ``TELEGRAM_BOT_TOKEN`` for Telegram).
- The cron job's prompt only works if the ``triggers_check_now`` tool is
  wired into the agent environment (add ``build_check_tool(env)`` to the
  tool registry next to the other ``*.tools`` modules).

Only stdlib is imported at module load; connectors are imported lazily
inside the ctx callables so this module loads (and tests run) without them.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from triggers.runner import run_checks
from triggers.store import TriggerStore

logger = logging.getLogger("boom.triggers.cron")

#: Stable id of the periodic job created by register_trigger_cron.
TRIGGER_CRON_ID = "triggers_periodic_check"
#: Every 5 minutes, in the 5-field cron dialect of tools/cronjob_tool.
TRIGGER_CRON_EXPR = "*/5 * * * *"
#: What the scheduled job asks the agent to do.
TRIGGER_CRON_PROMPT = (
    "Run the event-trigger checker now by calling the triggers_check_now "
    "tool, then summarize how many triggers fired and were notified."
)


# ------------------------------------------------------------------ helpers


def _run_coro_sync(coro):
    """Run a coroutine from synchronous code.

    Works whether or not the calling thread already runs an event loop: when
    a loop is running here, the coroutine executes on a helper thread with
    its own loop (avoiding deadlock); otherwise ``asyncio.run`` is used.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _get_gmail_connector():
    """Return the Gmail connector instance. Imported lazily for testability."""
    from connectors.gmail import CONNECTOR

    return CONNECTOR


def _get_news_connector():
    """Return the news connector instance. Imported lazily for testability."""
    from connectors.news import CONNECTOR

    return CONNECTOR


def _get_tiki_connector():
    """Return the Tiki connector instance. Imported lazily for testability."""
    from connectors.tiki import CONNECTOR

    return CONNECTOR


def _make_notify_fn(env) -> Callable[[str, str, str], None]:
    """Build the sync ``notify_fn(channel, target, text)`` for ``run_checks``.

    Bridges to the async ``env.daemon.send_to``. Raises on failure — the
    runner catches it, records the error, and does NOT mark the trigger
    fired, so delivery is retried on the next cycle.
    """
    daemon = getattr(env, "daemon", None)
    send_to = getattr(daemon, "send_to", None) if daemon is not None else None

    def notify(channel: str, target: str, text: str) -> None:
        if not callable(send_to):
            raise RuntimeError(
                "notify: env.daemon.send_to is not available; "
                "bind the gateway daemon to env.daemon"
            )
        ok = _run_coro_sync(send_to(str(channel), str(target), str(text)))
        if not ok:
            raise RuntimeError(
                "notify: send_to(%r, %r) reported failure" % (channel, target)
            )

    return notify


# ------------------------------------------------------------------ ctx


def build_ctx(env) -> Dict[str, Callable]:
    """Build the real callable ctx for ``triggers.runner.run_checks``.

    Every callable is wrapped in try/except and never raises: on any error
    it returns a safe empty value (``[]`` for searches, ``None`` for price),
    so a broken connector can only suppress a trigger, never crash a cycle
    or fake a firing.
    """
    _ = env  # reserved: per-env connector configuration goes here.

    def gmail_search(query: str):
        try:
            res = _get_gmail_connector().gmail_search(query)
        except Exception as exc:
            logger.warning("trigger gmail_search failed: %s", exc)
            return []
        if isinstance(res, dict) and "error" in res:
            logger.warning("trigger gmail_search error: %s", res["error"])
            return []
        return res

    def news_search(keyword: str):
        try:
            res = _get_news_connector().news_search(keyword)
        except Exception as exc:
            logger.warning("trigger news_search failed: %s", exc)
            return []
        if isinstance(res, dict) and "error" in res:
            logger.warning("trigger news_search error: %s", res["error"])
            return []
        return res

    def get_price(url: str):
        try:
            res = _get_tiki_connector().tiki_product(url)
        except Exception as exc:
            logger.warning("trigger get_price failed: %s", exc)
            return None
        if not isinstance(res, dict) or "error" in res:
            return None
        try:
            return float(res.get("price"))
        except (TypeError, ValueError):
            return None

    return {
        "gmail_search": gmail_search,
        "news_search": news_search,
        "get_price": get_price,
    }


# ------------------------------------------------------------------ job


def _resolve_root(env) -> Optional[Path]:
    root = getattr(env, "root_dir", None)
    try:
        return Path(root) if root is not None else None
    except (TypeError, ValueError):
        return None


def _empty_report(error: str) -> Dict[str, Any]:
    return {
        "checked": 0,
        "fired": [],
        "notified": [],
        "skipped_cooldown": [],
        "errors": [error],
    }


def check_triggers_job(env) -> Dict[str, Any]:
    """Run one full trigger-check cycle. Never raises.

    Loads the trigger store from ``<root_dir>/memories/triggers.db``, builds
    the real ctx, and runs ``run_checks``. Returns the runner's report dict.
    """
    try:
        root_dir = _resolve_root(env)
        if root_dir is None:
            return _empty_report(
                "check_triggers_job: env has no root_dir"
            )
        store = TriggerStore(root_dir / "memories" / "triggers.db")
        ctx = build_ctx(env)
        notify_fn = _make_notify_fn(env)
        return run_checks(store, ctx, notify_fn)
    except Exception as exc:  # never raise out of a scheduled job
        logger.exception("check_triggers_job failed")
        return _empty_report("check_triggers_job failed: %s" % (exc,))


def build_check_tool(env) -> Dict[str, Dict[str, Any]]:
    """Expose ``check_triggers_job`` as the agent tool ``triggers_check_now``.

    Wire this into the environment's tool registry (next to the other
    ``*.tools`` modules) so the periodic cron job's prompt can invoke it.
    """
    def _check_now() -> Dict[str, Any]:
        return check_triggers_job(env)

    return {
        "triggers_check_now": {
            "name": "triggers_check_now",
            "func": _check_now,
            "description": (
                "Run one event-trigger check cycle now: evaluate every "
                "enabled trigger (gmail_new, price_below, keyword_news, "
                "webhook) and run their actions. Returns the run report "
                "{checked, fired, notified, skipped_cooldown, errors}."
            ),
            "schema": {"type": "object", "properties": {}, "required": []},
        }
    }


def register_trigger_cron(env) -> Dict[str, Any]:
    """Idempotently register the 5-minute trigger-check cron job.

    Uses the existing cron API (tools/cronjob_tool). If the job already
    exists it is returned untouched with ``already_existed: True``.
    """
    from tools.cronjob_tool import build_tools as _cron_build_tools

    tools = _cron_build_tools(env)
    cron_add = tools["cron_add"]["func"]
    cron_list = tools["cron_list"]["func"]

    listed = cron_list()
    if "error" in listed:
        return {"error": listed["error"]}
    for job in listed.get("jobs", []):
        if isinstance(job, dict) and job.get("id") == TRIGGER_CRON_ID:
            return {"job": job, "already_existed": True}

    added = cron_add(
        cron=TRIGGER_CRON_EXPR,
        prompt=TRIGGER_CRON_PROMPT,
        job_id=TRIGGER_CRON_ID,
    )
    if isinstance(added, dict) and "error" in added:
        return {"error": added["error"]}
    job = added.get("job", {}) if isinstance(added, dict) else {}
    return {"job": job, "already_existed": False}
