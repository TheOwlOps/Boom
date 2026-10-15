"""Unified channel adapter interface + registry.

Every chat platform Boom speaks gets one thin adapter instead of a branch in a
giant if/else: `send_text`/`can_deliver` for the reply path, `verify_request` /
`handshake` / `normalize` for the ingress path, and optional message actions
(`react`, `edit_message`, `delete_message`) where the platform's API really has
them.

An action a platform does not offer is never faked: the base implementation
answers `{"error": "... not supported"}` and the adapter simply does not
override it. There is intentionally no TikTok adapter — TikTok exposes no
public bot messaging API, and a stub would be decorative code.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class ChannelAdapter:
    """One platform. Subclass and override only what the platform's API offers."""

    name = "base"

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


