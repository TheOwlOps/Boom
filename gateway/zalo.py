"""Zalo adapter: Official Account (OA) customer-service message API.

Native Zalo OA send — no bridge in front: POST openapi.zalo.me/v3.0/oa/message/cs
with the OA access token in the `access_token` header. Zalo answers
{"error": 0, ...} on success.

The OA API exposes no reactions, edit or delete for sent messages, so those
actions honestly report not-supported via the base class. Inbound Zalo traffic
arrives through a bridge posting canonical events, which pass through
normalize() untouched.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional


from gateway.channel import ChannelAdapter

OA_API = "https://openapi.zalo.me"


class ZaloAdapter(ChannelAdapter):
    name = "zalo"

    def __init__(self, access_token: Optional[str] = None,
                 api_base: Optional[str] = None) -> None:
        # None means "read the environment at call time".
        self.access_token = access_token
        self.api_base = (api_base or OA_API).rstrip("/")

    def _token(self) -> str:
        if self.access_token is not None:
            return self.access_token
        return os.getenv("BOOM_ZALO_OA_ACCESS_TOKEN", "")

    # -- reply path -------------------------------------------------------

    def can_deliver(self) -> bool:
        return bool(self._token())

    async def send_text(self, target: str, text: str) -> bool:
        token = self._token()
        if not token:
            return False
        client = self._http()
        resp = await client.post(
            f"{self.api_base}/v3.0/oa/message/cs",
            headers={"access_token": token},
            json={"recipient": {"user_id": str(target)},
                  "message": {"text": text}},
        )
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        ok = resp.status_code < 300 and body.get("error") == 0
        print(f"[zalo] -> {resp.status_code} ok={ok} {len(text)} chars", flush=True)
        return ok

    # -- actions -----------------------------------------------------------
    # react / edit_message / delete_message: the OA API has no such endpoints —
    # the base class's honest {"error": ...} stands.

    async def recall_message(self, target: str, message_id: str) -> Dict[str, Any]:
        """Zalo OA exposes no message-recall endpoint (verified against the
        public OA API docs — "thu hồi" exists only in the personal Zalo app
        and unofficial personal-account APIs). Honest error, never a fake call."""
        return {"error": "zalo: OA API has no message recall endpoint"}

    async def send_sticker(self, target: str, sticker: str) -> Dict[str, Any]:
        """OA customer-service message with a sticker id. Zalo answers
        {"error": 0, ...} on success."""
        token = self._token()
        if not token:
            return {"error": "zalo: no OA access token configured"}
        client = self._http()
        resp = await client.post(
            f"{self.api_base}/v3.0/oa/message/cs",
            headers={"access_token": token},
            json={"recipient": {"user_id": str(target)},
                  "message": {"stickerid": str(sticker)}},
        )
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        ok = resp.status_code < 300 and body.get("error") == 0
        print(f"[zalo] sticker -> {resp.status_code} ok={ok}", flush=True)
        if ok:
            return {"ok": True}
        return {"error": f"zalo send sticker refused: HTTP {resp.status_code}: "
                         f"{body or 'no body'}"}
    # the base class's honest {"error": ...} stands.

    # -- ingress path -------------------------------------------------------
    # Zalo reaches us through a bridge that posts the canonical event shape;
    # the base normalize() passes it through untouched.
