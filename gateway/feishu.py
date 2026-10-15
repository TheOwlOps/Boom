"""Feishu/Lark adapter: tenant-token send + reactions + edit + delete.

Feishu wants a tenant_access_token (app_id + app_secret exchanged for one that
lasts ~2h) before it will accept a message, so the token is cached here until
it is close to expiring. Reactions, message update and message delete are real
im/v1 endpoints; Feishu answers HTTP 200 with a nonzero `code` for a refused
call, so the body is checked, not just the status.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional

import httpx

from gateway.channel import ChannelAdapter

DEFAULT_API_BASE = "https://open.feishu.cn"

# Kept at module level so every adapter instance shares one token, like before.
_FEISHU_TOKEN: Dict[str, Any] = {"value": "", "expiry": 0.0}


async def _feishu_token(base: str, app_id: str, app_secret: str) -> str:
    """The cached tenant access token, fetched from Feishu when there is none or stale."""
    if _FEISHU_TOKEN["value"] and time.monotonic() < _FEISHU_TOKEN["expiry"]:
        return _FEISHU_TOKEN["value"]
    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(
                f"{base}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            )
        body = resp.json() or {}
    except Exception as e:
        print(f"[feishu] token request failed: {type(e).__name__}: {e}", flush=True)
        return ""
    token = str(body.get("tenant_access_token") or "")
    if body.get("code") or not token:
        print(f"[feishu] token refused: {body}", flush=True)
        return ""
    _FEISHU_TOKEN["value"] = token
    # A minute of slack, so a token does not expire in the middle of a send.
    try:
        expires = float(body.get("expire", 7200))
    except (TypeError, ValueError):
        expires = 7200.0
    _FEISHU_TOKEN["expiry"] = time.monotonic() + max(0.0, expires - 60.0)
    return token


def _text_of(content: Any) -> str:
    """Feishu packs a message's text as a JSON string inside the event."""
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
    except ValueError:
        return ""
    return str((parsed or {}).get("text") or "") if isinstance(parsed, dict) else ""


