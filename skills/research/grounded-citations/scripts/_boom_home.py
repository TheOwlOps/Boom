"""Resolve BOOM_HOME for standalone skill scripts.

Skill scripts may run outside the Boom process (system Python, nix env,
CI) where ``boom_constants`` is not importable.  This module provides the
same ``get_boom_home()`` contract without requiring it on ``sys.path``.

When ``boom_constants`` IS available it is used directly so profile
resolution and any future enhancements are picked up automatically.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from boom_constants import get_boom_home
except (ModuleNotFoundError, ImportError):

    def get_boom_home() -> Path:
        """Return the Boom home directory (default: ``~/.boom``)."""
        val = os.environ.get("BOOM_HOME", "").strip()
        return Path(val) if val else Path.home() / ".boom"
