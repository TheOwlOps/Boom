"""WhatsApp adapter: Cloud API send + reactions.

Reactions are a real message type ("reaction" with the target message_id).
The Cloud API offers no edit or delete for bot-sent messages, so those two
actions honestly report not-supported via the base class.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import httpx

from gateway.channel import ChannelAdapter

DEFAULT_API_BASE = "https://graph.facebook.com/v21.0"


class WhatsAppAdapter(ChannelAdapter):
    name = "whatsapp"

    def __init__(self, token: Optional[str] = None, phone_id: Optional[str] = None,
                 api_base: Optional[str] = None) -> None:
        # None means "read the environment at call time", like the old daemon did.
        self.token = token
        self.phone_id = phone_id
        self.api_base = api_base

    def _token(self) -> str:
        if self.token is not None:
            return self.token
        return os.getenv("BOOM_WHATSAPP_TOKEN", "")

    def _phone(self) -> str:
        if self.phone_id is not None:
            return self.phone_id
        return os.getenv("BOOM_WHATSAPP_PHONE_ID", "")

    def _base(self) -> str:
        if self.api_base is not None:
            return self.api_base.rstrip("/")
        return os.getenv("BOOM_WHATSAPP_API_BASE", DEFAULT_API_BASE).rstrip("/")

    async def _post_message(self, payload: Dict[str, Any]) -> Any:
        async with httpx.AsyncClient(timeout=35.0) as client:
            return await client.post(
                f"{self._base()}/{self._phone()}/messages",
                headers={"Authorization": f"Bearer {self._token()}"},
                json=payload,
            )

    # -- reply path -------------------------------------------------------

    def can_deliver(self) -> bool:
        return bool(self._token() and self._phone())

    async def send_text(self, target: str, text: str) -> bool:
        if not self._phone():
            return False
        resp = await self._post_message({
            "messaging_product": "whatsapp", "to": target, "type": "text",
            "text": {"body": text},
        })
        print(f"[whatsapp] -> {resp.status_code} {len(text)} chars", flush=True)
        return resp.status_code < 300

    # -- actions -----------------------------------------------------------
    # edit_message / delete_message: the Cloud API has no such endpoints, so the
    # base class's honest {"error": ...} stands.

    async def react(self, target: str, message_id: str, emoji: str) -> Dict[str, Any]:
        resp = await self._post_message({
            "messaging_product": "whatsapp", "to": target, "type": "reaction",
            "reaction": {"message_id": str(message_id), "emoji": emoji},
        })
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        if resp.status_code < 300 and not body.get("error"):
            return {"ok": True}
        err = (body.get("error") or {})
        return {"error": f"whatsapp reaction refused: HTTP {resp.status_code}: "
                         f"{err.get('message') if isinstance(err, dict) else body or 'no body'}"}

    async def send_sticker(self, target: str, sticker: str) -> Dict[str, Any]:
        """Cloud API sticker message. `sticker` is a media id, or an http(s)
        URL (sent as a link — the sticker must be WebP)."""
        sticker = str(sticker or "").strip()
        if not sticker:
            return {"error": "whatsapp sticker is empty"}
        media = ({"link": sticker} if sticker.startswith(("http://", "https://"))
                 else {"id": sticker})
        resp = await self._post_message({
            "messaging_product": "whatsapp", "to": target, "type": "sticker",
            "sticker": media,
        })
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        if resp.status_code < 300 and not body.get("error"):
            return {"ok": True}
        err = (body.get("error") or {})
        return {"error": f"whatsapp sticker refused: HTTP {resp.status_code}: "
                         f"{err.get('message') if isinstance(err, dict) else body or 'no body'}"}

    # -- ingress path -------------------------------------------------------

    def normalize(self, event: Dict[str, Any]) -> Dict[str, Any]:
        if "sender_id" in event and "text" in event:
            return event  # a bridge's canonical event passes through untouched
        if "entry" not in event:
            return event
        for entry in event.get("entry") or []:
            for change in (entry or {}).get("changes") or []:
                value = (change or {}).get("value") or {}
                for message in value.get("messages") or []:
                    if (message or {}).get("type") != "text":
                        continue
                    sender = str(message.get("from") or "")
                    contacts = value.get("contacts") or []
                    name = ""
                    if contacts and isinstance(contacts[0], dict):
                        name = str((contacts[0].get("profile") or {}).get("name") or "")
                    return {"sender_id": sender, "sender_name": name or sender,
                            "text": str((message.get("text") or {}).get("body") or ""),
                            "message_id": str(message.get("id") or ""), "target": sender}
        return {"ignore": "WhatsApp status callback, not a message"}
