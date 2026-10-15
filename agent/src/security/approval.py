"""Interactive human-in-the-loop approval gate for mutating/dangerous tools."""

import sys
from typing import Any, Callable, Dict, Optional

# Everything not listed here prompts before it runs. Keep this in step with the tools
# AgentEnvironment actually registers: naming a tool that does not exist silently makes
# the real one prompt, and naming a mutating tool here makes the prompt a formality.
SAFE_TOOLS = {
    "read_file", "list_skills", "view_skill", "session_search",
    "ast_inspect", "process_list", "process_poll", "sample_echo",
    # Read-only discovery and mail tools: no external side effects.
    "search_tools", "list_toolsets",
    "gmail_search", "gmail_read", "gmail_draft", "gmail_discard",
    # NOTE: gmail_send is deliberately NOT here — sending mail always prompts.
}

class ApprovalGate:
    def __init__(self, yolo: bool = False, interactive_prompt_fn: Optional[Callable[[str, Dict[str, Any]], bool]] = None):
        self.yolo = yolo
        self.prompt_fn = interactive_prompt_fn

    def check_approval(self, tool_name: str, arguments: Dict[str, Any]) -> bool:
        """Return True if approved to execute, False otherwise."""
        if self.yolo:
            return True

        if tool_name in SAFE_TOOLS:
            return True

        # Mutating tool requires confirmation
        if self.prompt_fn:
            return self.prompt_fn(tool_name, arguments)

        # With no terminal there is nobody to ask, and reading stdin anyway would consume a
        # piped prompt line. Deny: BOOM_YOLO=1 is how this runs unattended.
        if not sys.stdin.isatty():
            return False

        try:
            ans = input(
                f"[Approval Required] Allow tool '{tool_name}' with args {arguments}? [y/N]: "
            ).strip().lower()
            return ans in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False
