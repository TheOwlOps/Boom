"""Routes nhóm "chat" — hội thoại với agent.

Nguồn gốc: ``gateway/api.py`` (``do_POST`` -> ``_handle_chat``,
``do_GET`` -> nhánh ``/api/sessions``).

Lưu ý trung thực: KHÔNG tồn tại path riêng ``/chat/stream`` trên gateway.
Stream SSE là một *chế độ* của chính ``POST /api/chat``: khi request gửi
header ``Accept: text/event-stream``, api.py rẽ sang ``_chat_sse`` và
emit các event ``status`` -> ``message`` -> ``done``. Vì vậy module này
không bịa thêm path mới — chỉ liệt kê 2 route thật.

Backward-compat: giữ nguyên ``/api/chat`` (POST) và ``/api/sessions`` (GET).
"""

# Mỗi entry: (HTTP method, path pattern, tên handler trong api.py).
ROUTES = [
    # _handle_chat: nhận {"prompt","strategy","session_id"}; trả JSON
    # hoặc SSE tùy header Accept. Strategy hợp lệ: auto/react/dag/mass.
    ("POST", "/api/chat", "_handle_chat"),
    # Nhánh do_GET /api/sessions: contract trả {"sessions": []}
    # (ReActLoop chưa có API liệt kê session — không bịa dữ liệu).
    ("GET", "/api/sessions", "handle_sessions"),
]

# Ghi chú: streaming SSE dùng chung path /api/chat (POST) với handler
# phụ _chat_sse, kích hoạt qua header Accept: text/event-stream.
SSE_HANDLER = "_chat_sse"
SSE_TRIGGER_HEADER = ("Accept", "text/event-stream")


def register(router):
    """Gắn mọi route của module này vào router (duck-typed)."""
    for method, path, handler in ROUTES:
        router.add(method, path, handler, __name__.rsplit(".", 1)[-1])
