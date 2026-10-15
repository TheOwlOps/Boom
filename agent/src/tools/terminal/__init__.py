"""tools.terminal — chạy lệnh shell + quản lý tiến trình nền cho Boom.

Re-export từ .shell (run/bg/status/kill) và .process (list/kill tiến trình ledger).
"""

from agent.src.tools.terminal.process import (
    ProcessManager,
    configure,
    get_manager,
    kill_process,
    list_processes,
    poll_process,
    start_process,
)
from agent.src.tools.terminal.shell import (
    DEFAULT_TIMEOUT,
    RunOptions,
    bg,
    forget,
    kill,
    run,
    status,
)

__all__ = [
    "run", "bg", "status", "kill", "forget", "RunOptions", "DEFAULT_TIMEOUT",
    "ProcessManager", "configure", "get_manager", "start_process",
    "poll_process", "list_processes", "kill_process",
]
