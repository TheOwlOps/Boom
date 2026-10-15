"""Agent-facing tools cho trigger module.

Tool dicts theo repo convention {"name", "func", "description", "schema"} va
failure envelope {"error": ...}. Moi tool tra ve dict thuan; khong raise
ra ngoai agent loop.

Trigger luu tai <root_dir>/memories/triggers.db (SQLite, tao lazy khi goi tool).
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from triggers.models import Trigger, VALID_KINDS
from triggers.store import TriggerStore

_ACTION_TYPES = frozenset({"notify", "log"})


def _schema(properties: Dict[str, Any] = None,
            required: List[str] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties or {},
            "required": required or []}


def _resolve_root(env_or_root_dir) -> Optional[Path]:
    """Chap nhan environment (co .root_dir, convention cua repo) hoac path thuan."""
    root = getattr(env_or_root_dir, "root_dir", env_or_root_dir)
    try:
        return Path(root) if root is not None else None
    except (TypeError, ValueError):
        return None


def _validate_config(kind: str, config: Any) -> Optional[str]:
    """Validate nhe config theo kind. Tra ve chuoi loi, None neu ok."""
    if not isinstance(config, dict):
        return "config must be a dict"
    if kind == "gmail_new":
        if not (config.get("query") or "").strip():
            return "gmail_new needs config.query (non-empty string)"
    elif kind == "price_below":
        if not (config.get("url") or "").strip():
            return "price_below needs config.url (non-empty string)"
        try:
            float(config.get("threshold"))
        except (TypeError, ValueError):
            return ("price_below needs config.threshold "
                    "as a number, got %r" % (config.get("threshold"),))
    elif kind == "keyword_news":
        if not (config.get("keyword") or "").strip():
            return "keyword_news needs config.keyword (non-empty string)"
    elif kind == "webhook":
        pass  # khong can config gi
    return None


def _validate_action(action: Any) -> Optional[str]:
    """Validate nhe action dict. Tra ve chuoi loi, None neu ok."""
    if not isinstance(action, dict):
        return "action must be a dict"
    atype = action.get("type")
    if atype not in _ACTION_TYPES:
        return ("action.type must be one of %s, got %r"
                % (sorted(_ACTION_TYPES), atype))
    if atype == "notify":
        for key in ("channel", "target", "template"):
            val = action.get(key)
            bad = not val.strip() if isinstance(val, str) else not val
            if bad:
                return "notify action needs non-empty %r" % (key,)
    return None


def build_tools(env_or_root_dir) -> Dict[str, Dict[str, Any]]:
    """4 tools: trigger_create, trigger_list, trigger_delete, trigger_toggle.

    DB tai <root_dir>/memories/triggers.db (lazy tao khi goi tool).
    root_dir None -> moi tool tra {"error"}.
    """
    root_dir = _resolve_root(env_or_root_dir)

    def _need_store():
        if root_dir is None:
            return None, {"error": "environment has no root_dir: "
                                   "nowhere to keep triggers"}
        try:
            return TriggerStore(root_dir / "memories" / "triggers.db"), None
        except OSError as e:
            return None, {"error": "could not open the triggers db: %s" % (e,)}

    def _create(name: str, kind: str, config: Dict[str, Any] = None,
                action: Dict[str, Any] = None,
                cooldown_sec: int = 3600) -> Dict[str, Any]:
        store, err = _need_store()
        if err:
            return err
        if not (name or "").strip():
            return {"error": "name is empty: give the trigger a name"}
        if kind not in VALID_KINDS:
            return {"error": "unknown trigger kind %r: must be one of %s"
                    % (kind, sorted(VALID_KINDS))}
        cfg_err = _validate_config(kind, config if config is not None else {})
        if cfg_err:
            return {"error": cfg_err}
        act_err = _validate_action(action if action is not None else {})
        if act_err:
            return {"error": act_err}
        try:
            cooldown = int(cooldown_sec)
        except (TypeError, ValueError):
            return {"error": "cooldown_sec must be an integer, got %r"
                    % (cooldown_sec,)}
        if cooldown < 0:
            return {"error": "cooldown_sec must be >= 0"}
        trigger = Trigger(
            id="",
            name=name.strip(),
            kind=kind,
            config=dict(config or {}),
            action=dict(action or {}),
            enabled=True,
            cooldown_sec=cooldown,
        )
        try:
            tid = store.add(trigger)
        except (ValueError, OSError) as e:
            return {"error": "could not save trigger: %s" % (e,)}
        saved = store.get(tid)
        return {"trigger": saved.to_dict() if saved else trigger.to_dict()}

    def _list(enabled_only: bool = False) -> Dict[str, Any]:
        store, err = _need_store()
        if err:
            return err
        try:
            triggers = store.list(enabled_only=bool(enabled_only))
        except OSError as e:
            return {"error": "could not read the triggers db: %s" % (e,)}
        return {"triggers": [t.to_dict() for t in triggers],
                "total": len(triggers)}

    def _delete(trigger_id: str) -> Dict[str, Any]:
        store, err = _need_store()
        if err:
            return err
        try:
            deleted = store.delete(trigger_id)
        except OSError as e:
            return {"error": "could not write the triggers db: %s" % (e,)}
        return {"deleted": bool(deleted)}

    def _toggle(trigger_id: str) -> Dict[str, Any]:
        store, err = _need_store()
        if err:
            return err
        try:
            ok = store.toggle(trigger_id)
        except OSError as e:
            return {"error": "could not write the triggers db: %s" % (e,)}
        if not ok:
            return {"enabled": None}
        cur = store.get(trigger_id)
        return {"enabled": bool(cur.enabled) if cur else None}

    return {
        "trigger_create": {
            "name": "trigger_create",
            "func": _create,
            "description": (
                "Create a trigger: a condition checked periodically by the "
                "runner, running an action when it fires. kinds: gmail_new "
                "(config.query), price_below (config.url + config.threshold), "
                "keyword_news (config.keyword), webhook (no config). "
                "action: {'type': 'notify', 'channel', 'target', 'template'} "
                "or {'type': 'log'}. Returns the created trigger."
            ),
            "schema": _schema(
                {
                    "name": {"type": "string",
                             "description": "Trigger name, e.g. 'New invoice mail'."},
                    "kind": {"type": "string",
                             "description": "One of gmail_new, price_below, keyword_news, webhook."},
                    "config": {"type": "object",
                               "description": "Kind-specific config (see description)."},
                    "action": {"type": "object",
                               "description": "{'type': 'notify'|'log', ...}."},
                    "cooldown_sec": {"type": "integer",
                                     "description": "Min seconds between two fires. Default 3600."},
                },
                ["name", "kind", "config", "action"],
            ),
        },
        "trigger_list": {
            "name": "trigger_list",
            "func": _list,
            "description": "List triggers, optionally only enabled ones.",
            "schema": _schema(
                {
                    "enabled_only": {"type": "boolean",
                                     "description": "Only list enabled triggers. Default false."},
                },
                [],
            ),
        },
        "trigger_delete": {
            "name": "trigger_delete",
            "func": _delete,
            "description": "Delete a trigger by id. Returns {'deleted': bool}.",
            "schema": _schema(
                {
                    "trigger_id": {"type": "string",
                                   "description": "Trigger id from trigger_list."},
                },
                ["trigger_id"],
            ),
        },
        "trigger_toggle": {
            "name": "trigger_toggle",
            "func": _toggle,
            "description": ("Flip a trigger's enabled flag. Returns the new "
                            "state, or {'enabled': null} when not found."),
            "schema": _schema(
                {
                    "trigger_id": {"type": "string",
                                   "description": "Trigger id from trigger_list."},
                },
                ["trigger_id"],
            ),
        },
    }
