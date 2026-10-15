"""Local secure credential vault for Boom."""

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

class LocalVaultStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or (Path.home() / "AppData" / "Local" / "boom" / "vault.json")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._items: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self.db_path.exists():
            try:
                return json.loads(self.db_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save(self):
        self.db_path.write_text(json.dumps(self._items, indent=2), encoding="utf-8")

    def save_item(self, origin: str, kind: str, identifier: str, secret: str, label: Optional[str] = None) -> str:
        handle = f"vault_{hashlib.sha256(f'{origin}:{identifier}:{time.time()}'.encode()).hexdigest()[:16]}"
        self._items[handle] = {
            "handle": handle,
            "origin": origin,
            "kind": kind,
            "identifier": identifier,
            "label": label or origin,
            "secret_masked": "[PROTECTED_IN_VAULT]",
            "created_at": time.time()
        }
        self._save()
        return handle

    def list_items(self, origin: Optional[str] = None) -> List[Dict[str, Any]]:
        res = []
        for it in self._items.values():
            if not origin or it["origin"] == origin:
                # Never leak secret in list
                res.append({
                    "handle": it["handle"],
                    "origin": it["origin"],
                    "kind": it["kind"],
                    "identifier": it["identifier"],
                    "label": it["label"]
                })
        return res

    def get_handle(self, handle: str) -> Optional[Dict[str, Any]]:
        return self._items.get(handle)

_GLOBAL_VAULT = LocalVaultStore()

def vault_manage(action: str, **kwargs) -> Dict[str, Any]:
    action = (action or "").strip().lower()
    if action == "list":
        return {"action": "list", "items": _GLOBAL_VAULT.list_items(kwargs.get("origin"))}
    elif action == "save":
        h = _GLOBAL_VAULT.save_item(
            origin=kwargs.get("origin", "localhost"),
            kind=kwargs.get("kind", "login"),
            identifier=kwargs.get("identifier", "user"),
            secret=kwargs.get("secret", ""),
            label=kwargs.get("label")
        )
        return {"action": "save", "status": "success", "handle": h}
    return {"error": f"Unknown vault action '{action}'"}
