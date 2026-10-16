"""Route table tách theo domain cho gateway HTTP API của Boom.

Gateway (``gateway/api.py``) dùng stdlib ``http.server`` thuần
(ThreadingHTTPServer + BaseHTTPRequestHandler), không có framework routing
như FastAPI/Starlette. Vì vậy mỗi module con export::

    ROUTES = [(method, path_pattern, handler_name), ...]   # list 3-tuple str
    register(router)                                       # gắn vào router

Package này gộp 6 modules thành một bảng duy nhất và cung cấp:

- ``ALL_ROUTES``    : list 4-tuple (module, method, path, handler).
- ``get_route_table()`` : dict {path: (module, methods)} — methods là tuple
  các HTTP method đã sắp xếp, phục vụ introspection/kiểm tra trùng lặp.
- ``RouteRegistry`` : registry tối giản cho http.server: ``add()`` để đăng ký,
  ``lookup(method, path)`` để tra cứu (method, path) -> (module, handler).
  Dùng để thay thế dần chuỗi if/elif trong ``_Handler.do_GET/do_POST``.

Backward-compat: toàn bộ path giữ nguyên y hệt ``api.py`` hiện tại
(9 endpoints dưới ``/api/*``); không thêm, không đổi, không bịa path mới.
"""

from . import admin, channels, chat, health, tools, websocket

# Thứ tự modules cố định để ALL_ROUTES deterministic.
MODULES = (health, chat, tools, channels, admin, websocket)

# Gộp ROUTES của 6 modules thành 4-tuple (module, method, path, handler).
ALL_ROUTES = []
for _mod in MODULES:
    _mod_name = _mod.__name__.rsplit(".", 1)[-1]
    for _method, _path, _handler in _mod.ROUTES:
        ALL_ROUTES.append((_mod_name, _method, _path, _handler))
del _mod, _mod_name, _method, _path, _handler


def get_route_table():
    """Trả về dict {path: (module, methods)}.

    ``module`` là tên module domain sở hữu path, ``methods`` là tuple các
    HTTP method (đã sort) mà path đó phục vụ. Ném ``ValueError`` nếu cùng
    một path bị khai báo ở 2 modules khác nhau (vi phạm tách domain).
    """
    table = {}
    for module, method, path, _handler in ALL_ROUTES:
        if path in table:
            owner = table[path][0]
            if owner != module:
                raise ValueError(
                    "path %r khai báo trùng ở 2 modules: %s và %s"
                    % (path, owner, module)
                )
            methods = set(table[path][1]) | {method}
            table[path] = (module, tuple(sorted(methods)))
        else:
            table[path] = (module, (method,))
    return table


class RouteRegistry:
    """Registry tối giản cho http.server: đăng ký và tra cứu route.

    Dùng như target của ``module.register(router)``; sau khi mọi module
    đã register, ``lookup(method, path)`` thay thế chuỗi if/elif trong
    ``_Handler`` mà không đổi hành vi.
    """

    def __init__(self):
        # key (method, path) -> (module, handler_name)
        self._routes = {}

    def add(self, method, path, handler, module):
        """Đăng ký một route. Trùng (method, path) -> ValueError."""
        key = (method.upper(), path)
        if key in self._routes:
            raise ValueError(
                "route trùng lặp: %s %s (đã đăng ký bởi %s)"
                % (method, path, self._routes[key][0])
            )
        self._routes[key] = (module, handler)

    def lookup(self, method, path):
        """Tra cứu (method, path) -> (module, handler_name) hoặc None."""
        return self._routes.get((method.upper(), path))

    def register_module(self, module):
        """Gọi ``module.register(self)`` — tiện ích gom một dòng."""
        module.register(self)

    def register_all(self):
        """Gọi register() của cả 6 modules theo thứ tự MODULES."""
        for module in MODULES:
            module.register(self)

    def __len__(self):
        return len(self._routes)

    def paths(self):
        """Set các path đã đăng ký (không tính method)."""
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
