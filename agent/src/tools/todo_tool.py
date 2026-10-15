"""In-memory, revisioned task list tool for multi-step work and context compaction."""

from typing import Any, Dict, List, Optional

VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}
MAX_TODO_ITEMS = 256
MAX_TODO_CONTENT_CHARS = 4000
TODO_INJECTION_HEADER = "[Your active task list was preserved across context compression]"
_STATUS_MARKERS = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]", "cancelled": "[~]"}

class TodoStore:
    def __init__(self):
        self._items: List[Dict[str, str]] = []
        self._revision = 0

    def _validate(self, item: Dict[str, Any]) -> Dict[str, str]:
        if not isinstance(item, dict):
            raise ValueError("Todo item must be an object.")
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            raise ValueError("Todo item must have a non-empty 'id'.")
        content = str(item.get("content") or "").strip()
        if not content:
            raise ValueError("Todo item must have non-empty 'content'.")
        if len(content) > MAX_TODO_CONTENT_CHARS:
            content = content[:MAX_TODO_CONTENT_CHARS] + "… [truncated]"
        status = str(item.get("status") or "pending").strip().lower()
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status '{status}'. Must be one of {sorted(VALID_STATUSES)}.")

        res = {"id": item_id, "content": content, "status": status}
        if item.get("parent"):
            res["parent"] = str(item["parent"]).strip()
        return res

    def read(self) -> List[Dict[str, str]]:
        return [item.copy() for item in self._items]

    def write(self, todos: List[Dict[str, Any]], merge: bool = False) -> List[Dict[str, str]]:
        before = self.read()
        try:
            if merge:
                existing = {item["id"]: item for item in self._items}
                for t in todos:
                    item_id = str(t.get("id", "")).strip()
                    if not item_id:
                        continue
                    if item_id in existing:
                        cur = existing[item_id]
                        if t.get("content"):
                            cur["content"] = str(t["content"]).strip()[:MAX_TODO_CONTENT_CHARS]
                        if t.get("status") and str(t["status"]).strip().lower() in VALID_STATUSES:
                            cur["status"] = str(t["status"]).strip().lower()
                        if "parent" in t:
                            cur["parent"] = str(t["parent"]).strip()
                    else:
                        val = self._validate(t)
                        existing[val["id"]] = val
                        self._items.append(val)
            else:
                self._items = [self._validate(t) for t in todos][:MAX_TODO_ITEMS]
        except ValueError:
            self._items = before
            raise

        if self._items != before:
            self._revision += 1
        return self.read()

    def format_for_injection(self) -> Optional[str]:
        if not self._items:
            return None
        lines = [TODO_INJECTION_HEADER]
        for it in self._items:
            marker = _STATUS_MARKERS.get(it["status"], "[ ]")
            parent_info = f" (parent: {it['parent']})" if "parent" in it else ""
            lines.append(f"{marker} [{it['id']}] {it['content']}{parent_info}")
        return "\n".join(lines)


_GLOBAL_TODO_STORE = TodoStore()

def todo_list(todos: Optional[List[Dict[str, Any]]] = None, merge: bool = False) -> Dict[str, Any]:
    """View or update the active todo list."""
    if todos is not None:
        updated = _GLOBAL_TODO_STORE.write(todos, merge=merge)
        return {"todos": updated, "count": len(updated), "revision": _GLOBAL_TODO_STORE._revision}
    items = _GLOBAL_TODO_STORE.read()
    return {"todos": items, "count": len(items), "revision": _GLOBAL_TODO_STORE._revision}
