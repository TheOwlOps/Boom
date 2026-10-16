"""The "tools" route group — listing and trial-running tools.

Origin: ``gateway/api.py`` (the ``do_GET`` branch for ``/api/tools``,
``do_POST`` -> ``_handle_tool_run``).

Backward-compat: keep ``/api/tools`` (GET) and ``/api/tools/run`` (POST).
"""

# Each entry: (HTTP method, path pattern, handler name in api.py).
ROUTES = [
    # List real tools from AgentEnvironment: {"tools":[{name,
    # description, group}], "count": n}.
    ("GET", "/api/tools", "handle_tools"),
    # _handle_tool_run: accepts {"tool","args"}; runs via SandboxedToolRunner
    # when importable, otherwise calls directly. Returns {"ok","result"|"error"}.
    ("POST", "/api/tools/run", "_handle_tool_run"),
]


def register(router):
    """Attach every route of this module to the router (duck-typed)."""
    for method, path, handler in ROUTES:
        router.add(method, path, handler, __name__.rsplit(".", 1)[-1])
