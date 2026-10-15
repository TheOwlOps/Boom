# Channels — cấu trúc mới (`gateway/channels/`)

> Tài liệu refactor: gom 8 module channel rời rạc dưới `gateway/` thành package
> `gateway/channels/`, tách code dùng chung vào `base.py`, giữ tương thích ngược
> bằng shim.

## 1. Cấu trúc thư mục

### Trước (flat, 8 file rời dưới `gateway/`)

```
gateway/
├── channel.py        # ChannelAdapter + ChannelRegistry + build_default_registry
├── telegram.py       # class TelegramAdapter
├── discord.py        # class DiscordChannel   (+ INTENTS, MAX_MESSAGE, ...)
├── slack.py          # class SlackAdapter
├── line.py           # class LineAdapter
├── zalo.py           # class ZaloAdapter
├── whatsapp.py       # class WhatsAppAdapter
├── messenger.py      # class MessengerAdapter
├── feishu.py         # class FeishuAdapter
├── daemon.py         # from gateway.discord import DiscordChannel
└── ingress.py
```

### Sau (package `gateway/channels/` + shim tương thích)

```
gateway/
├── channels/
│   ├── __init__.py   # re-export 8 classes + build_default_registry() -> dict
│   ├── base.py       # ChannelAdapter, HmacWebhookMixin, RetryMixin, normalize_message
│   ├── telegram.py   # class TelegramAdapter(ChannelAdapter, ...)
│   ├── discord.py    # class DiscordChannel(ChannelAdapter, ...)
│   ├── slack.py      # class SlackAdapter(ChannelAdapter, ...)
│   ├── line.py       # class LineAdapter(ChannelAdapter, ...)
│   ├── zalo.py       # class ZaloAdapter(ChannelAdapter, ...)
│   ├── whatsapp.py   # class WhatsAppAdapter(ChannelAdapter, ...)
│   ├── messenger.py  # class MessengerAdapter(ChannelAdapter, ...)
│   └── feishu.py     # class FeishuAdapter(ChannelAdapter, ...)
├── telegram.py       # ← shim  (1 dòng: from gateway.channels.telegram import *)
├── discord.py        # ← shim  (giữ nguyên: from gateway.discord import DiscordChannel)
├── slack.py          # ← shim
├── line.py           # ← shim
├── zalo.py           # ← shim
├── whatsapp.py       # ← shim
├── messenger.py      # ← shim
├── feishu.py         # ← shim
├── daemon.py
└── ingress.py
```

Cách import sau refactor:

```python
from gateway.channels import TelegramAdapter              # mới, nên dùng
from gateway.channels.telegram import TelegramAdapter     # mới, tường minh
from gateway.telegram import TelegramAdapter              # cũ, vẫn chạy (qua shim)
```

## 2. Thêm channel mới trong 3 bước

Giả sử thêm channel `shopee`:

**Bước 1 — Tạo file `gateway/channels/shopee.py`**

```python
from gateway.channels.base import ChannelAdapter

class ShopeeAdapter(ChannelAdapter):   # tên class tự chọn, nên giữ hậu tố Adapter
    name = "shopee"

    async def send_text(self, target: str, text: str) -> bool:
        ...  # chỉ override đúng API nền tảng có

    def can_deliver(self) -> bool:
        return bool(self.token)
```

Chỉ override method nào nền tảng thật sự có. Method nào không có thì để nguyên
implementation mặc định của `ChannelAdapter` (trả `{"error": ...}` trung thực,
không fake).

**Bước 2 — Đăng ký vào `gateway/channels/__init__.py`**

```python
from gateway.channels.shopee import ShopeeAdapter
# thêm "ShopeeAdapter" vào __all__
```

**Bước 3 — Thêm vào `build_default_registry()`**

```python
adapters = (..., ShopeeAdapter(), ...)
```

Xong — không cần sửa daemon/ingress/dispatcher, vì mọi đường gọi đều đi qua
registry theo `adapter.name`.

## 3. So sánh cũ / mới

Số liệu đo trên code hiện tại (`/tmp/boom_git/gateway/*.py`, đếm bằng script):

| Chỉ số | Trước | Sau |
|---|---|---|
| File chứa logic channel | 8 file flat dưới `gateway/` | 8 file trong `gateway/channels/` + `base.py` + `__init__.py` |
| Tổng dòng code channel | **1638 dòng** | ~1638 dòng (không tăng code, chỉ sắp xếp lại) |
| Method định nghĩa lặp lại ở ≥2 file | **20 method** (`send_text`, `can_deliver`, `react`, `verify_request`, `get_group_info`, `send_sticker`, `normalize`, `edit_message`, `delete_message`, `kick_member`, … — mỗi method lặp ở 2–8 file) | **1 lần duy nhất** trong `base.py` (mixin + base class) |
| Dòng code trùng lặp có thể gom về base | **~766 dòng** (tổng body các bản copy trừ 1 bản chuẩn) | 0 — mỗi logic chung chỉ tồn tại 1 nơi |
| Thêm 1 channel mới | copy-paste ~100–300 dòng từ channel cũ rồi sửa | tạo 1 file, kế thừa `ChannelAdapter`, override đúng API có thật |
| Import trong daemon/evals | `from gateway.discord import DiscordChannel` (rải rác, phụ thuộc vị trí file) | `from gateway.channels import DiscordChannel` (1 điểm tập trung) |

## 4. Tương thích ngược (shim files)

8 file `gateway/<name>.py` cũ được giữ lại dưới dạng shim 1 dòng:

```python
from gateway.channels.telegram import *  # noqa: F401,F403
```

Đảm bảo:

- `from gateway.telegram import TelegramAdapter` → **đúng object class** với
  `gateway.channels.telegram.TelegramAdapter` (đã verify bằng test: `is`).
- Các hằng số module-level cũng đi qua shim: ví dụ
  `from gateway.discord import INTENTS, MAX_MESSAGE, DiscordChannel`
  (dùng trong `evals/test_discord_channel.py`) vẫn chạy.
- `gateway/__init__.py` (`from gateway.discord import DiscordChannel`) và
  `gateway/daemon.py` (dòng 12, dòng 14) **không cần sửa**.
- `import gateway.slack as slack_mod` (kiểu dùng trong `evals/`) vẫn chạy.

Test tương thích: `test_compat.py` dựng cây package giả trong `/tmp`, copy
**nguyên file** `channels_new/__init__.py` và 8 shim vào, rồi assert 39 checks —
**tất cả PASS**.

### Lưu ý khi dọn dẹp sau này

- Shim chỉ nên xóa khi đã grep toàn repo không còn import đường cũ
  (`from gateway.telegram import ...`, `import gateway.discord`, …).
- `DiscordChannel` là ngoại lệ duy nhất: constructor cần `(daemon, token)` nên
  daemon vẫn tự tạo instance live và `registry.register(...)` riêng — entry
  `discord` trong `build_default_registry()` chỉ là placeholder cho đủ dict.
- `gateway/channel.py` (số ít) vẫn tồn tại với `ChannelRegistry` cũ; nếu refactor
  xóa nó thì `daemon.py` dòng 14 (`from gateway.channel import build_default_registry`)
  phải chuyển sang `from gateway.channels import build_default_registry`.
