"""Long-term goal tracking for the owner.

A goal is a durable, owner-level outcome ("ship the Boom web UI", "learn conversational
Japanese") with a running progress log. Goals live in a JSON ledger beside the other
memories/ state and are manipulated through the goal_* tools (see tools.py).

Nothing here is decorative: GoalStore is the persistence layer, tools.py exposes it to
the agent loop, and environment.py wires both in one guarded import.
"""

from goals.store import GOALS_FILE, STATUSES, GoalStore, _goals_path
from goals.tools import build_tools

__all__ = ["GOALS_FILE", "STATUSES", "GoalStore", "_goals_path", "build_tools"]
