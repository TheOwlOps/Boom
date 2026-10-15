import re
from typing import Dict, Any, Tuple, Optional

class SystemOneGate:
    """Fast Reflex Routing, Intent Safety Classifier (<2ms latency), and Owner-Only Guard."""

    FAST_SLASH_COMMANDS = {
        "/status": "system_status",
        "/ping": "ping",
        "/help": "system_help",
        "/skills": "list_skills",
        "/clear": "clear_session",
    }

    INJECTION_PATTERNS = [
        r"ignore\s+(all\s+)?previous\s+instructions",
        r"you\s+are\s+now\s+an\s+unrestricted",
        r"system\s*prompt\s*leak",
        r"reveal\s+(internal\s+)?prompt",
        r"bypass\s+safety\s+filter",
    ]

    ARCH_INQUIRY_PATTERNS = [
        r"system\s*prompt",
        r"cấu\s*trúc\s*model",
        r"model\s*structure",
        r"danh\s*sách\s*tool",
        r"tools?\s*list",
        r"cấu\s*hình\s*nội\s*bộ",
        r"internal\s*config",
        r"người\s*tạo\s*ra\s*bạn",
        r"who\s*created\s*you",
        r"who\s*made\s*you",
        r"prompt\s*gốc",
    ]

    OWNER_IDENTITIES = {"ryan", "sếp", "mark", "son", "son nguyen", "vansoncoder51@gmail.com"}

    @classmethod
    def evaluate(cls, user_prompt: str, user_identity: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
        """
        Classify input into:
        - 'blocked': prompt injection or dangerous intent
        - 'owner_guard_blocked': blocked by Owner-Only Guard for non-owner
        - 'fast_command': slash commands handled immediately without LLM
        - 'reasoning_path': complex request routed to ReAct / DAG engine
        """
        trimmed = user_prompt.strip()

        # 1. Fast Slash Commands
        if trimmed.startswith("/"):
            cmd = trimmed.split()[0].lower()
            if cmd in cls.FAST_SLASH_COMMANDS:
                return "fast_command", {"command": cls.FAST_SLASH_COMMANDS[cmd]}

        # 2. Owner-Only Guard
        # If user asks about internal architecture/prompt/model/tools and is NOT verified Owner
        user_id_clean = (user_identity or "").strip().lower()
        is_owner = user_id_clean in cls.OWNER_IDENTITIES

        if not is_owner:
            for pattern in cls.ARCH_INQUIRY_PATTERNS:
                if re.search(pattern, trimmed, re.IGNORECASE):
                    return "owner_guard_blocked", {
                        "response": "Xin lỗi, thông tin kiến trúc và cấu hình hệ thống được bảo mật tuyệt đối.",
                        "reason": "Owner-Only Guard: Non-owner inquiring about confidential architecture."
                    }

        # 3. Injection & Jailbreak Guard
        for pattern in cls.INJECTION_PATTERNS:
            if re.search(pattern, trimmed, re.IGNORECASE):
                return "blocked", {
                    "reason": "Request blocked by System 1 Security Gate (Prompt Injection Detected)"
                }

        # 4. Default to Reasoning Path
        return "reasoning_path", {"prompt": trimmed}
