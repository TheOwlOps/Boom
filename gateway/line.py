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
        client = self._http()
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

    async def recall_message(self, target: str, message_id: str) -> Dict[str, Any]:
        """LINE Messaging API has no unsend/delete endpoint for bot messages
        (verified: all Messaging API methods checked — "unsend" exists only
        as a webhook event when a *user* unsends). Honest error, never fake."""
        return {"error": "line: Messaging API has no unsend endpoint for bot messages"}

    async def send_sticker(self, target: str, sticker: str) -> Dict[str, Any]:
        """Push a sticker message. `sticker` is "packageId:stickerId"
        (e.g. "446:1988" — LINE documents the package/sticker ids)."""
        parts = str(sticker or "").split(":", 1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            return {"error": "line sticker must look like 'packageId:stickerId'"}
        package_id, sticker_id = parts[0].strip(), parts[1].strip()
        client = self._http()
        resp = await client.post(
            f"{self._base()}/v2/bot/message/push",
            headers={"Authorization": f"Bearer {self._token()}"},
            json={"to": target, "messages": [{"type": "sticker",
                                              "packageId": package_id,
                                              "stickerId": sticker_id}]},
        )
        print(f"[line] sticker -> {resp.status_code}", flush=True)
        if resp.status_code < 300:
            return {"ok": True}
        return {"error": f"line send sticker refused: HTTP {resp.status_code}"}

    async def get_group_info(self, target: str) -> Dict[str, Any]:
        """GET /bot/group/{groupId}/summary (+ best-effort member count)
        -> {id, name, member_count, type}.

        LINE's group summary carries no member count; the members/count
        endpoint is called best-effort and member_count stays None when it
        fails.
        """
        headers = {"Authorization": f"Bearer {self._token()}"}
        client = self._http()
        resp = await client.get(
            f"{self._base()}/bot/group/{target}/summary", headers=headers)
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        if resp.status_code >= 300:
            return {"error": f"line group summary refused (bot not in group?): "
                             f"HTTP {resp.status_code}"}
        info: Dict[str, Any] = {
            "id": str(body.get("groupId", target)),
            "name": body.get("groupName", ""),
            "member_count": None,
            "type": "group",
        }
        try:
            client = self._http()
            cresp = await client.get(
                f"{self._base()}/bot/group/{target}/members/count",
                headers=headers)
            cbody = cresp.json() or {}
            if cresp.status_code < 300:
                info["member_count"] = cbody.get("count")
        except Exception:
            pass
        return info

    async def get_member_info(self, target: str, user_id: str) -> Dict[str, Any]:
        """GET /bot/group/{groupId}/member/{userId} -> {id, name}.

        HONESTY: LINE only exposes profiles of members in groups the bot
        belongs to. Anyone else answers 404 — reported honestly, never
        fabricated. (LINE member profiles carry no role or join date.)
        """
        client = self._http()
        resp = await client.get(
            f"{self._base()}/bot/group/{target}/member/{user_id}",
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        if resp.status_code == 404:
            return {"error": f"line: user {user_id} not visible to the bot "
                             f"in group {target}"}
        if resp.status_code >= 300:
            return {"error": f"line member profile refused: HTTP {resp.status_code}"}
        return {
            "id": str(body.get("userId", user_id)),
            "name": body.get("displayName", ""),
        }

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
