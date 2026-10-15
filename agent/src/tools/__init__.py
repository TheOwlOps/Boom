"""tools — root package: re-export toàn bộ tools core để backward-compat.

Các module cũ (agent/src/tools/shell.py, file_ops.py, ...) vẫn import được
bình thường vì nằm cùng thư mục; package mới filesystem/terminal/git
được re-export tại đây:

    from agent.src.tools import read, write, run, git_status, ...
"""

from agent.src.tools.filesystem import *
from agent.src.tools.git import *
from agent.src.tools.terminal import *

from . import filesystem, git, terminal

__all__ = filesystem.__all__ + terminal.__all__ + git.__all__
