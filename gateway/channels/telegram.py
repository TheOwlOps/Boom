"""Telegram adapter: Bot API send + reactions + edit + delete.

Endpoints per the public Bot API: sendMessage, setMessageReaction (7.0+),
editMessageText, deleteMessage. Webhook posts can be verified with the secret
token Telegram sends as X-Telegram-Bot-Api-Secret-Token.
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Dict, Optional

import httpx

from gateway.channel import ChannelAdapter

DEFAULT_API_BASE = "https://api.telegram.org"


class TelegramAdapter(ChannelAdapter):
    name = "telegram"

    def __init__(self, token: Optional[str] = None,
                 secret_token: Optional[str] = None,
                 api_base: Optional[str] = None) -> None:
        # None means "read the environment at call time", like the old daemon did.
        self.token = token
        self.secret_token = secret_token
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")

    def _tok(self) -> str:
        return self.token if self.token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")

    def _api(self) -> str:
        return f"{self.api_base}/bot{self._tok()}"

    @staticmethod
    def _msg_id(message_id: str) -> Any:
        try:
            return int(message_id)
        except (TypeError, ValueError):
            return message_id

    # -- reply path -------------------------------------------------------

    def can_deliver(self) -> bool:
        return bool(self._tok())

    async def send_text(self, target: str, text: str) -> bool:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(f"{self._api()}/sendMessage",
                                     json={"chat_id": target, "text": text})
        print(f"[telegram] -> {resp.status_code} {len(text)} chars", flush=True)
        return resp.status_code < 300

    # -- actions -----------------------------------------------------------

    async def react(self, target: str, message_id: str, emoji: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(
                f"{self._api()}/setMessageReaction",
                json={"chat_id": target, "message_id": self._msg_id(message_id),
                      "reaction": [{"type": "emoji", "emoji": emoji}]},
            )
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        if resp.status_code < 300 and body.get("ok"):
            return {"ok": True}
        return {"error": f"telegram setMessageReaction refused: "
                         f"HTTP {resp.status_code}: {body or 'no body'}"}

    async def edit_message(self, target: str, message_id: str, text: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(
                f"{self._api()}/editMessageText",
                json={"chat_id": target, "message_id": self._msg_id(message_id),
                      "text": text},
            )
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        if resp.status_code < 300 and body.get("ok"):
            return {"ok": True}
        return {"error": f"telegram editMessageText refused: "
                         f"HTTP {resp.status_code}: {body or 'no body'}"}

    async def delete_message(self, target: str, message_id: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(
                f"{self._api()}/deleteMessage",
                json={"chat_id": target, "message_id": self._msg_id(message_id)},
            )
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        if resp.status_code < 300 and body.get("ok"):
            return {"ok": True}
        return {"error": f"telegram deleteMessage refused: "
                         f"HTTP {resp.status_code}: {body or 'no body'}"}

    # -- ingress path -------------------------------------------------------

    def _secret(self) -> str:
        if self.secret_token is not None:
            return self.secret_token
        return os.getenv("BOOM_TELEGRAM_SECRET_TOKEN", "")

    def has_verify_secret(self) -> bool:
        return bool(self._secret())

    def verify_request(self, headers: Dict[str, str], body: bytes) -> Optional[str]:
        secret = self._secret()
        if not secret:
            return None
        if not hmac.compare_digest(headers.get("x-telegram-bot-api-secret-token", ""), secret):
            return "bad X-Telegram-Bot-Api-Secret-Token"
        return None

    def normalize(self, event: Dict[str, Any]) -> Dict[str, Any]:
        if "sender_id" in event and "text" in event:
            return event  # a bridge's canonical event passes through untouched
        msg = event.get("message") or event.get("channel_post") or {}
        if not isinstance(msg, dict) or "text" not in msg:
            if "update_id" in event or "message" in event or "channel_post" in event:
                return {"ignore": "Telegram non-text update"}
            return event
        chat = msg.get("chat") or {}
        frm = msg.get("from") or {}
        return {
            "sender_id": str(frm.get("id") or ""),
            "sender_name": str(frm.get("username") or frm.get("first_name") or ""),
            "text": str(msg.get("text") or ""),
            "message_id": str(msg.get("message_id") or ""),
            "target": str(chat.get("id") or ""),
        }
