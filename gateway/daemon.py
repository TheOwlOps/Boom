"""Production Multi-Channel Gateway Daemon (Telegram, Zalo Bridge, Ingress Webhooks)."""

import asyncio
import logging
import os
from typing import Any, Callable, Dict, Optional
import httpx

from agent.src.security.system_one_gate import SystemOneGate
from gateway.delivery_ledger import DeliveryLedger
from gateway.dispatcher import TurnDispatcher, new_job
from gateway.discord import DiscordChannel
from gateway.ingress import IngressServer
from gateway.channel import build_default_registry

logger = logging.getLogger("boom.gateway")

FAILED_TEXT = "Xử lý thất bại. Chi tiết đã ghi trong log của daemon."
QUEUED_ACK = "Yêu cầu đang được xử lý, kết quả sẽ được gửi sau."


class GatewayDaemon:
    def __init__(
        self,
        runner_fn: Callable[[str, str, str], Any],
        delivery_ledger: Optional[DeliveryLedger] = None,
        telegram_token: Optional[str] = None,
        discord_token: Optional[str] = None,
        zalo_bridge_url: str = "http://127.0.0.1:8787",
        root_dir: Optional[Any] = None
    ):
        self.runner_fn = runner_fn
        self.ledger = delivery_ledger or DeliveryLedger()
        self.tg_token = telegram_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.dc_token = discord_token or os.getenv("DISCORD_BOT_TOKEN")
        self.zalo_url = zalo_bridge_url
        self.root_dir = root_dir
        self.ingress: Optional[IngressServer] = None
        self.discord: Optional[DiscordChannel] = None
        self.dispatcher: Optional[TurnDispatcher] = None
        self._running = False
        # One adapter per channel; send/verify/normalize fan out through here.
        # Discord joins in start(), because its adapter needs the live daemon.
        self.registry = build_default_registry()
        if self.tg_token:
            self.registry.get("telegram").token = self.tg_token

    def can_deliver(self, channel: str) -> bool:
        """Whether a reply can still reach this channel after the request that carried it is gone."""
        return self.registry.can_deliver(channel)

    async def send_to(self, channel: str, target: str, text: str) -> bool:
        """Send text to a channel outside a request. `target` is where the reply goes.

        Used for every queued turn: each adapter owns its HTTP client, so nothing here holds a
        socket the channel's listener owns, and a listener that reconnects cannot take a reply
        down with it.
        """
        return await self.registry.send_to(channel, target, text)

    async def react(self, channel: str, target: str, message_id: str,
                    emoji: str) -> Dict[str, Any]:
        """Put a reaction on a channel message. Honest {"error": ...} when the
        platform has no such API."""
        return await self.registry.react(channel, target, message_id, emoji)

    async def edit_message(self, channel: str, target: str, message_id: str,
                           text: str) -> Dict[str, Any]:
        """Replace the text of a bot-sent channel message."""
        return await self.registry.edit_message(channel, target, message_id, text)

    async def delete_message(self, channel: str, target: str,
                             message_id: str) -> Dict[str, Any]:
        """Delete a bot-sent channel message."""
        return await self.registry.delete_message(channel, target, message_id)

    async def handle_incoming_message(
        self,
        channel: str,
        sender_id: str,
        sender_name: str,
        text: str,
        message_id: str,
        deliver: Optional[Callable[[str], Any]] = None,
        target: str = "",
        priority: int = 0
    ) -> Optional[str]:
        """
        Process an incoming message from Telegram/Zalo/Discord.
        Guarantees idempotency and enforces Owner-Only Guard.
        """
        # 1. Idempotency check via DeliveryLedger
        obligation_key = f"{channel}:{sender_id}:{message_id}"
        if not self.ledger.claim(obligation_key, channel, target=sender_id):
            logger.info("Duplicate message %s suppressed by DeliveryLedger", obligation_key)
            return None

        # 2. System 1 Reflex Gate & Owner Guard
        route, meta = SystemOneGate.evaluate(text, user_identity=sender_name)
        if route == "owner_guard_blocked":
            reply = meta.get("response", "Xin lỗi, thông tin kiến trúc và cấu hình hệ thống được bảo mật tuyệt đối.")
            self.ledger.fulfill(obligation_key)
            return reply
        elif route == "blocked":
            reply = "Yêu cầu bị từ chối do vi phạm quy tắc bảo mật an toàn."
            self.ledger.fulfill(obligation_key)
            return reply

        # 3. Dispatch to ReAct / Conversation Runner
        if self.dispatcher is None:
            # No queue at all: run it here and answer in-band.
            try:
                res = await self.runner_fn(text, channel, sender_name)
                reply_text = str(res)
            except Exception as e:
                # Anyone who finds the bot can trip this, so the reply says only that it failed: a
                # provider error string carries the endpoint, the model and the account's billing
                # state.
                print(f"[gateway] runner failed: {type(e).__name__}: {e}", flush=True)
                reply_text = FAILED_TEXT
            self.ledger.fulfill(obligation_key)
            return reply_text

        # A heavy turn must not stall the channel or pile up unbounded, so it goes through the
        # bounded queue. A channel the daemon can reach on its own is acked and answered later
        # (through its own client, not the one the message arrived on); a bridge holding this
        # request open waits for the reply instead.
        wait = deliver is None and not self.can_deliver(channel)
        outcome = await self.dispatcher.submit(
            new_job(channel, sender_id, sender_name, text, message_id,
                    target=target or sender_id, priority=priority),
            deliver=deliver, wait=wait,
        )
        self.ledger.fulfill(obligation_key)
        if outcome["status"] == "rejected":
            return f"Hệ thống đang bận ({outcome['error']})."
        if outcome["status"] == "queued":
            return QUEUED_ACK
        if outcome["status"] == "failed":
            return FAILED_TEXT
        if outcome["status"] == "cancelled":
            return "Yêu cầu đã bị huỷ."
        return outcome["reply"]

    async def _recover_delivery(self, job: Any, reply: str):
        """Where a queued turn's reply goes when no caller is left to hand it to."""
        if not await self.send_to(job.channel, job.reply_target(), reply):
            print(f"[gateway] job {job.job_id} has no route back to "
                  f"{job.channel}:{job.reply_target()}", flush=True)

    async def poll_telegram(self):
        """Long-polling listener for Telegram."""
        if not self.tg_token:
            return

        offset = 0
        url = f"https://api.telegram.org/bot{self.tg_token}"
        async with httpx.AsyncClient(timeout=35.0) as client:
            while self._running:
                try:
                    resp = await client.get(f"{url}/getUpdates", params={"offset": offset, "timeout": 25})
                    if resp.status_code == 200:
                        data = resp.json()
                        for update in data.get("result", []):
                            offset = update["update_id"] + 1
                            msg = update.get("message") or update.get("channel_post")
                            if not msg or "text" not in msg:
                                continue

                            text = msg["text"]
                            sender = msg.get("from", {})
                            sender_name = sender.get("username") or sender.get("first_name", "User")
                            chat_id = str(msg["chat"]["id"])
                            msg_id = str(msg["message_id"])

                            # The daemon's own logging is unconfigured, so a live run would show
                            # nothing; the operator needs to see what arrived and what went out.
                            print(f"[telegram] {sender_name}: {text[:100]}", flush=True)

                            # No callback is handed over on purpose: a queued turn's reply goes out
                            # through send_to, on a client of its own, so a reconnect of this poll
                            # loop cannot swallow it.
                            reply = await self.handle_incoming_message(
                                "telegram", chat_id, sender_name, text, msg_id, target=chat_id
                            )
                            if reply:
                                await self.send_to("telegram", chat_id, reply)
                except Exception as e:
                    logger.warning("Telegram polling error: %s. Retrying in 5s...", e)
                    await asyncio.sleep(5.0)

    async def start(self):
        self._running = True
        tasks = []
        if self.tg_token:
            tasks.append(asyncio.create_task(self.poll_telegram()))
        if self.dc_token:
            self.discord = DiscordChannel(self, self.dc_token)
            self.registry.register(self.discord)
            tasks.append(asyncio.create_task(self.discord.run()))
        # Push channels come in over HTTP; the Rust GatewayServer that defines this route is
        # never constructed, so the listener lives here.
        self.ingress = IngressServer(
            self,
            host=os.getenv("BOOM_INGRESS_HOST", "127.0.0.1"),
            port=int(os.getenv("BOOM_INGRESS_PORT", "8788")),
        )
        tasks.append(asyncio.create_task(self.ingress.serve()))
        self.dispatcher = TurnDispatcher(
            self.runner_fn, root_dir=self.root_dir, recover=self._recover_delivery,
            failure_text=FAILED_TEXT,  # an acked turn that fails must still answer the channel
        )
        await self.dispatcher.start()
        try:
            await asyncio.gather(*tasks)
        finally:
            self.stop()
            if self.dispatcher is not None:
                await self.dispatcher.stop()

    def stop(self):
        self._running = False
        if self.ingress is not None:
            self.ingress.stop()
        if self.discord is not None:
            self.discord.stop()
