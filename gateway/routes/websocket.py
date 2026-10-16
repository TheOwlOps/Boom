"""The "websocket" route group — placeholder for a realtime channel.

The FACT to state clearly: the gateway's HTTP API (``gateway/api.py``,
stdlib ``http.server``) currently does NOT expose any public ``/ws`` or
``/events`` endpoint. WebSocket exists only *internally* inside channel
adapters (e.g. ``gateway/channels/discord.py`` connects to Discord's
gateway) — not as HTTP routes.

The only realtime the HTTP API serves is SSE — but that is a mode of
``POST /api/chat`` (see ``routes/chat.py``), not a separate path.

So this module deliberately exports ``ROUTES = []``: it keeps its role
as a "domain module" for router splitting without inventing paths that
don't exist. When the gateway genuinely opens /ws later, just add an
entry to ROUTES below and ``get_route_table()`` will pick it up.
"""

# No public /ws or /events route on the gateway HTTP API yet.
ROUTES = []


def register(router):
    """No routes to attach — a deliberate no-op (see docstring)."""
    for method, path, handler in ROUTES:
        router.add(method, path, handler, __name__.rsplit(".", 1)[-1])
