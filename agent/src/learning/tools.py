"""Agent-facing tools for the learning subsystem (learning.store.LearningStore).

Tool dicts follow the repo convention {"name", "func", "description", "schema"} and the
{"error": ...} failure envelope. Every tool returns a plain dict; nothing here raises
into the agent loop.

Import root is agent/src, so: from learning.tools import build_tools.
"""

from pathlib import Path
from typing import Any, Dict, Optional

from learning.models import Learning
from learning.store import LearningStore
from learning.extractor import extract_learnings, should_learn

LEARNINGS_DB = "learnings.db"


def _schema(properties: Dict[str, Any] = None, required=None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or []}


def _resolve_root(env_or_root_dir) -> Optional[Path]:
    """Accept either an environment (has .root_dir, the repo convention) or a plain path."""
    root = getattr(env_or_root_dir, "root_dir", env_or_root_dir)
    if root is None:
        return None
    return Path(root)


def _learnings_path(env_or_root_dir) -> Optional[Path]:
    root = _resolve_root(env_or_root_dir)
    if root is None:
        return None
    return root / "memories" / LEARNINGS_DB


def build_tools(env) -> Dict[str, Dict[str, Any]]:
    """Tool dicts for the environment: learn_from_conversation, recall_learned,
    forget_learning.

    Learnings live in <root_dir>/memories/learnings.db. learn_from_conversation needs
    env.llm (a callable); recall_learned and forget_learning only need the store.
    Accepts the environment itself or a root_dir path directly.
    """

    def _need_store():
        path = _learnings_path(env)
        if path is None:
            return None, {"error": "environment has no root_dir: nowhere to keep learnings"}
        try:
            return LearningStore(str(path)), None
        except Exception as e:
            return None, {"error": f"could not open the learnings store: {e}"}

    def _need_llm():
        llm = getattr(env, "llm", None)
        if not callable(llm):
            return None, {"error": "no llm configured"}
        return llm, None

    def _as_learning(item) -> Optional[Learning]:
        if isinstance(item, Learning):
            return item
        if isinstance(item, dict):
            try:
                return Learning.from_dict(item)
            except Exception:
                return None
        return None

    def _learn_from_conversation(text: str, source: str = "") -> Dict[str, Any]:
        llm, err = _need_llm()
        if err:
            return err
        try:
            if not should_learn(text):
                return {"status": "skipped", "reason": "nothing durable to learn"}
        except Exception as e:
            return {"error": f"learning gate failed: {e}"}
        try:
            extracted = extract_learnings(text, llm, source)
        except Exception as e:
            return {"error": f"extraction failed: {e}"}
        store, err = _need_store()
        if err:
            return err
        ids = []
        try:
            for item in extracted or []:
                learning = _as_learning(item)
                if learning is None:
                    continue
                stored_id = store.add(learning)  # returns the id (str)
                ids.append(stored_id)
        except Exception as e:
            return {"error": f"could not store learnings: {e}"}
        return {"learned": len(ids), "ids": ids}

    def _recall_learned(query: str) -> Dict[str, Any]:
        store, err = _need_store()
        if err:
            return err
        try:
            hits = store.search(query)
        except Exception as e:
            return {"error": f"learning search failed: {e}"}
        try:
            out = [h.to_dict() if isinstance(h, Learning) else h for h in (hits or [])]
        except Exception as e:
            return {"error": f"could not serialise learnings: {e}"}
        return {"learnings": out}

    def _forget_learning(learning_id: str) -> Dict[str, Any]:
        store, err = _need_store()
        if err:
            return err
        try:
            removed = store.remove(learning_id)
        except Exception as e:
            return {"error": f"could not remove learning: {e}"}
        return {"removed": bool(removed)}

    return {
        "learn_from_conversation": {
            "name": "learn_from_conversation",
            "func": _learn_from_conversation,
            "description": (
                "Extract durable learnings (preferences, facts, corrections) from a "
                "conversation and remember them. Returns how many were learned and "
                "their ids. Skips texts with nothing durable to learn."
            ),
            "schema": _schema(
                {
                    "text": {"type": "string", "description": "Conversation text to learn from."},
                    "source": {"type": "string", "description": "Where the text came from. Default \"\"."},
                },
                ["text"],
            ),
        },
        "recall_learned": {
            "name": "recall_learned",
            "func": _recall_learned,
            "description": (
                "Search the learnings remembered by learn_from_conversation. Returns "
                "matching learnings ordered by confidence."
            ),
            "schema": _schema(
                {"query": {"type": "string", "description": "Search query."}},
                ["query"],
            ),
        },
        "forget_learning": {
            "name": "forget_learning",
            "func": _forget_learning,
            "description": (
                "Permanently delete one remembered learning by its id. Returns "
                '{"removed": true} when something was deleted.'
            ),
            "schema": _schema(
                {"learning_id": {"type": "string", "description": "Id returned by learn_from_conversation."}},
                ["learning_id"],
            ),
        },
    }
