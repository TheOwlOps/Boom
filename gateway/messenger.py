"""Messenger adapter: Meta Messenger Platform Send API.

Send goes to graph.facebook.com/v21.0/me/messages with the Page access token.
Webhook verification is Meta's GET handshake (hub.mode=subscribe, hub.verify_token,
hub.challenge); request signing is X-Hub-Signature-256 = "sha256=" +
HMAC-SHA256(app secret, raw body).

The Send API exposes no reactions, edit or delete for sent messages, so those
actions honestly report not-supported via the base class.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any, Dict, Optional


from gateway.channel import ChannelAdapter

DEFAULT_API_BASE = "https://graph.facebook.com/v21.0"


class MessengerAdapter(ChannelAdapter):
    name = "messenger"

    def __init__(self, page_token: Optional[str] = None,
                 app_secret: Optional[str] = None,
                 verify_token: Optional[str] = None,
                 api_base: Optional[str] = None) -> None:
        # None means "read the environment at call time".
        self.page_token = page_token
        self.app_secret = app_secret
        self.verify_token = verify_token
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")

    def _token(self) -> str:
        if self.page_token is not None:
            return self.page_token
        return os.getenv("BOOM_MESSENGER_PAGE_TOKEN", "")

    def _app_secret(self) -> str:
        if self.app_secret is not None:
            return self.app_secret
        return os.getenv("BOOM_MESSENGER_APP_SECRET", "")

    def _verify_token(self) -> str:
        if self.verify_token is not None:
            return self.verify_token
        return os.getenv("BOOM_MESSENGER_VERIFY_TOKEN", "")

    # -- reply path -------------------------------------------------------

    def can_deliver(self) -> bool:
        return bool(self._token())

    async def send_text(self, target: str, text: str) -> bool:
        token = self._token()
        if not token:
            return False
        client = self._http()
        resp = await client.post(
            f"{self.api_base}/me/messages",
            params={"access_token": token},
            json={"recipient": {"id": str(target)},
                  "messaging_type": "RESPONSE",
                  "message": {"text": text}},
        )
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        ok = resp.status_code < 300 and bool(body.get("message_id"))
        print(f"[messenger] -> {resp.status_code} ok={ok} {len(text)} chars", flush=True)
        return ok

    # -- actions -----------------------------------------------------------
    # react / edit_message / delete_message: the Send API has no such endpoints —
    # the base class's honest {"error": ...} stands.

    # -- ingress path -------------------------------------------------------

    def has_verify_secret(self) -> bool:
        return bool(self._app_secret())

    def verify_request(self, headers: Dict[str, str], body: bytes) -> Optional[str]:
        secret = self._app_secret()
        if not secret:
            return None
        signature = headers.get("x-hub-signature-256", "")
        if not signature.startswith("sha256="):
            return "missing X-Hub-Signature-256"
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return "bad X-Hub-Signature-256"
        return None

    def verify_webhook(self, params: Dict[str, str]) -> Optional[str]:
        """Meta's GET handshake: answer the challenge only when mode and token match."""
        if params.get("hub.mode") != "subscribe":
            return None
        want = self._verify_token()
        if not want or params.get("hub.verify_token") != want:
            return None
        return params.get("hub.challenge")

    def normalize(self, event: Dict[str, Any]) -> Dict[str, Any]:
        if "sender_id" in event and "text" in event:
            return event  # a bridge's canonical event passes through untouched
        if "entry" not in event:
            return event
        for entry in event.get("entry") or []:
            for item in (entry or {}).get("messaging") or []:
                message = (item or {}).get("message") or {}
                text = message.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                sender = str(((item.get("sender") or {}).get("id")) or "")
                return {"sender_id": sender, "sender_name": sender,
                        "text": text, "message_id": str(message.get("mid") or ""),
                        "target": sender}
        return {"ignore": "Messenger non-message event"}
