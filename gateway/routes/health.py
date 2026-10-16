"""The "health" route group — system health (system observability).

Origin: ``gateway/api.py`` (the ``do_GET`` branch of ``_Handler``).
Gateway framework: stdlib ``http.server`` (ThreadingHTTPServer +
BaseHTTPRequestHandler) — no FastAPI/Starlette, so this module only
declares the route table, with no decorators.

Backward-compat: keep the legacy paths ``/api/health`` and ``/api/logs``.
"""

# Each entry: (HTTP method, path pattern, handler name in api.py).
# - /api/health : returns {"status","version","model","uptime_sec",
#                 "tools_count","skills_count"} (counts genuinely read from environment).
# - /api/logs   : reads the internal _LOG_BUFFER log buffer (?limit=, max 200).
ROUTES = [
    ("GET", "/api/health", "handle_health"),
    ("GET", "/api/logs", "handle_logs"),
]


def register(router):
    """Attach every route of this module to the router (duck-typed).

    ``router`` only needs an ``add(method, path, handler, module)`` method.
    """
    for method, path, handler in ROUTES:
        router.add(method, path, handler, __name__.rsplit(".", 1)[-1])
