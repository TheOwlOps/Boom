# Channels — new layout (`gateway/channels/`)

> Refactor notes: consolidated 8 scattered channel modules under `gateway/` into the
> `gateway/channels/` package, extracted shared code into `base.py`, and kept
> backward compatibility via shims.

## 1. Directory layout

### Before (flat, 8 scattered files under `gateway/`)

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

### After (the `gateway/channels/` package + compatibility shims)

```
gateway/
├── channels/
│   ├── __init__.py   # re-exports 8 classes + build_default_registry() -> dict
│   ├── base.py       # ChannelAdapter, HmacWebhookMixin, RetryMixin, normalize_message
│   ├── telegram.py   # class TelegramAdapter(ChannelAdapter, ...)
│   ├── discord.py    # class DiscordChannel(ChannelAdapter, ...)
│   ├── slack.py      # class SlackAdapter(ChannelAdapter, ...)
│   ├── line.py       # class LineAdapter(ChannelAdapter, ...)
│   ├── zalo.py       # class ZaloAdapter(ChannelAdapter, ...)
│   ├── whatsapp.py   # class WhatsAppAdapter(ChannelAdapter, ...)
│   ├── messenger.py  # class MessengerAdapter(ChannelAdapter, ...)
│   └── feishu.py     # class FeishuAdapter(ChannelAdapter, ...)
├── telegram.py       # ← shim  (one line: from gateway.channels.telegram import *)
├── discord.py        # ← shim  (unchanged: from gateway.discord import DiscordChannel)
├── slack.py          # ← shim
├── line.py           # ← shim
├── zalo.py           # ← shim
├── whatsapp.py       # ← shim
├── messenger.py      # ← shim
├── feishu.py         # ← shim
├── daemon.py
└── ingress.py
```

Import styles after the refactor:

```python
from gateway.channels import TelegramAdapter              # new, preferred
from gateway.channels.telegram import TelegramAdapter     # new, explicit
from gateway.telegram import TelegramAdapter              # old, still works (via shim)
```

## 2. Adding a new channel in 3 steps

Suppose we're adding a `shopee` channel:

**Step 1 — Create `gateway/channels/shopee.py`**

```python
from gateway.channels.base import ChannelAdapter

class ShopeeAdapter(ChannelAdapter):   # class name is free, keeping the Adapter suffix is recommended
    name = "shopee"

    async def send_text(self, target: str, text: str) -> bool:
        ...  # only override what the platform's API actually has

    def can_deliver(self) -> bool:
        return bool(self.token)
```

Only override methods the platform really has. Leave everything else at
`ChannelAdapter`'s defaults (they return an honest `{"error": ...}`,
no faking).

**Step 2 — Register in `gateway/channels/__init__.py`**

```python
from gateway.channels.shopee import ShopeeAdapter
# add "ShopeeAdapter" to __all__
```

**Step 3 — Add to `build_default_registry()`**

```python
adapters = (..., ShopeeAdapter(), ...)
```

Done — no need to touch daemon/ingress/dispatcher, since all calls go through
the registry by `adapter.name`.

## 3. Old vs. new

Numbers measured on the actual code (`/tmp/boom_git/gateway/*.py`, counted with a script):

| Metric | Before | After |
|---|---|---|
| Files holding channel logic | 8 flat files under `gateway/` | 8 files in `gateway/channels/` + `base.py` + `__init__.py` |
| Total channel lines of code | **1638 lines** | ~1638 lines (no new code, just rearranged) |
| Methods defined in ≥2 files | **20 methods** (`send_text`, `can_deliver`, `react`, `verify_request`, `get_group_info`, `send_sticker`, `normalize`, `edit_message`, `delete_message`, `kick_member`, … — each duplicated in 2–8 files) | **exactly once** in `base.py` (mixins + base class) |
| Duplicated lines foldable into base | **~766 lines** (sum of copy bodies minus one canonical copy) | 0 — each shared logic exists in exactly one place |
| Adding 1 new channel | copy-paste ~100–300 lines from an old channel, then edit | create 1 file, inherit `ChannelAdapter`, override only APIs that really exist |
| Imports in daemon/evals | `from gateway.discord import DiscordChannel` (scattered, file-location dependent) | `from gateway.channels import DiscordChannel` (single entry point) |

## 4. Backward compatibility (shim files)

The 8 old `gateway/<name>.py` files remain as one-line shims:

```python
from gateway.channels.telegram import *  # noqa: F401,F403
```

Guarantees:

- `from gateway.telegram import TelegramAdapter` → the **same class object** as
  `gateway.channels.telegram.TelegramAdapter` (verified by test: `is`).
- Module-level constants flow through the shims too: e.g.
  `from gateway.discord import INTENTS, MAX_MESSAGE, DiscordChannel`
  (used in `evals/test_discord_channel.py`) still works.
- `gateway/__init__.py` (`from gateway.discord import DiscordChannel`) and
  `gateway/daemon.py` (lines 12, 14) **need no changes**.
- `import gateway.slack as slack_mod` (the style used in `evals/`) still works.

Compatibility test: `test_compat.py` builds a fake package tree in `/tmp`, copies
`channels_new/__init__.py` and the 8 shims **verbatim**, then asserts 39 checks —
**all PASS**.

### Cleanup notes for later

- Only delete the shims after grepping the whole repo for the old import paths
  (`from gateway.telegram import ...`, `import gateway.discord`, …) and finding none.
- `DiscordChannel` is the sole exception: its constructor needs `(daemon, token)`, so
  the daemon still creates the live instance and calls `registry.register(...)` itself —
  the `discord` entry in `build_default_registry()` is only a placeholder to complete the dict.
- `gateway/channel.py` (singular) still exists with the old `ChannelRegistry`; if a future
  refactor deletes it, `daemon.py` line 14 (`from gateway.channel import build_default_registry`)
  must move to `from gateway.channels import build_default_registry`.
