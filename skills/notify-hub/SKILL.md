---
name: notify-hub
description: Gửi thông báo đa kênh qua webhook URL từ env.
version: 1.0.0
author: boom-overnight
license: MIT
platforms: [linux, macos]
tags: [notification, telegram, discord, slack, webhook]
category: communication
---

# Notify Hub

Gửi một thông điệp tới nhiều kênh cùng lúc. Mỗi kênh đọc webhook URL từ
biến môi trường, kênh nào thiếu cấu hình hoặc lỗi sẽ fail graceful (ghi vào
kết quả, không raise, không chặn kênh khác):

| Kênh       | Env var              | Ghi chú                    |
|------------|----------------------|----------------------------|
| telegram   | `TELEGRAM_WEBHOOK`   | URL webhook bot Telegram   |
| discord    | `DISCORD_WEBHOOK`    | Discord incoming webhook   |
| slack      | `SLACK_WEBHOOK`      | Slack incoming webhook     |
| webhook    | `GENERIC_WEBHOOK`    | Webhook tuỳ ý, JSON `{"text": ...}` |

## Cách dùng

```python
from notify import send, broadcast

# Một kênh — trả về dict {ok, channel, error}
send("telegram", "Backup xong lúc 02:00")

# Tất cả kênh — kênh lỗi vẫn trả kết quả, không raise
results = broadcast("Deploy thành công", channels=["telegram", "discord", "slack"])
```

`broadcast` hữu ích sau các cron job dài: báo trạng thái dù chỉ một kênh
cấu hình thành công. Đừng hardcode webhook URL trong code — luôn qua env.
