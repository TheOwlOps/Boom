"""Boom autonomous capabilities: self-modification with safety nets."""

from .surgery import (  # noqa: F401
    apply_patch,
    insert_after,
    preview_diff,
    backup,
    rollback,
    validate_syntax,
)

__all__ = [
    "apply_patch",
    "insert_after",
    "preview_diff",
    "backup",
    "rollback",
    "validate_syntax",
    "NeedsHuman",
    "run_goal",
    "guard",
    "controller",
    "debugger",
    "classify_error",
    "suggest_fix",
    "debug_cycle",
    "run_python",
    "critic_available",
]

from .controller import NeedsHuman, run_goal  # noqa: F401,E402
from .debugger import (  # noqa: F401,E402
    classify_error,
    suggest_fix,
    debug_cycle,
    run_python,
    critic_available,
)
from . import guard  # noqa: F401,E402
from . import controller  # noqa: F401,E402
from . import debugger  # noqa: F401,E402
