"""Proactive briefing builder: compact Vietnamese markdown digests.

A cron job's prompt asks the agent to gather the day's sections (goals, people,
reminders, notes); the agent renders them with `digest_preview` (see tools.py) and
the resulting markdown is pushed to the job's `deliver_to` channel by the cron
delivery hook (CronRunner._maybe_deliver in tools/cronjob_tool.py).

Public surface:
    build_briefing(sections, on_date=None) -> str
    empty_briefing() -> str
    build_tools(env=None) -> {"digest_preview": {...}}
"""

from digest.briefing import build_briefing, empty_briefing
from digest.tools import build_tools

__all__ = ["build_briefing", "empty_briefing", "build_tools"]
