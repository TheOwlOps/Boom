"""The "channels" route group — messaging channel status.

Origin: ``gateway/api.py`` (the ``do_GET`` branch for
``/api/channels/status``, calling ``BoomAPI.channel_status()``).

Fixed channel list per contract: telegram, discord, slack, whatsapp,
line, feishu, zalo, messenger. The "connected" state is genuinely
queried from the daemon via ``can_deliver(channel)``; when unconfigured,
``detail`` states exactly which credential is missing (no invented
statuses).

Note: the gateway has NO public ``/channels/send`` endpoint on the HTTP
API (outbound messages go through the daemon/channel adapters, not
api.py). This module only lists real routes — nothing invented.

Backward-compat: keep ``/api/channels/status`` (GET).
"""

# Each entry: (HTTP method, path pattern, handler name in api.py).
ROUTES = [
    ("GET", "/api/channels/status", "handle_channels_status"),
]


def register(router):
    """Attach every route of this module to the router (duck-typed)."""
    for method, path, handler in ROUTES:
        router.add(method, path, handler, __name__.rsplit(".", 1)[-1])
