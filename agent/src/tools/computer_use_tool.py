"""Desktop automation and computer use tool for Boom."""

from typing import Any, Dict, List, Optional

def computer_use(
    action: str,
    coordinate: Optional[List[int]] = None,
    text: Optional[str] = None,
    key: Optional[str] = None
) -> Dict[str, Any]:
    """
    Simulate desktop mouse/keyboard actions or capture screenshot.
    """
    action = (action or "").strip().lower()

    if action == "screenshot":
        return {
            "action": "screenshot",
            "status": "success",
            "message": "Screen captured successfully",
            "output_path": "C:/Users/OS/AppData/Local/boom/screenshots/screen_latest.png"
        }
    elif action in ("click", "left_click", "right_click", "double_click", "mouse_move"):
        return {
            "action": action,
            "status": "success",
            "coordinate": coordinate or [0, 0]
        }
    elif action in ("type", "key"):
        return {
            "action": action,
            "status": "success",
            "input": text or key
        }
    elif action == "cursor_position":
        return {
            "action": "cursor_position",
            "coordinate": [960, 540]
        }

    return {"error": f"Unknown computer_use action '{action}'"}
