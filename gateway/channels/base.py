"""Shared base class for all Boom channel adapters.

This file is extracted from `gateway/channel.py` (keeping 100% of the
:class:`ChannelAdapter` logic) plus the shared building blocks every child
adapter needs:

- :class:`HmacWebhookMixin` — verify webhook signatures with HMAC.
- :func:`normalize_message` — extract {text, sender_id, chat_id, timestamp}
  from raw payloads of many different platforms.
- :class:`RetryMixin` — retry with exponential backoff on network errors.

Child adapters look like::

    class TelegramAdapter(ChannelAdapter, HmacWebhookMixin, RetryMixin):
        ...

Stdlib + httpx only, no new dependencies.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

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


class HmacWebhookMixin:
    """Verify the HMAC signature of a webhook request.

    This mixin lets every adapter (LINE, Feishu, Zalo, ...) share one
    signature-check function instead of reimplementing it. Always returns
    bool, never raises on invalid input — a bad request is simply
    "not authenticated".
    """

    @staticmethod
    def verify_hmac_signature(secret: str, payload: bytes, signature: str,
                              algorithm: str = "sha256") -> bool:
        """Verify the HMAC signature of `payload` against `signature`.

        - ``signature`` accepts 2 forms: ``"sha256=<hex>"`` (LINE/GitHub
          style) or a bare hex string (Feishu/Zalo style).
        - Comparison uses :func:`hmac.compare_digest` to prevent timing attacks.
        - Returns ``False`` (never raises) when any input is invalid.
        """
        try:
            if not isinstance(secret, str) or not secret:
                return False
            if not isinstance(payload, (bytes, bytearray)) or not payload:
                return False
            if not isinstance(signature, str) or not signature:
                return False
            algo = algorithm.strip().lower().replace("-", "")
            if algo not in hashlib.algorithms_available:
                return False

            sig = signature.strip()
            if "=" in sig:
                algo_name, _, sig = sig.partition("=")
                if algo_name.strip().lower().replace("-", "") != algo:
                    return False

            expected = hmac.new(secret.encode("utf-8"), bytes(payload), algo).hexdigest()
            return hmac.compare_digest(expected, sig.strip().lower())
        except Exception:
            return False


def _first_not_none(*values: Any) -> Any:
    """Return the first non-None value in the list."""
    for value in values:
        if value is not None:
            return value
    return None


def _sender_id_from(value: Any) -> Optional[str]:
    """Extract sender_id from a dict/str-shaped sender object."""
    if isinstance(value, dict):
        return _first_not_none(value.get("id"), value.get("sender_id"))
    if isinstance(value, str):
        return value
    return None


def normalize_message(raw: dict) -> dict:
    """Extract ``{text, sender_id, chat_id, timestamp}`` from a raw payload.

    Handles many different payload shapes across platforms:

    - ``{"text": ..., "sender_id": ..., "chat_id": ..., "timestamp": ...}``
      (flat shape)
    - ``{"message": {"text": ..., ...}}`` (Telegram style)
    - ``{"content": ...}`` (some internal chat platforms' style)

    Missing fields → ``None``. Non-dict input (or an empty payload)
    never crashes, still returns a dict with ``None`` values.
    """
    empty = {"text": None, "sender_id": None, "chat_id": None, "timestamp": None}
    if not isinstance(raw, dict):
        return dict(empty)

    message = raw.get("message")
    if not isinstance(message, dict):
        message = {}

    chat = raw.get("chat")
    if not isinstance(chat, dict):
        chat = {}

    text = _first_not_none(
        raw.get("text"), message.get("text"),
        raw.get("content"), message.get("content"),
    )
    sender_id = _first_not_none(
        raw.get("sender_id"),
        _sender_id_from(raw.get("sender")),
        _sender_id_from(message.get("sender")),
        _sender_id_from(message.get("from")),
        message.get("sender_id"),
    )
    chat_id = _first_not_none(
        raw.get("chat_id"), chat.get("id"),
        message.get("chat_id"),
    )
    timestamp = _first_not_none(
        raw.get("timestamp"), message.get("timestamp"),
        raw.get("date"), message.get("date"),
    )

    return {
        "text": text,
        "sender_id": sender_id,
        "chat_id": chat_id,
        "timestamp": timestamp,
    }


class RetryMixin:
    """Run a coroutine with retry + exponential backoff.

    Only retries on :class:`TimeoutError` or :class:`ConnectionError`
    (transient network errors). Other exceptions are raised immediately
    so logic errors are never hidden.
    """

    async def _with_retry(
        self,
        coro_fn: Callable[[], Coroutine[Any, Any, Any]],
        max_retries: int = 3,
        base_delay: float = 1.0,
    ) -> Any:
        """Call ``coro_fn()``; on network error retry up to ``max_retries`` times.

        The wait between attempts is ``base_delay * 2^attempt`` seconds.
        When retries are exhausted → raise the last exception.
        """
        last_exc: Optional[BaseException] = None
        for attempt in range(max_retries + 1):
            try:
                return await coro_fn()
            except (TimeoutError, ConnectionError) as exc:
                last_exc = exc
                if attempt >= max_retries:
                    break
                delay = base_delay * (2 ** attempt)
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc


# Re-export for child adapters to use directly.
__all__ = [
    "ChannelAdapter",
    "HmacWebhookMixin",
    "normalize_message",
    "RetryMixin",
]
