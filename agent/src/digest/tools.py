"""Agent tool for the digest module: preview the briefing markdown.

`digest_preview` takes the sections as a JSON string and returns the rendered
markdown, so the agent can check the exact text a cron job will deliver before
(or while) composing it. It never sends anything itself; delivery is the cron
runner's job via the job's `deliver_to`.
"""

import json
from typing import Any, Callable, Dict, List, Optional

from digest.briefing import build_briefing


def _schema(properties: Optional[Dict[str, Any]] = None,
            required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or []}


def build_tools(env: Any = None) -> Dict[str, Dict[str, Any]]:
    """Tool dicts for the environment: digest_preview.

    `env` is accepted for the same factory shape as the other tool modules; the
    preview needs nothing from it.
    """

    def _preview(sections: str) -> Dict[str, Any]:
        try:
            data = json.loads(sections)
        except (TypeError, ValueError) as e:
            return {"error": f"sections is not valid JSON: {e}"}
        if not isinstance(data, dict):
            return {"error": "sections must be a JSON object mapping section name to a list of strings"}
        return {"preview": build_briefing(data)}

    return {
        "digest_preview": {
            "name": "digest_preview",
            "func": _preview,
            "description": (
                "Render the proactive briefing markdown from its sections, so you can "
                "preview exactly what a cron job will deliver. `sections` is a JSON "
                "object like {\"goals\": [...], \"people\": [...], \"reminders\": [...], "
                "\"notes\": [...]}; each value is a list of strings and missing or "
                "empty sections are skipped. Returns {\"preview\": <markdown>}."
            ),
            "schema": _schema(
                {
                    "sections": {
                        "type": "string",
                        "description": "JSON object of section name -> list of strings.",
                    },
                },
                ["sections"],
            ),
        },
    }
