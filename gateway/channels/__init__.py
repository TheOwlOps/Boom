"""`gateway.channels` — one module per chat platform.

New home for the channel adapters that used to live as flat modules directly
under `gateway/` (`gateway.telegram`, `gateway.discord`, ...). Import paths:

    from gateway.channels import TelegramAdapter            # preferred
    from gateway.channels.telegram import TelegramAdapter   # explicit
    from gateway.telegram import TelegramAdapter            # old path, works via shim

The old flat modules are kept as one-line shims (`gateway/<name>.py`) so
existing imports keep working; remove the shims in a later cleanup once every
consumer has moved to `gateway.channels`.
"""

from __future__ import annotations

from typing import Dict

from gateway.channels.base import (
    ChannelAdapter,
    HmacWebhookMixin,
    RetryMixin,
    normalize_message,
)
from gateway.channels.telegram import TelegramAdapter
from gateway.channels.discord import DiscordChannel
from gateway.channels.slack import SlackAdapter
from gateway.channels.line import LineAdapter
from gateway.channels.zalo import ZaloAdapter
from gateway.channels.whatsapp import WhatsAppAdapter
from gateway.channels.messenger import MessengerAdapter
from gateway.channels.feishu import FeishuAdapter

__all__ = [
    "ChannelAdapter",
    "HmacWebhookMixin",
    "RetryMixin",
    "normalize_message",
    "TelegramAdapter",
    "DiscordChannel",
    "SlackAdapter",
    "LineAdapter",
    "ZaloAdapter",
    "WhatsAppAdapter",
    "MessengerAdapter",
    "FeishuAdapter",
    "build_default_registry",
]

# NOTE: class names are NOT uniform across platforms — they are the real names
# from the original flat modules (kept verbatim so moved code keeps working):
#   telegram.py -> TelegramAdapter      discord.py -> DiscordChannel
#   slack.py    -> SlackAdapter         line.py    -> LineAdapter
#   zalo.py     -> ZaloAdapter          whatsapp.py-> WhatsAppAdapter
#   messenger.py-> MessengerAdapter     feishu.py  -> FeishuAdapter


def build_default_registry() -> Dict[str, ChannelAdapter]:
    """One adapter instance per channel, keyed by ``adapter.name``.

    Mirrors the logic of the old ``gateway.channel.build_default_registry``:
    every adapter is constructed with its default (env-reading) constructor.
    Discord is the exception — ``DiscordChannel`` needs the live daemon for
    inbound messages, so the daemon registers it separately
    (``GatewayDaemon`` does ``registry.register(DiscordChannel(self, token))``);
    the entry here is a placeholder instance so the dict is complete.
    """
    adapters = (
        TelegramAdapter(),
        DiscordChannel(None, ""),  # placeholder: daemon re-registers the live one
        SlackAdapter(),
        LineAdapter(),
        ZaloAdapter(),
        WhatsAppAdapter(),
        MessengerAdapter(),
        FeishuAdapter(),
    )
    return {adapter.name: adapter for adapter in adapters}
