"""Domain-split route table for Boom's gateway HTTP API.

The gateway (``gateway/api.py``) uses pure stdlib ``http.server``
(ThreadingHTTPServer + BaseHTTPRequestHandler) with no routing framework
like FastAPI/Starlette. So each submodule exports::

    ROUTES = [(method, path_pattern, handler_name), ...]   # list of str 3-tuples
    register(router)                                       # attach to router

This package merges the 6 modules into a single table and provides:

- ``ALL_ROUTES``    : list of 4-tuples (module, method, path, handler).
- ``get_route_table()`` : dict {path: (module, methods)} — methods is a
  tuple of sorted HTTP methods, used for introspection/duplicate checks.
- ``RouteRegistry`` : minimal registry for http.server: ``add()`` to
  register, ``lookup(method, path)`` to resolve (method, path) ->
  (module, handler). Gradually replaces the if/elif chains in
  ``_Handler.do_GET/do_POST``.

Backward-compat: all paths stay exactly as in current ``api.py``
(9 endpoints under ``/api/*``); nothing added, nothing changed, no
invented paths.
"""

from . import admin, channels, chat, health, tools, websocket

# Fixed module order so ALL_ROUTES is deterministic.
MODULES = (health, chat, tools, channels, admin, websocket)

# Merge the 6 modules' ROUTES into 4-tuples (module, method, path, handler).
ALL_ROUTES = []
for _mod in MODULES:
    _mod_name = _mod.__name__.rsplit(".", 1)[-1]
    for _method, _path, _handler in _mod.ROUTES:
        ALL_ROUTES.append((_mod_name, _method, _path, _handler))
del _mod, _mod_name, _method, _path, _handler


def get_route_table():
    """Return a dict {path: (module, methods)}.

    ``module`` is the name of the domain module owning the path, ``methods``
    is the tuple of (sorted) HTTP methods serving that path. Raises
    ``ValueError`` when the same path is declared in 2 different modules
    (a domain-splitting violation).
    """
    table = {}
    for module, method, path, _handler in ALL_ROUTES:
        if path in table:
            owner = table[path][0]
            if owner != module:
                raise ValueError(
                    "path %r declared in 2 modules: %s and %s"
                    % (path, owner, module)
                )
            methods = set(table[path][1]) | {method}
            table[path] = (module, tuple(sorted(methods)))
        else:
            table[path] = (module, (method,))
    return table


class RouteRegistry:
    """Minimal registry for http.server: route registration and lookup.

    Use as the target of ``module.register(router)``; once every module
    has registered, ``lookup(method, path)`` replaces the if/elif chains
    in ``_Handler`` without changing behavior.
    """

    def __init__(self):
        # key (method, path) -> (module, handler_name)
        self._routes = {}

    def add(self, method, path, handler, module):
        """Register one route. Duplicate (method, path) -> ValueError."""
        key = (method.upper(), path)
        if key in self._routes:
            raise ValueError(
                "duplicate route: %s %s (already registered by %s)"
                % (method, path, self._routes[key][0])
            )
        self._routes[key] = (module, handler)

    def lookup(self, method, path):
        """Look up (method, path) -> (module, handler_name) or None."""
        return self._routes.get((method.upper(), path))

    def register_module(self, module):
        """Call ``module.register(self)`` — a one-line convenience."""
        module.register(self)

    def register_all(self):
        """Call register() of all 6 modules in MODULES order."""
        for module in MODULES:
            module.register(self)

    def __len__(self):
        return len(self._routes)

    def paths(self):
        """The set of registered paths (method not counted)."""
        return {path for (_method, path) in self._routes}


__all__ = [
    "MODULES",
    "ALL_ROUTES",
    "RouteRegistry",
    "get_route_table",
    "admin",
    "channels",
    "chat",
    "health",
    "tools",
    "websocket",
]
