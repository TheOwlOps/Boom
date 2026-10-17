"""Boomi brand assets for terminal output.

The Boomi owl ASCII art mirrors the official mascot: fluffy navy owl,
round orange glasses, sparkling eyes, antenna on top.
"""

BOOMI_ART = """
           .---.
           |o o|
            |=|
      .-----^-^-----.
     /  ___   ___   \\
    |  / _ \\ / _ \\  |
    | | (_) | (_) | |
    |  \\___/ \\___/  |
    |       >       |
     \\    _____   /
      '._________.'
    __/           \\__
   /  fluffy  navy   \\
   \\   _________    /
    \\_/         \\_/
      |__|   |__|
"""

# Compact version for narrow terminals
BOOMI_MINI = """
  ,_,
 (o,o)   Boomi - AI Agent Harness
 (   )
"""

LOGO_PATH = "assets/logo/boomi-logo.png"


def print_brand():
    """Print the Boomi brand header to stdout."""
    print(BOOMI_ART)
