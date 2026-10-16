"""Routes nhóm "websocket" — giữ chỗ cho kênh realtime.

SỰ THẬT cần ghi rõ: HTTP API của gateway (``gateway/api.py``, stdlib
``http.server``) hiện KHÔNG expose bất kỳ endpoint ``/ws`` hay ``/events``
công khai nào. WebSocket chỉ tồn tại *nội bộ* trong các channel adapter
(ví dụ ``gateway/channels/discord.py`` nối gateway Discord), không phải
route HTTP.

Realtime duy nhất mà HTTP API phục vụ là SSE — nhưng đó là một chế độ của
``POST /api/chat`` (xem ``routes/chat.py``), không phải path riêng.

Vì vậy module này export ``ROUTES = []`` một cách có chủ ý: giữ đúng vai trò
"domain module" cho việc tách router, nhưng không bịa path không tồn tại.
Khi gateway sau này mở /ws thật, chỉ cần thêm entry vào ROUTES bên dưới và
``get_route_table()`` sẽ tự nhận.
"""

# Chưa có route /ws hay /events công khai trên gateway HTTP API.
ROUTES = []


def register(router):
    """Không có route để gắn — giữ no-op có chủ ý (xem docstring)."""
    for method, path, handler in ROUTES:
        router.add(method, path, handler, __name__.rsplit(".", 1)[-1])
