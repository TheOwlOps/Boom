from typing import Any, Dict, List, Optional
try:
    from agent.src.memory.state_db import StateDB
except ModuleNotFoundError:
    from memory.state_db import StateDB

class SessionSearchTool:
    """Long-term conversation recall over StateDB with FTS5, read, and scroll modes."""

    def __init__(self, state_db: Optional[StateDB] = None):
        self.state_db = state_db or StateDB(":memory:")

    def search(
        self,
        query: Optional[str] = None,
        session_id: Optional[str] = None,
        around_message_id: Optional[int] = None,
        events: bool = False,
        limit: int = 10
    ) -> Dict[str, Any]:
        # Mode 0: TIMELINE — what happened in a session, not what was said. The loop records a
        # turn's steps and errors here; this is the only read of that table.
        if events:
            timeline = self.state_db.read_timeline(session_id, limit=limit)
            return {"mode": "timeline", "session_id": session_id, "count": len(timeline),
                    "events": timeline}
        # Mode 1: SCROLL around anchor message
        if session_id and around_message_id:
            all_msgs = self.state_db.get_session_messages(session_id, limit=200)
            target_idx = -1
            for idx, m in enumerate(all_msgs):
                if m["id"] == around_message_id:
                    target_idx = idx
                    break

            if target_idx == -1:
                return {"mode": "scroll", "error": f"Message ID {around_message_id} not found", "messages": []}

            half = limit // 2
            start = max(0, target_idx - half)
            end = min(len(all_msgs), target_idx + half + 1)
            return {
                "mode": "scroll",
                "session_id": session_id,
                "anchor_id": around_message_id,
                "messages": all_msgs[start:end]
            }

        # Mode 2: READ session transcript
        if session_id and not query:
            msgs = self.state_db.get_session_messages(session_id, limit=limit)
            return {
                "mode": "read",
                "session_id": session_id,
                "count": len(msgs),
                "messages": msgs
            }

        # Mode 3: DISCOVERY across all sessions via FTS5
        if query:
            fts_rows = self.state_db.search_fts(query, limit=limit)
            return {
                "mode": "discovery",
                "query": query,
                "matches": len(fts_rows),
                "results": fts_rows
            }

        return {"mode": "none", "error": "Provide either query, session_id, or session_id + around_message_id"}
