"""Routes nhóm "channels" — trạng thái các kênh nhắn tin.

Nguồn gốc: ``gateway/api.py`` (nhánh ``do_GET`` cho ``/api/channels/status``,
gọi ``BoomAPI.channel_status()``).

Danh sách kênh cố định theo contract: telegram, discord, slack, whatsapp,
line, feishu, zalo, messenger. Trạng thái "connected" được hỏi thật từ
daemon qua ``can_deliver(channel)``; khi chưa cấu hình, ``detail`` nêu rõ
credential nào còn thiếu (không bịa trạng thái).

Lưu ý: gateway KHÔNG có endpoint ``/channels/send`` công khai trên HTTP API
(gửi tin đi qua daemon/channels adapters, không qua api.py). Module này chỉ
liệt kê route thật, không bịa thêm.

Backward-compat: giữ nguyên ``/api/channels/status`` (GET).
"""

# Mỗi entry: (HTTP method, path pattern, tên handler trong api.py).
ROUTES = [
    ("GET", "/api/channels/status", "handle_channels_status"),
]


def register(router):
    """Gắn mọi route của module này vào router (duck-typed)."""
    for method, path, handler in ROUTES:
        router.add(method, path, handler, __name__.rsplit(".", 1)[-1])
