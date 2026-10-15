"""tools.filesystem — thao tác file/thư mục an toàn cho Boom.

Re-export từ .ops để backward-compat: các tool dùng
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

# Alias đúng tên yêu cầu trong spec ("list"); list_dir là tên chính
# để tránh đè builtin list trong code nội bộ module.
list = list_dir  # noqa: A001 - alias có chủ đích theo spec

__all__ = ["read", "write", "edit", "append", "delete", "list", "list_dir", "info"]
