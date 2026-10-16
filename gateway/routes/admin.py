"""Routes nhóm "admin" — introspection nội bộ (skills, DAG).

Nguồn gốc: ``gateway/api.py`` (nhánh ``do_GET`` cho ``/api/skills`` và
``/api/dag/status``).

Lưu ý: gateway KHÔNG có namespace ``/admin/*`` hay ``/config`` trên HTTP API
hiện tại. Hai route introspection sẵn có được gom vào module admin vì chúng
phục vụ quản trị/giám sát agent, không thuộc luồng chat/tools/channels.

Backward-compat: giữ nguyên ``/api/skills`` và ``/api/dag/status`` (GET).
"""

# Mỗi entry: (HTTP method, path pattern, tên handler trong api.py).
ROUTES = [
    # Quét thư mục skills/ thật của repo: {"skills":[{name,description}],
    # "count": n}. Mô tả lấy từ dòng đầu của SKILL.md.
    ("GET", "/api/skills", "handle_skills"),
    # BoomAPI.dag_status(): {"nodes": [...]} — hôm nay Brain chưa lưu
    # last-run nên trả [] trung thực; nếu Brain tương lai expose
    # last_result thì surface theo đúng contract.
    ("GET", "/api/dag/status", "handle_dag_status"),
]


def register(router):
    """Gắn mọi route của module này vào router (duck-typed)."""
    for method, path, handler in ROUTES:
        router.add(method, path, handler, __name__.rsplit(".", 1)[-1])
