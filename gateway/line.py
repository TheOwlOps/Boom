"""LINE adapter: push send + webhook verification.

LINE's Messaging API offers no reactions, edit or delete for bot-sent messages,
so all three actions honestly report not-supported via the base class.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Any, Dict, Optional

import httpx

from gateway.channel import ChannelAdapter

DEFAULT_API_BASE = "https://api.line.me"


class LineAdapter(ChannelAdapter):
    name = "line"

    def __init__(self, channel_token: Optional[str] = None,
                 channel_secret: Optional[str] = None,
                 api_base: Optional[str] = None) -> None:
        # None means "read the environment at call time", like the old daemon did.
        self.channel_token = channel_token
        self.channel_secret = channel_secret
        self.api_base = api_base

    def _token(self) -> str:
        if self.channel_token is not None:
            return self.channel_token
        return os.getenv("BOOM_LINE_CHANNEL_TOKEN", "")

    def _base(self) -> str:
        if self.api_base is not None:
            return self.api_base.rstrip("/")
        return os.getenv("BOOM_LINE_API_BASE", DEFAULT_API_BASE).rstrip("/")

    def _secret(self) -> str:
        if self.channel_secret is not None:
            return self.channel_secret
        return os.getenv("BOOM_LINE_CHANNEL_SECRET", "")

    # -- reply path -------------------------------------------------------
    # LINE answers a webhook with 200 and then expects the reply to be pushed:
    # holding its request open for a whole turn is what makes it retry and
    # duplicate the message.

    def can_deliver(self) -> bool:
        return bool(self._token())

    async def send_text(self, target: str, text: str) -> bool:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(
                f"{self._base()}/v2/bot/message/push",
                headers={"Authorization": f"Bearer {self._token()}"},
                json={"to": target, "messages": [{"type": "text", "text": text}]},
            )
        print(f"[line] -> {resp.status_code} {len(text)} chars", flush=True)
        return resp.status_code < 300

    # -- actions -----------------------------------------------------------
    # react / edit_message / delete_message: no such LINE Messaging API
    # endpoints — the base class's honest {"error": ...} stands.

    # -- ingress path -------------------------------------------------------

    def has_verify_secret(self) -> bool:
        return bool(self._secret())

    def verify_request(self, headers: Dict[str, str], body: bytes) -> Optional[str]:
        """LINE: base64(HMAC-SHA256(channel secret, raw body)) in X-Line-Signature."""
        secret = self._secret()
        if not secret:
            return None
        signature = headers.get("x-line-signature", "")
        if not signature:
            return "missing X-Line-Signature"
        expected = base64.b64encode(
            hmac.new(secret.encode(), body, hashlib.sha256).digest()
        ).decode("ascii")
        if not hmac.compare_digest(expected, signature):
            return "bad LINE signature"
        return None

    def normalize(self, event: Dict[str, Any]) -> Dict[str, Any]:
        if "sender_id" in event and "text" in event:
            return event  # a bridge's canonical event passes through untouched
        if "events" not in event:
            return event
        for item in event.get("events") or []:
            message = (item or {}).get("message") or {}
            if (item or {}).get("type") != "message" or message.get("type") != "text":
                continue
            user = str(((item.get("source") or {}).get("userId")) or "")
            return {"sender_id": user, "sender_name": user, "text": str(message.get("text") or ""),
                    "message_id": str(message.get("id") or ""), "target": user}
        return {"ignore": "LINE non-message event"}
