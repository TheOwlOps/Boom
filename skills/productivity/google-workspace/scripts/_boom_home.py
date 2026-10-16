"""Resolve BOOM_HOME for standalone skill scripts.

Skill scripts may run outside the Boom process (e.g. system Python,
nix env, CI) where ``boom_constants`` is not importable.  This module
provides the same ``get_boom_home()`` and ``display_boom_home()``
contracts as ``boom_constants`` without requiring it on ``sys.path``.

When ``boom_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``boom_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``BOOM_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from boom_constants import display_boom_home
    from boom_constants import get_boom_home
except (ModuleNotFoundError, ImportError):

    def get_boom_home() -> Path:
        """Return the Boom home directory (default: ~/.boom).

        Mirrors ``boom_constants.get_boom_home()``."""
        val = os.environ.get("BOOM_HOME", "").strip()
        return Path(val) if val else Path.home() / ".boom"

    def display_boom_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``boom_constants.display_boom_home()``."""
        home = get_boom_home()
        try:
            return "~/" + home.relative_to(Path.home()).as_posix()
        except ValueError:
            return str(home)
