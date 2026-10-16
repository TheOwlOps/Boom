"""tools.filesystem — safe file/dir operations for Boom.

Re-exported from .ops for backward-compat: tools use
`from agent.src.tools.filesystem import read, write, ...`
"""

from agent.src.tools.filesystem.ops import (
    append,
    delete,
    edit,
    info,
    list_dir,
    read,
    write,
)

# Alias with the spec-required name ("list"); list_dir is the canonical name
# to avoid shadowing the builtin list inside module code.
list = list_dir  # noqa: A001 - deliberate alias per spec

__all__ = ["read", "write", "edit", "append", "delete", "list", "list_dir", "info"]
