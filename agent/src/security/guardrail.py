import re
from pathlib import Path
from typing import Any, Tuple

class SecurityGuardrail:
    """Tirith-inspired security and safety guardrails."""

    DANGEROUS_PATTERNS = [
        r"\brm\s+-rf\s+[/~]",
        r"\bmkfs\b",
        r"\bdd\s+if=.*\sof=/dev/[s|h|v]d",
        r"\bformat\s+[a-z]:\s+/fs",
        r"\bdrop\s+database\b",
        r"\bchmod\s+-R\s+777\s+/",
        r"\bcurl.*\|\s*(?:bash|sh|zsh)\b",
        r"cat\s+.*id_rsa",
        r"cat\s+.*id_ed25519",
    ]

    def __init__(self, workspace_root: Path):
        self.workspace_root = Path(workspace_root).resolve()

    def check_command(self, cmd: str) -> Tuple[bool, str]:
        """Verify command against hazardous execution patterns."""
        for pattern in self.DANGEROUS_PATTERNS:
            if re.search(pattern, cmd, re.IGNORECASE):
                return False, f"Command blocked by security policy: matches dangerous pattern '{pattern}'"
        return True, "Allowed"

    def command_allowed(self, *parts: Any) -> Tuple[bool, str]:
        """check_command over a command and its arguments, which arrive separately."""
        return self.check_command(" ".join(str(p) for p in parts if p))
