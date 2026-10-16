"""Routes nhóm "tools" — liệt kê và chạy thử tools.

Nguồn gốc: ``gateway/api.py`` (nhánh ``do_GET`` cho ``/api/tools``,
``do_POST`` -> ``_handle_tool_run``).

Backward-compat: giữ nguyên ``/api/tools`` (GET) và ``/api/tools/run`` (POST).
"""

# Mỗi entry: (HTTP method, path pattern, tên handler trong api.py).
ROUTES = [
    # Liệt kê tools thật từ AgentEnvironment: {"tools":[{name,
    # description, group}], "count": n}.
    ("GET", "/api/tools", "handle_tools"),
    # _handle_tool_run: nhận {"tool","args"}; chạy qua SandboxedToolRunner
    # nếu import được, ngược lại gọi trực tiếp. Trả {"ok","result"|"error"}.
    ("POST", "/api/tools/run", "_handle_tool_run"),
]


def register(router):
    """Gắn mọi route của module này vào router (duck-typed)."""
    for method, path, handler in ROUTES:
        router.add(method, path, handler, __name__.rsplit(".", 1)[-1])
