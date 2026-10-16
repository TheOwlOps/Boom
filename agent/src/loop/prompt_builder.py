import os
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

class SystemPromptBuilder:
    """Dynamic system prompt assembly for the Boom agent architecture."""

    def __init__(self, workspace_root: Path):
        self.workspace_root = Path(workspace_root)

    @staticmethod
    def get_platform_tone(channel: str) -> str:
        ch = channel.lower()
        if "telegram" in ch or "discord" in ch:
            return (
                "Platform Tone Rules:\n"
                "- Discord / Telegram: ultra-terse, telegraphic. 1 line when enough.\n"
                "- Strip conjunctions. State: [thing] [action] [reason]. [next step]."
            )
        elif "zalo" in ch:
            return (
                "Platform Tone Rules:\n"
                "- Zalo: LỄ PHÉP, dạ/vâng/ạ, thưa gửi đàng hoàng. Full sentences, respectful.\n"
                "- 2-4 lines, warm. Always end with ạ/dạ when appropriate."
            )
        return "Platform Tone Rules: Direct, precise, professional."

    def build_system_prompt(
        self,
        channel: str = "tui",
        available_skills: Optional[List[Dict[str, str]]] = None,
        memory_notes: Optional[str] = None
    ) -> str:
        now_utc = datetime.now(timezone.utc).strftime("%A, %B %d, %Y (%Z)")
        host_os = platform.system()
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

        parts = [
            "# Role & Identity",
            "You are an intelligent, versatile, and highly capable AI assistant built on the Boom Harness.",
            "You operate with precision, speed, and strict verification before claiming work is complete.",
            "",
            "# Runtime Environment",
            f"Host OS: {host_os}",
            f"Python: {py_ver}",
            f"Current working directory: {self.workspace_root.as_posix()}",
            f"Current time: {now_utc}",
            "",
            self.get_platform_tone(channel),
        ]

        # Injected durable memory notes
        if memory_notes:
            parts.extend([
                "",
                "# Persistent Memory Notes",
                memory_notes
            ])

        # Progressive skills index block
        if available_skills:
            parts.extend([
                "",
                "<available_skills>"
            ])
            for s in available_skills:
                desc = s.get("description", "").split("\n")[0][:80]
                parts.append(f"  {s['name']}: {desc}")
            parts.append("</available_skills>")
            parts.append("Load detailed skills on-demand using view_skill(name).")

        return "\n".join(parts)
