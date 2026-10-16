"""Routes nhóm "health" — tình trạng hệ thống (system observability).

Nguồn gốc: ``gateway/api.py`` (nhánh ``do_GET`` của ``_Handler``).
Framework gateway: stdlib ``http.server`` (ThreadingHTTPServer +
BaseHTTPRequestHandler) — không có FastAPI/Starlette, nên module này
chỉ khai báo bảng route, không gắn decorator.

Backward-compat: giữ nguyên path cũ ``/api/health`` và ``/api/logs``.
"""

# Mỗi entry: (HTTP method, path pattern, tên handler trong api.py).
# - /api/health : trả về {"status","version","model","uptime_sec",
#                 "tools_count","skills_count"} (đếm thật từ environment).
# - /api/logs   : đọc buffer log nội bộ _LOG_BUFFER (?limit=, tối đa 200).
ROUTES = [
    ("GET", "/api/health", "handle_health"),
    ("GET", "/api/logs", "handle_logs"),
]


def register(router):
    """Gắn mọi route của module này vào router (duck-typed).

    ``router`` chỉ cần có phương thức ``add(method, path, handler, module)``.
    """
    for method, path, handler in ROUTES:
        router.add(method, path, handler, __name__.rsplit(".", 1)[-1])
