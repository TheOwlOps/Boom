"""Real Discord channel: gateway WebSocket in, REST out. No SDK, no stub.

Speaks the protocol as Discord documents it: `GET /gateway/bot` for the wss endpoint, op 10
HELLO -> op 2 IDENTIFY, op 1 heartbeats, op 0 MESSAGE_CREATE dispatch, and a reconnect for op 7
RECONNECT / op 9 INVALID_SESSION (the identify payload is valid once per connection, so the
honest recovery is a fresh socket). Replies go out over `POST /channels/<id>/messages`.

Message actions use the documented REST endpoints: PUT .../reactions/{emoji}/@me,
PATCH the message for edit (own messages only), DELETE for delete.

Anything it cannot do is logged and skipped; nothing here reports success it did not have.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

from tools.ws_client import WebSocket

from gateway.channel import ChannelAdapter

logger = logging.getLogger("boom.gateway.discord")

API_BASE = "https://discord.com/api/v10"
MAX_MESSAGE = 2000  # Discord rejects a longer message rather than truncating it

# GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT. MESSAGE_CONTENT is privileged:
# without it enabled for the app in the developer portal, `content` arrives empty.
INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)

RECONNECT_OPS = (7, 9)


class DiscordChannel(ChannelAdapter):
    name = "discord"

    def __init__(self, daemon: Any, token: str, api_base: str = API_BASE,
                 ssl_context: Optional[ssl.SSLContext] = None, timeout: float = 30.0):
        self.daemon = daemon
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.ssl_context = ssl_context
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self.seq: Optional[int] = None
        self.bot_user_id: Optional[str] = None
        self._running = False

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bot {self.token}"}

    # ------------------------------------------------------------------ adapter

    def can_deliver(self) -> bool:
        return bool(self.token)

    async def send_text(self, target: str, text: str) -> bool:
        try:
            await self.send_message(target, text)
            return True
        except Exception as e:
            print(f"[gateway] discord send to {target} failed: {type(e).__name__}: {e}",
                  flush=True)
            return False

    async def react(self, target: str, message_id: str, emoji: str) -> Dict[str, Any]:
        """PUT /channels/{id}/messages/{id}/reactions/{emoji}/@me -> 204."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.put(
                f"{self.api_base}/channels/{target}/messages/{message_id}"
                f"/reactions/{quote(emoji, safe='')}/@me",
                headers=self.headers,
            )
        if resp.status_code in (200, 201, 204):
            return {"ok": True}
        return {"error": f"discord add reaction refused: HTTP {resp.status_code}"}

    async def edit_message(self, target: str, message_id: str,
                           text: str) -> Dict[str, Any]:
        """PATCH the message content (only the bot's own messages)."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.patch(
                f"{self.api_base}/channels/{target}/messages/{message_id}",
                headers=self.headers,
                json={"content": text[:MAX_MESSAGE]},
            )
        if resp.status_code in (200, 201):
            return {"ok": True}
        return {"error": f"discord edit refused: HTTP {resp.status_code}"}

    async def delete_message(self, target: str, message_id: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.delete(
                f"{self.api_base}/channels/{target}/messages/{message_id}",
                headers=self.headers,
            )
        if resp.status_code in (200, 201, 204):
            return {"ok": True}
        return {"error": f"discord delete refused: HTTP {resp.status_code}"}

    # ------------------------------------------------------------------ REST/WS

    async def gateway_url(self) -> str:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.api_base}/gateway/bot", headers=self.headers)
            resp.raise_for_status()
            data = resp.json()
        return f"{data['url']}/?v=10&encoding=json"

    async def send_message(self, channel_id: str, text: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.api_base}/channels/{channel_id}/messages",
                headers=self.headers,
                json={"content": text[:MAX_MESSAGE]},
            )
            resp.raise_for_status()  # a refused send must not read as delivered
            return resp.json()

    async def run(self):
        """Stay connected until stop(). A dropped socket is retried with backoff."""
        self._running = True
        backoff = 1.0
        while self._running:
            try:
                await self._session(await self.gateway_url())
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._running:
                    break
                logger.warning("discord session failed: %s: %s (retry in %.0fs)",
                               type(e).__name__, e, backoff)
            if not self._running:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _session(self, url: str):
        ws = await asyncio.to_thread(WebSocket.connect, url, self.timeout, self.ssl_context)
        heartbeat: Optional[asyncio.Task] = None
        try:
            while self._running:
                raw = await asyncio.to_thread(ws.recv, self.timeout)
                if raw is None:
                    return  # peer closed; run() reconnects
                msg = json.loads(raw)
                if msg.get("s") is not None:
                    self.seq = msg["s"]
                op, name, data = msg.get("op"), msg.get("t"), msg.get("d") or {}
                if op == 10:
                    if heartbeat is not None:
                        heartbeat.cancel()
                    await self._identify(ws)
                    heartbeat = asyncio.create_task(
                        self._heartbeat(ws, data.get("heartbeat_interval", 41250) / 1000.0)
                    )
                elif op == 0 and name == "READY":
                    self.session_id = data.get("session_id")
                    self.bot_user_id = str((data.get("user") or {}).get("id") or "")
                    logger.info("discord ready as user %s", self.bot_user_id)
                elif op == 0 and name == "MESSAGE_CREATE":
                    await self._on_message(data)
                elif op in RECONNECT_OPS:
                    self.session_id = None
                    return
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
            await asyncio.to_thread(ws.close)

    async def _identify(self, ws: WebSocket):
        await asyncio.to_thread(ws.send_text, json.dumps({
            "op": 2,
            "d": {
                "token": self.token,
                "intents": INTENTS,
                "properties": {"os": "windows", "browser": "boom", "device": "boom"},
            },
        }))

    async def _heartbeat(self, ws: WebSocket, interval: float):
        try:
            while True:
                await asyncio.sleep(interval)
                await asyncio.to_thread(ws.send_text, json.dumps({"op": 1, "d": self.seq}))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("discord heartbeat stopped: %s: %s", type(e).__name__, e)

    async def _on_message(self, data: dict):
        author = data.get("author") or {}
        # Our own replies arrive on this same socket.
        if author.get("bot") or (self.bot_user_id and str(author.get("id")) == self.bot_user_id):
            return
        text = (data.get("content") or "").strip()
        if not text:
            return
        sender_id = str(author.get("id") or "")
        sender_name = author.get("global_name") or author.get("username") or sender_id
        channel_id = str(data.get("channel_id") or "")

        # The reply goes to the channel the message arrived on, not to the author, and a queued
        # turn is routed by the daemon (see can_deliver) rather than by a closure over this socket.
        reply = await self.daemon.handle_incoming_message(
            "discord", sender_id, sender_name, text, str(data.get("id") or ""), target=channel_id
        )
        if not reply:
            return
        try:
            await self.send_message(channel_id, reply)
        except Exception as e:
            logger.warning("discord reply failed: %s: %s", type(e).__name__, e)

    def stop(self):
        self._running = False
