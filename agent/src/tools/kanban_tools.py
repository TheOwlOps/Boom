"""Kanban board toolset for Boom agent task workflow tracking."""

import time
from typing import Any, Dict, List, Optional

class KanbanBoard:
    def __init__(self):
        self.cards: Dict[str, Dict[str, Any]] = {}
        self._seq = 1

    def create(self, title: str, description: str = "", column: str = "todo") -> Dict[str, Any]:
        card_id = f"KB-{self._seq}"
        self._seq += 1
        card = {
            "id": card_id,
            "title": title,
            "description": description,
            "column": column,
            "status": "open",
            "comments": [],
            "created_at": time.time(),
            "updated_at": time.time()
        }
        self.cards[card_id] = card
        return card

    def list_cards(self, column: Optional[str] = None) -> List[Dict[str, Any]]:
        cards = list(self.cards.values())
        if column:
            cards = [c for c in cards if c["column"] == column]
        return cards

    def show(self, card_id: str) -> Optional[Dict[str, Any]]:
        return self.cards.get(card_id)

    def complete(self, card_id: str) -> bool:
        if card_id in self.cards:
            self.cards[card_id]["column"] = "done"
            self.cards[card_id]["status"] = "completed"
            self.cards[card_id]["updated_at"] = time.time()
            return True
        return False

    def comment(self, card_id: str, text: str) -> bool:
        if card_id in self.cards:
            self.cards[card_id]["comments"].append({"text": text, "timestamp": time.time()})
            self.cards[card_id]["updated_at"] = time.time()
            return True
        return False

    def block(self, card_id: str, reason: str) -> bool:
        if card_id in self.cards:
            self.cards[card_id]["status"] = "blocked"
            self.cards[card_id]["blocked_reason"] = reason
            self.cards[card_id]["updated_at"] = time.time()
            return True
        return False

    def unblock(self, card_id: str) -> bool:
        if card_id in self.cards:
            self.cards[card_id]["status"] = "open"
            self.cards[card_id].pop("blocked_reason", None)
            self.cards[card_id]["updated_at"] = time.time()
            return True
        return False

_GLOBAL_KANBAN = KanbanBoard()

def kanban_manage(action: str, **kwargs) -> Dict[str, Any]:
    action = (action or "").strip().lower()
    if action == "create":
        card = _GLOBAL_KANBAN.create(kwargs.get("title", "Untitled"), kwargs.get("description", ""), kwargs.get("column", "todo"))
        return {"action": "create", "card": card}
    elif action == "list":
        return {"action": "list", "cards": _GLOBAL_KANBAN.list_cards(kwargs.get("column"))}
    elif action == "show":
        c = _GLOBAL_KANBAN.show(kwargs.get("card_id", ""))
        return {"action": "show", "card": c} if c else {"error": "Card not found"}
    elif action == "complete":
        ok = _GLOBAL_KANBAN.complete(kwargs.get("card_id", ""))
        return {"action": "complete", "success": ok}
    elif action == "comment":
        ok = _GLOBAL_KANBAN.comment(kwargs.get("card_id", ""), kwargs.get("text", ""))
        return {"action": "comment", "success": ok}
    elif action == "block":
        ok = _GLOBAL_KANBAN.block(kwargs.get("card_id", ""), kwargs.get("reason", ""))
        return {"action": "block", "success": ok}
    elif action == "unblock":
        ok = _GLOBAL_KANBAN.unblock(kwargs.get("card_id", ""))
        return {"action": "unblock", "success": ok}
    return {"error": f"Unknown action '{action}'"}