class FeishuAdapter(ChannelAdapter):
    name = "feishu"

    def __init__(self, app_id: Optional[str] = None, app_secret: Optional[str] = None,
                 encrypt_key: Optional[str] = None,
                 api_base: Optional[str] = None) -> None:
        # None means "read the environment at call time", like the old daemon did.
        self.app_id = app_id
        self.app_secret = app_secret
        self.encrypt_key = encrypt_key
        self.api_base = api_base

    def _app(self) -> str:
        if self.app_id is not None:
            return self.app_id
        return os.getenv("BOOM_FEISHU_APP_ID", "")

    def _secret(self) -> str:
        if self.app_secret is not None:
            return self.app_secret
        return os.getenv("BOOM_FEISHU_APP_SECRET", "")

    def _base(self) -> str:
        if self.api_base is not None:
            return self.api_base.rstrip("/")
        return os.getenv("BOOM_FEISHU_API_BASE", DEFAULT_API_BASE).rstrip("/")

    def _key(self) -> str:
        if self.encrypt_key is not None:
            return self.encrypt_key
        return os.getenv("BOOM_FEISHU_ENCRYPT_KEY", "")

    async def _authed(self) -> tuple:
        """(client, headers) with a fresh tenant token, or (None, {}) without one."""
        token = await _feishu_token(self._base(), self._app(), self._secret())
        if not token:
            return None, {}
        client = httpx.AsyncClient(timeout=35.0)
        return client, {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _refused(resp: Any) -> Optional[Dict[str, Any]]:
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        if resp.status_code < 300 and not body.get("code"):
            return None
        return {"error": f"feishu refused: HTTP {resp.status_code}: "
                         f"{body.get('msg') or body or 'no body'}"}

    # -- reply path -------------------------------------------------------

    def can_deliver(self) -> bool:
        return bool(self._app() and self._secret())

    async def send_text(self, target: str, text: str) -> bool:
        client, headers = await self._authed()
        if client is None:
            return False
        async with client:
            resp = await client.post(
                f"{self._base()}/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers=headers,
                json={"receive_id": target, "msg_type": "text",
                      "content": json.dumps({"text": text})},
            )
        refused = self._refused(resp)
        ok = refused is None
        print(f"[feishu] -> {resp.status_code} ok={ok} {len(text)} chars", flush=True)
        return ok

    # -- actions -----------------------------------------------------------

    async def react(self, target: str, message_id: str, emoji: str) -> Dict[str, Any]:
        client, headers = await self._authed()
        if client is None:
            return {"error": "feishu: no tenant token (check BOOM_FEISHU_APP_ID/SECRET)"}
        async with client:
            resp = await client.post(
                f"{self._base()}/open-apis/im/v1/messages/{message_id}/reactions",
                headers=headers,
                # emoji_type is Feishu's reaction code ("SMILE", "OK", ...).
                json={"reaction_type": {"emoji_type": emoji}},
            )
        return self._refused(resp) or {"ok": True}

    async def edit_message(self, target: str, message_id: str, text: str) -> Dict[str, Any]:
        client, headers = await self._authed()
        if client is None:
            return {"error": "feishu: no tenant token (check BOOM_FEISHU_APP_ID/SECRET)"}
        async with client:
            resp = await client.put(
                f"{self._base()}/open-apis/im/v1/messages/{message_id}",
                headers=headers,
                json={"msg_type": "text", "content": json.dumps({"text": text})},
            )
        return self._refused(resp) or {"ok": True}

    async def delete_message(self, target: str, message_id: str) -> Dict[str, Any]:
        client, headers = await self._authed()
        if client is None:
            return {"error": "feishu: no tenant token (check BOOM_FEISHU_APP_ID/SECRET)"}
        async with client:
            resp = await client.delete(
                f"{self._base()}/open-apis/im/v1/messages/{message_id}",
                headers=headers,
            )
        return self._refused(resp) or {"ok": True}

    # -- ingress path -------------------------------------------------------

    def has_verify_secret(self) -> bool:
        return bool(self._key())

    def verify_request(self, headers: Dict[str, str], body: bytes) -> Optional[str]:
        """Feishu/Lark v2: sha256(timestamp + nonce + encrypt_key) over the raw body."""
        key = self._key()
        if not key:
            return None
        signature = headers.get("x-lark-signature", "")
        stamp = headers.get("x-lark-request-timestamp", "")
        nonce = headers.get("x-lark-request-nonce", "")
        if not signature or not stamp:
            return "missing X-Lark-Signature or X-Lark-Request-Timestamp"
        computed = hashlib.sha256(
            (stamp + nonce + key).encode() + body
        ).hexdigest()
        if not hmac.compare_digest(computed, signature):
            return "bad Feishu signature"
        return None

    def handshake(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # Feishu's v2 challenge is the same shape as Slack's, and arrives encrypted
        # when the key is set (the ingress decrypts it before calling here).
        if event.get("type") == "url_verification":
            return {"challenge": event.get("challenge")}
        return None

    def normalize(self, event: Dict[str, Any]) -> Dict[str, Any]:
        if "sender_id" in event and "text" in event:
            return event  # a bridge's canonical event passes through untouched
        if "encrypt" not in event and "header" not in event:
            return event
        if "encrypt" in event:
            # Only reached with no Encrypt Key configured here: the body is still
            # ciphertext, and a turn answered from ciphertext would be garbage.
            return {"error": "encrypted Feishu event: set BOOM_FEISHU_ENCRYPT_KEY to the app's "
                             "Encrypt Key so the body can be opened"}
        header = event.get("header") or {}
        if header.get("event_type") != "im.message.receive_v1":
            return {"ignore": f"Feishu event {header.get('event_type')!r}"}
        inner = event.get("event") or {}
        message = inner.get("message") or {}
        if message.get("message_type") != "text":
            return {"ignore": f"Feishu {message.get('message_type')} message"}
        sender = (inner.get("sender") or {}).get("sender_id") or {}
        sender_id = str(sender.get("open_id") or sender.get("user_id") or "")
        return {"sender_id": sender_id, "sender_name": sender_id,
                "text": _text_of(message.get("content")),
                "message_id": str(message.get("message_id") or ""),
                "target": str(message.get("chat_id") or "")}
