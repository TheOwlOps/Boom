"""Test cho route table tách domain (gateway-router-split).

Kiểm tra trên bảng route đã tách, KHÔNG đụng vào repo /tmp/boom_git
(không import api.py — repo là read-only và cần agent deps nặng).
Backward-compat được kiểm chứng bằng EXPECTED_PATHS: tập 9 endpoints thật
đọc trực tiếp từ ``gateway/api.py`` + ``gateway/openapi.json``
(commit bf171b9).

Chạy:  python3 test_routes.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from routes import (  # noqa: E402
    ALL_ROUTES,
    MODULES,
    RouteRegistry,
    admin,
    channels,
    chat,
    get_route_table,
    health,
    tools,
    websocket,
)

# 9 endpoints THẬT trích từ gateway/api.py (do_GET/do_POST) và openapi.json.
# Đây là "hợp đồng" backward-compat: bảng tách ra không được thêm/bớt/đổi.
EXPECTED = {
    # path: (module domain, methods)
    "/api/health": ("health", ("GET",)),
    "/api/logs": ("health", ("GET",)),
    "/api/chat": ("chat", ("POST",)),
    "/api/sessions": ("chat", ("GET",)),
    "/api/tools": ("tools", ("GET",)),
    "/api/tools/run": ("tools", ("POST",)),
    "/api/channels/status": ("channels", ("GET",)),
    "/api/skills": ("admin", ("GET",)),
    "/api/dag/status": ("admin", ("GET",)),
}
EXPECTED_HANDLER = {
    "/api/health": "handle_health",
    "/api/logs": "handle_logs",
    "/api/chat": "_handle_chat",
    "/api/sessions": "handle_sessions",
    "/api/tools": "handle_tools",
    "/api/tools/run": "_handle_tool_run",
    "/api/channels/status": "handle_channels_status",
    "/api/skills": "handle_skills",
    "/api/dag/status": "handle_dag_status",
}

passed = 0
failed = []


def check(cond, msg):
    """Ghi nhận 1 assertion: PASS tăng đếm, FAIL lưu lại để báo cuối."""
    global passed
    if cond:
        passed += 1
    else:
        failed.append(msg)


# ------------------------------------------------- 1. package import & MODULES
check(isinstance(MODULES, tuple), "MODULES phải là tuple")
check(len(MODULES) == 6, "phải có đúng 6 modules, thấy %d" % len(MODULES))
check(
    {m.__name__.rsplit(".", 1)[-1] for m in MODULES}
    == {"health", "chat", "tools", "channels", "admin", "websocket"},
    "tên 6 modules phải đúng: health/chat/tools/channels/admin/websocket",
)

# ------------------------------------------------- 2. ROUTES từng module
for mod in MODULES:
    name = mod.__name__.rsplit(".", 1)[-1]
    check(hasattr(mod, "ROUTES"), "%s: thiếu ROUTES" % name)
    check(isinstance(mod.ROUTES, list), "%s: ROUTES phải là list" % name)
    check(
        callable(getattr(mod, "register", None)),
        "%s: thiếu hàm register(router)" % name,
    )
    for entry in mod.ROUTES:
        check(
            isinstance(entry, tuple) and len(entry) == 3,
            "%s: entry %r phải là 3-tuple" % (name, entry),
        )
        method, path, handler = entry
        check(
            all(isinstance(x, str) for x in entry),
            "%s: entry %r phải toàn str" % (name, entry),
        )
        check(
            method in ("GET", "POST"),
            "%s: method %r phải là GET/POST" % (name, method),
        )
        check(
            path.startswith("/api/"),
            "%s: path %r phải nằm dưới /api/ (backward-compat)" % (name, path),
        )
        check(
            len(handler.strip()) > 0,
            "%s: handler_name của %s rỗng" % (name, path),
        )

# ------------------------------------------------- 3. module websocket trung thực
check(websocket.ROUTES == [], "websocket.ROUTES phải rỗng (gateway chưa có /ws)")

# ------------------------------------------------- 4. tổng số & không trùng lặp
check(len(ALL_ROUTES) == 9, "ALL_ROUTES phải có 9 entries, thấy %d" % len(ALL_ROUTES))
for entry in ALL_ROUTES:
    check(
        isinstance(entry, tuple) and len(entry) == 4,
        "ALL_ROUTES entry %r phải là 4-tuple (module,method,path,handler)"
        % (entry,),
    )
    check(
        all(isinstance(x, str) for x in entry),
        "ALL_ROUTES entry %r phải toàn str" % (entry,),
    )

pairs = [(m, p) for (mod, m, p, h) in ALL_ROUTES]
check(len(set(pairs)) == len(pairs), "không được trùng cặp (method, path)")
paths = [p for (_mod, _m, p, _h) in ALL_ROUTES]
check(len(set(paths)) == len(paths), "không được trùng path giữa các modules")

# ------------------------------------------------- 5. get_route_table()
table = get_route_table()
check(isinstance(table, dict), "get_route_table() phải trả về dict")
check(set(table.keys()) == set(EXPECTED.keys()),
      "route table phải đúng 9 path cũ, lệch: %s"
      % (set(table.keys()) ^ set(EXPECTED.keys())))
for path, (exp_module, exp_methods) in EXPECTED.items():
    check(path in table, "thiếu path %s trong route table" % path)
    module, methods = table[path]
    check(isinstance(module, str) and isinstance(methods, tuple),
          "%s: value phải là (module: str, methods: tuple)" % path)
    check(module == exp_module,
          "%s: module phải là %r, thấy %r" % (path, exp_module, module))
    check(methods == exp_methods,
          "%s: methods phải là %r, thấy %r" % (path, exp_methods, methods))
    check(all(isinstance(m, str) for m in methods),
          "%s: methods phải toàn str" % path)
    check(list(methods) == sorted(methods),
          "%s: methods phải đã sort" % path)

# health routes tồn tại (yêu cầu tường minh của task)
check("/api/health" in table, "health route /api/health phải tồn tại")
check("/api/logs" in table, "health route /api/logs phải tồn tại")
check(table["/api/health"][0] == "health", "/api/health thuộc module health")
check(table["/api/logs"][0] == "health", "/api/logs thuộc module health")

# ------------------------------------------------- 6. handler_name khớp api.py
for (mod, method, path, handler) in ALL_ROUTES:
    check(handler == EXPECTED_HANDLER[path],
          "%s %s: handler phải là %r (khớp api.py), thấy %r"
          % (method, path, EXPECTED_HANDLER[path], handler))

# ------------------------------------------------- 7. register(router) từng module
for mod in MODULES:
    name = mod.__name__.rsplit(".", 1)[-1]

    class _StubRouter:
        def __init__(self):
            self.calls = []

        def add(self, method, path, handler, module):
            self.calls.append((method, path, handler, module))

    stub = _StubRouter()
    mod.register(stub)  # không được raise
    check(len(stub.calls) == len(mod.ROUTES),
          "%s.register: phải gọi add đúng %d lần" % (name, len(mod.ROUTES)))
    for (method, path, handler, module) in stub.calls:
        check(module == name,
              "%s.register: module ghi nhận phải là %r" % (name, module))
    # websocket register là no-op có chủ ý
    if name == "websocket":
        check(stub.calls == [], "websocket.register phải là no-op")

# ------------------------------------------------- 8. RouteRegistry end-to-end
reg = RouteRegistry()
reg.register_all()
check(len(reg) == 9, "register_all: registry phải có 9 routes")
check(reg.paths() == set(EXPECTED.keys()),
      "registry.paths() phải đúng 9 path cũ")
for path, (exp_module, exp_methods) in EXPECTED.items():
    for method in exp_methods:
        hit = reg.lookup(method, path)
        check(hit is not None, "lookup(%s, %s) phải tìm thấy" % (method, path))
        check(hit[0] == exp_module,
              "lookup(%s, %s): module phải là %r" % (method, path, exp_module))
        check(hit[1] == EXPECTED_HANDLER[path],
              "lookup(%s, %s): handler phải là %r"
              % (method, path, EXPECTED_HANDLER[path]))
    # method sai -> không match (phân biệt GET/POST)
    wrong = "POST" if exp_methods == ("GET",) else "GET"
    check(reg.lookup(wrong, path) is None,
          "lookup(%s, %s) phải trả None (sai method)" % (wrong, path))

check(reg.lookup("GET", "/api/khong-ton-tai") is None,
      "lookup path lạ phải trả None")
check(reg.lookup("get", "/api/health") is not None,
      "lookup phải case-insensitive với method")

# đăng ký trùng -> ValueError
try:
    reg.add("GET", "/api/health", "handle_health", "health")
    check(False, "add trùng (method,path) phải raise ValueError")
except ValueError:
    check(True, "add trùng (method,path) raise ValueError")

# ------------------------------------------------- 9. báo cáo
print("PASS: %d assertions" % passed)
if failed:
    print("FAIL: %d" % len(failed))
    for msg in failed:
        print("  -", msg)
    sys.exit(1)
print("OK: tất cả assertions PASS, 0 failures")
