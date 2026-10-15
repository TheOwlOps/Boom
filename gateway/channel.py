"""Unified channel adapter interface + registry.

Every chat platform Boom speaks gets one thin adapter instead of a branch in a
giant if/else: `send_text`/`can_deliver` for the reply path, `verify_request` /
`handshake` / `normalize` for the ingress path, and optional message actions
(`react`, `edit_message`, `delete_message`) plus member moderation
(`kick_member`, `ban_member`) where the platform's API really has them.

An action a platform does not offer is never faked: the base implementation
answers `{"error": "... not supported"}` and the adapter simply does not
override it. There is intentionally no TikTok adapter — TikTok exposes no
public bot messaging API, and a stub would be decorative code.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx


class ChannelAdapter:
    """One platform. Subclass and override only what the platform's API offers."""

    name = "base"

    # ------------------------------------------------------------ http client
    # One shared AsyncClient per adapter instance (lazy): connections are
    # reused across calls instead of a fresh handshake every time. Adapters
    # must NOT close it after each call; close once via aclose() on shutdown.

    def _http(self, timeout: float = 35.0) -> httpx.AsyncClient:
        """The adapter's shared HTTP client, created lazily on first use."""
        client = getattr(self, "_client", None)
        if client is None:
            client = httpx.AsyncClient(
                timeout=timeout,
                limits=httpx.Limits(max_connections=20,
                                    max_keepalive_connections=10),
            )
            self._client = client
        return client

    async def aclose(self) -> None:
        """Close the shared client. Call once on shutdown; safe to call twice."""
        client = getattr(self, "_client", None)
        if client is not None:
            self._client = None
            await client.aclose()

    # ------------------------------------------------------------------ reply path

    async def send_text(self, target: str, text: str) -> bool:
        """Send plain text. Returns True when the platform accepted it."""
        raise NotImplementedError

    def can_deliver(self) -> bool:
        """True when this process holds whatever credential the send needs."""
        return False

    # ------------------------------------------------------------ message actions
    # Only override these when the platform documents the endpoint. Everything
    # else falls through to the honest {"error": ...} below.

    async def react(self, target: str, message_id: str, emoji: str) -> Dict[str, Any]:
        """Put a reaction (e.g. a heart) on a message. `target` is the chat or
        channel the message lives in, `message_id` the platform's id for it."""
        return {"error": f"{self.name}: reactions not supported by this platform"}

    async def edit_message(self, target: str, message_id: str, text: str) -> Dict[str, Any]:
        """Replace the text of a message the bot sent."""
        return {"error": f"{self.name}: editing messages not supported by this platform"}

    async def delete_message(self, target: str, message_id: str) -> Dict[str, Any]:
        """Delete a message the bot sent."""
        return {"error": f"{self.name}: deleting messages not supported by this platform"}

    async def recall_message(self, target: str, message_id: str) -> Dict[str, Any]:
        """Recall/unsend a message ("thu hồi" in Zalo terms).

        On Telegram/Discord/Slack this deletes the bot-sent message — deleting
        removes it for everyone, which is what recall means. Zalo OA and LINE
        expose no bot-accessible recall/unsend endpoint (verified against
        their public API docs), so those adapters override this with an
        honest {"error": ...} instead of a fabricated call."""
        return await self.delete_message(target, message_id)

    async def send_sticker(self, target: str, sticker: str) -> Dict[str, Any]:
        """Send a sticker to a chat. `sticker` is platform-native: a file_id or
        URL (Telegram), a sticker id (Discord, Zalo), "packageId:stickerId"
        (LINE), or a media id / URL (WhatsApp)."""
        return {"error": f"{self.name}: stickers not supported by this platform"}

    async def kick_member(self, target: str, user_id: str) -> Dict[str, Any]:
        """Remove a member from a group chat (they can rejoin afterwards).
        `target` is the group/channel, `user_id` the platform's id for the member.
        Admin-only on every platform that offers it: only call this on the
        user's explicit request. Platforms without the API keep the honest
        {"error": ...} below — never faked."""
        return {"error": f"{self.name}: kicking members not supported by this platform"}

    async def ban_member(self, target: str, user_id: str) -> Dict[str, Any]:
        """Ban a member permanently. Same admin-only rule as kick_member."""
        return {"error": f"{self.name}: banning members not supported by this platform"}

    async def get_group_info(self, target: str) -> Dict[str, Any]:
        """Fetch group/chat metadata: {id, name, member_count, type}.

        `target` is the platform's chat/group/channel id. Only override when
        the platform documents a bot-accessible endpoint — a group the bot is
        not a member of must surface the API's refusal, never a guess."""
        return {"error": f"{self.name}: group info not supported by this platform"}

    async def get_member_info(self, target: str, user_id: str) -> Dict[str, Any]:
        """Fetch one member's info inside a group: {id, name, role, ...}.

        HONESTY CONTRACT: a bot only sees members of groups it belongs to.
        A stranger the bot never interacted with is NOT lookable up on any
        platform — Telegram/Discord/Slack/LINE all answer 404/forbidden, and
        that refusal is reported as {"error": ...}, never fabricated."""
        return {"error": f"{self.name}: member info not supported by this platform"}

    # --------------------------------------------------------------- ingress path

    def verify_request(self, headers: Dict[str, str], body: bytes) -> Optional[str]:
        """Channel-native signature check. None means authentic *or* that this
        adapter has no secret configured — in the latter case the registry falls
        back to the shared ingress token, exactly like the old ingress did."""
        return None

    def has_verify_secret(self) -> bool:
        """True when a channel-native secret is configured (so the shared-token
        fallback must NOT apply)."""
        return False

    def handshake(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Answer a platform's endpoint-verification ping without running a turn."""
        return None

    def verify_webhook(self, params: Dict[str, str]) -> Optional[str]:
        """Answer a GET-style webhook verification (Messenger's hub.challenge).
        Returns the challenge string, or None when it does not verify."""
        return None

    def normalize(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Read the platform's webhook body as the canonical event
        (`sender_id`, `sender_name`, `text`, `message_id`, `target`), or return
        `{"ignore": reason}` / `{"error": reason}`. A bridge's canonical event
        passes through untouched."""
        return event


class ChannelRegistry:
    """Holds one adapter per channel and fans the daemon/ingress calls out."""

    def __init__(self) -> None:
        self._adapters: Dict[str, ChannelAdapter] = {}

    def register(self, adapter: ChannelAdapter) -> ChannelAdapter:
        self._adapters[adapter.name] = adapter
        return adapter

    def get(self, channel: str) -> Optional[ChannelAdapter]:
        return self._adapters.get(channel)

    def names(self) -> List[str]:
        return sorted(self._adapters)

    # -- reply path -------------------------------------------------------

    async def send_to(self, channel: str, target: str, text: str) -> bool:
        adapter = self._adapters.get(channel)
        if adapter is None:
            return False
        return bool(await adapter.send_text(target, text))

    def can_deliver(self, channel: str) -> bool:
        adapter = self._adapters.get(channel)
        return bool(adapter is not None and adapter.can_deliver())

    # -- actions -----------------------------------------------------------

    async def react(self, channel: str, target: str, message_id: str,
                    emoji: str) -> Dict[str, Any]:
        adapter = self._adapters.get(channel)
        if adapter is None:
            return {"error": f"unknown channel {channel!r}"}
        return await adapter.react(target, message_id, emoji)

    async def edit_message(self, channel: str, target: str, message_id: str,
                           text: str) -> Dict[str, Any]:
        adapter = self._adapters.get(channel)
        if adapter is None:
            return {"error": f"unknown channel {channel!r}"}
        return await adapter.edit_message(target, message_id, text)

    async def delete_message(self, channel: str, target: str,
                             message_id: str) -> Dict[str, Any]:
        adapter = self._adapters.get(channel)
        if adapter is None:
            return {"error": f"unknown channel {channel!r}"}
        return await adapter.delete_message(target, message_id)

    async def recall_message(self, channel: str, target: str,
                             message_id: str) -> Dict[str, Any]:
        adapter = self._adapters.get(channel)
        if adapter is None:
            return {"error": f"unknown channel {channel!r}"}
        return await adapter.recall_message(target, message_id)

    async def send_sticker(self, channel: str, target: str,
                           sticker: str) -> Dict[str, Any]:
        adapter = self._adapters.get(channel)
        if adapter is None:
            return {"error": f"unknown channel {channel!r}"}
        return await adapter.send_sticker(target, sticker)

    async def kick_member(self, channel: str, target: str,
                          user_id: str) -> Dict[str, Any]:
        adapter = self._adapters.get(channel)
        if adapter is None:
            return {"error": f"unknown channel {channel!r}"}
        return await adapter.kick_member(target, user_id)

    async def ban_member(self, channel: str, target: str,
                         user_id: str) -> Dict[str, Any]:
        adapter = self._adapters.get(channel)
        if adapter is None:
            return {"error": f"unknown channel {channel!r}"}
        return await adapter.ban_member(target, user_id)

    async def get_group_info(self, channel: str, target: str) -> Dict[str, Any]:
        adapter = self._adapters.get(channel)
        if adapter is None:
            return {"error": f"unknown channel {channel!r}"}
        return await adapter.get_group_info(target)

    async def get_member_info(self, channel: str, target: str,
                              user_id: str) -> Dict[str, Any]:
        adapter = self._adapters.get(channel)
        if adapter is None:
            return {"error": f"unknown channel {channel!r}"}
        return await adapter.get_member_info(target, user_id)

    # -- ingress path -------------------------------------------------------

    def verify(self, channel: str, headers: Dict[str, str], body: bytes,
               shared_token: str = "") -> Optional[str]:
        """None when the request is authentic, else why it is refused. A channel
        with its own signature secret configured needs that signature; every
        other channel needs the shared token when one is set."""
        adapter = self._adapters.get(channel)
        if adapter is not None:
            refusal = adapter.verify_request(headers, body)
            if refusal:
                return refusal
            if adapter.has_verify_secret():
                return None
        if shared_token and headers.get("x-boom-token") != shared_token:
            return "bad or missing X-Boom-Token"
        return None

    def handshake(self, channel: str, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        adapter = self._adapters.get(channel)
        return adapter.handshake(event) if adapter is not None else None

    def normalize(self, channel: str, event: Dict[str, Any]) -> Dict[str, Any]:
        adapter = self._adapters.get(channel)
        return adapter.normalize(event) if adapter is not None else event


def build_default_registry() -> ChannelRegistry:
    """All webhook/poll adapters. Discord is registered separately by the daemon
    because its adapter needs the live daemon for inbound messages."""
    from gateway.telegram import TelegramAdapter
    from gateway.slack import SlackAdapter
    from gateway.whatsapp import WhatsAppAdapter
    from gateway.line import LineAdapter
    from gateway.feishu import FeishuAdapter
    from gateway.zalo import ZaloAdapter
    from gateway.messenger import MessengerAdapter

    registry = ChannelRegistry()
    for adapter in (TelegramAdapter(), SlackAdapter(), WhatsAppAdapter(),
                    LineAdapter(), FeishuAdapter(), ZaloAdapter(),
                    MessengerAdapter()):
        registry.register(adapter)
    return registry


