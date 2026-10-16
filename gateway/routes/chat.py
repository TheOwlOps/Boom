"""The "chat" route group — conversations with the agent.

Origin: ``gateway/api.py`` (``do_POST`` -> ``_handle_chat``,
``do_GET`` -> the ``/api/sessions`` branch).

Honesty note: there is NO separate ``/chat/stream`` path on the
gateway. SSE streaming is a *mode* of ``POST /api/chat`` itself: when
the request carries header ``Accept: text/event-stream``, api.py
branches to ``_chat_sse`` and emits ``status`` -> ``message`` ->
``done`` events. So this module invents no new path — it only lists the
2 real routes.

Backward-compat: keep ``/api/chat`` (POST) and ``/api/sessions`` (GET).
"""

# Each entry: (HTTP method, path pattern, handler name in api.py).
ROUTES = [
    # _handle_chat: accepts {"prompt","strategy","session_id"}; returns JSON
    # or SSE depending on the Accept header. Valid strategies: auto/react/dag/mass.
    ("POST", "/api/chat", "_handle_chat"),
    # do_GET /api/sessions branch: contract returns {"sessions": []}
    # (ReActLoop has no session-listing API — no invented data).
    ("GET", "/api/sessions", "handle_sessions"),
]

# Note: SSE streaming shares the /api/chat (POST) path with the auxiliary
# _chat_sse handler, triggered via header Accept: text/event-stream.
SSE_HANDLER = "_chat_sse"
SSE_TRIGGER_HEADER = ("Accept", "text/event-stream")


def register(router):
    """Attach every route of this module to the router (duck-typed)."""
    for method, path, handler in ROUTES:
        router.add(method, path, handler, __name__.rsplit(".", 1)[-1])
