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

    async def send_sticker(self, target: str, sticker: str) -> Dict[str, Any]:
        """POST /sendSticker. `sticker` is a file_id from a previous sticker,
        an http(s) URL to a .webp/.tgs/.webm sticker, or an uploaded file_id."""
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(
                f"{self._api()}/sendSticker",
                json={"chat_id": target, "sticker": sticker},
            )
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        if resp.status_code < 300 and body.get("ok"):
            return {"ok": True}
        return {"error": f"telegram sendSticker refused: "
                         f"HTTP {resp.status_code}: {body or 'no body'}"}

    @staticmethod
    def _refused(method: str, status: int, body: Dict[str, Any]) -> Dict[str, Any]:
        # Telegram signals missing admin rights as HTTP 403 or error_code 403
        # inside a 200 body; anything else is a plain refusal. Never retried.
        if status == 403 or body.get("error_code") == 403:
            return {"error": "bot lacks admin permission"}
        return {"error": f"telegram {method} refused: "
                         f"HTTP {status}: {body or 'no body'}"}

    async def kick_member(self, target: str, user_id: str) -> Dict[str, Any]:
        """Kick = banChatMember then unbanChatMember immediately, so the user
        is removed but can rejoin (a plain ban would block re-entry).
        Admin-only: call only on the user's explicit request."""
        async with httpx.AsyncClient(timeout=35.0) as client:
            ban = await client.post(
                f"{self._api()}/banChatMember",
                json={"chat_id": target, "user_id": user_id},
            )
            try:
                ban_body = ban.json() or {}
            except ValueError:
                ban_body = {}
            if not (ban.status_code < 300 and ban_body.get("ok")):
                return self._refused("banChatMember", ban.status_code, ban_body)
            unban = await client.post(
                f"{self._api()}/unbanChatMember",
                json={"chat_id": target, "user_id": user_id},
            )
            try:
                unban_body = unban.json() or {}
            except ValueError:
                unban_body = {}
            if not (unban.status_code < 300 and unban_body.get("ok")):
                return self._refused("unbanChatMember", unban.status_code, unban_body)
        return {"ok": True}

    async def ban_member(self, target: str, user_id: str) -> Dict[str, Any]:
        """banChatMember: the user is removed and cannot rejoin until unbanned.
        Admin-only: call only on the user's explicit request."""
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(
                f"{self._api()}/banChatMember",
                json={"chat_id": target, "user_id": user_id},
            )
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        if resp.status_code < 300 and body.get("ok"):
            return {"ok": True}
        return self._refused("banChatMember", resp.status_code, body)

    async def get_group_info(self, target: str) -> Dict[str, Any]:
        """getChat (+ best-effort getChatMemberCount) -> {id, name, member_count, type}.

        Only chats the bot is a member of. getChatMemberCount is a separate
        call: when it fails (e.g. a private chat), member_count stays None but
        the rest of the info still returns.
        """
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(f"{self._api()}/getChat",
                                     json={"chat_id": target})
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        if not (resp.status_code < 300 and body.get("ok")):
            return {"error": f"telegram getChat refused (bot not in chat?): "
                             f"HTTP {resp.status_code}: {body or 'no body'}"}
        chat = body.get("result") or {}
        info: Dict[str, Any] = {
            "id": str(chat.get("id", target)),
            "name": chat.get("title") or chat.get("username") or chat.get("first_name") or "",
            "type": chat.get("type", ""),
            "member_count": None,
        }
        try:
            async with httpx.AsyncClient(timeout=35.0) as client:
                cresp = await client.post(f"{self._api()}/getChatMemberCount",
                                          json={"chat_id": target})
            cbody = cresp.json() or {}
            if cresp.status_code < 300 and cbody.get("ok"):
                info["member_count"] = cbody.get("result")
        except Exception:
            pass
        return info

    async def get_member_info(self, target: str, user_id: str) -> Dict[str, Any]:
        """getChatMember -> {id, name, role, is_bot}.

        HONESTY: a bot only sees members of groups it belongs to. Querying a
        stranger — or a chat the bot is not in — makes Telegram answer
        400/403, which is reported as an error, never fabricated.
        """
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(
                f"{self._api()}/getChatMember",
                json={"chat_id": target, "user_id": user_id},
            )
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        if not (resp.status_code < 300 and body.get("ok")):
            return {"error": f"telegram getChatMember refused (user not visible "
                             f"to bot?): HTTP {resp.status_code}: {body or 'no body'}"}
        member = body.get("result") or {}
        user = member.get("user") or {}
        name = user.get("username") or " ".join(
            part for part in (user.get("first_name"), user.get("last_name")) if part
        )
        return {
            "id": str(user.get("id", user_id)),
            "name": name,
            "role": member.get("status", ""),
            "is_bot": bool(user.get("is_bot")),
        }

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
