"""tools — root package: re-exports all core tools for backward-compat.

Old modules (agent/src/tools/shell.py, file_ops.py, ...) still import fine
from the same dir; the new filesystem/terminal/git packages are re-exported
here:

    from tools import read, write, run, git_status, ...
"""

from .filesystem import *
from .git import *
from .terminal import *

from . import filesystem, git, terminal

__all__ = filesystem.__all__ + terminal.__all__ + git.__all__
