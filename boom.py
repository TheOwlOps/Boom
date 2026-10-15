#!/usr/bin/env python3
"""Boom Agent Harness - Main Executable Entrypoint."""

import sys
from pathlib import Path

# Add project root to path
root_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(root_dir))
sys.path.insert(0, str(root_dir / "agent" / "src"))

from agent.src.cli import main

if __name__ == "__main__":
    main()
