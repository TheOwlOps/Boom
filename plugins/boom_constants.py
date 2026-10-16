"""Shared constants for Boom plugins.

Provides the canonical Boom home directory used by plugins for
data, config, and auth storage.
"""

import os
from pathlib import Path


def get_boom_home() -> Path:
    """Return the Boom home directory: $BOOM_HOME or ~/.boom."""
    return Path(os.environ.get("BOOM_HOME", str(Path.home() / ".boom")))


def get_boom_config() -> Path:
    """Return the default config file path: $BOOM_HOME/config.json."""
    return get_boom_home() / "config.json"
