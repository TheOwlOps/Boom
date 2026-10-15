"""HTTP ingress for channels that push to us instead of being polled.

`crates/gateway` already defines this interface (`POST /webhook/<channel>` -> `IncomingEvent`)
but nothing constructs `GatewayServer` there, so the daemon listens here instead: zalo_bridge_url
and any other bridge on this machine can forward platform traffic into the owner guard and the
delivery ledger without a new SDK per platform.

`GET /health` reports the turn queue (per channel) and `POST /cancel {"job_id": ...}` stops a
turn that is still running — both behind the same token.

Loopback-only by default, because anything that can reach this endpoint can talk to the agent.
`BOOM_INGRESS_TOKEN` requires `X-Boom-Token` on every request; channels whose adapter holds a
signature secret (`BOOM_SLACK_SIGNING_SECRET`, `BOOM_FEISHU_ENCRYPT_KEY`,
`BOOM_LINE_CHANNEL_SECRET`, `BOOM_MESSENGER_APP_SECRET`) verify their platform's own signature
instead, replay window included, which is what makes binding off loopback defensible.

The body may be a bridge's canonical event or the platform's own webhook payload: Slack's Events
API (including the `url_verification` ping), the WhatsApp Cloud API, Feishu's v2 event (decrypted
when the app's Encrypt Key is set), LINE's webhook and Messenger's webhook are read into the same
event by their channel adapters, so a real app can point at this endpoint with no translation
service in front of it. A payload that needs no turn — a delivery receipt, an event we do not act
on — is accepted with a 200 and ignored, because a platform retries anything it does not see
accepted.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
from typing import Any, Optional
from urllib.parse import urlsplit, parse_qsl

import httpx

from gateway.channel import build_default_registry, ChannelAdapter

logger = logging.getLogger("boom.gateway.ingress")

MAX_BODY = 64 * 1024  # a chat message, not an upload
_PREFIX = "/webhook/"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
SLACK_WINDOW = 300  # seconds, Slack's own documented replay window


def _decrypt_feishu(key: str, blob: str) -> dict:
    """Feishu's encrypted event: AES-256-CBC, the IV is the first 16 bytes, PKCS7 padded.

    With an Encrypt Key set the whole body arrives as `{"encrypt": ...}`, so the signature check
    passes on ciphertext: reading it is the only way to answer such an event at all.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw = base64.b64decode(blob)
    if len(raw) <= 16:
        raise ValueError("encrypted body is too short to hold an IV and a block")
    decryptor = Cipher(algorithms.AES(hashlib.sha256(key.encode()).digest()),
                       modes.CBC(raw[:16])).decryptor()
    padded = decryptor.update(raw[16:]) + decryptor.finalize()
    pad = padded[-1] if padded else 0  # PKCS7: the last byte is the padding length
    if not 0 < pad <= 16 or pad > len(padded):
        raise ValueError("bad padding: wrong Encrypt Key?")
    return json.loads(padded[:-pad].decode("utf-8"))


_DEFAULT_REGISTRY = None


def _default_registry():
    """Process-wide adapter registry for the module-level helpers below. Adapters
    read their secrets from the environment at call time, so one instance is enough."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = build_default_registry()
    return _DEFAULT_REGISTRY


def _handshake(channel: str, event: dict) -> Optional[dict]:
    """A platform's own endpoint-verification ping, answered without running a turn."""
    return _default_registry().handshake(channel, event)


def _normalize(channel: str, event: dict) -> dict:
    """The platform's own webhook body read as the canonical event, or why it is not one.

    A bridge we wrote posts the canonical shape (`sender_id`, `sender_name`, `text`,
    `message_id`, optional `reply_url`/`priority`/`target`) and falls straight through. The
    channel adapters read Slack's Events API, the WhatsApp Cloud API, Feishu's v2 event, LINE's
    webhook, Telegram's update object and Messenger's webhook, so a real app can point at this
    endpoint without a translation service in between. Returns `{"ignore": reason}` for a payload
    that needs no turn (a delivery receipt, an event we do not act on) and `{"error": reason}`
    for one we cannot read.
    """
    return _default_registry().normalize(channel, event)


class IngressServer:
    def __init__(self, daemon: Any, host: str = "127.0.0.1", port: int = 8788,
                 token: Optional[str] = None, slack_secret: Optional[str] = None,
                 feishu_key: Optional[str] = None, line_secret: Optional[str] = None):
        self.daemon = daemon
        self.host = host
        self.port = port
        # Secrets come from the environment and are never written anywhere; a platform that
        # signs its webhooks needs its own secret, not the shared token.
        self.token = os.getenv("BOOM_INGRESS_TOKEN", "") if token is None else token
        self.slack_secret = (os.getenv("BOOM_SLACK_SIGNING_SECRET", "")
                             if slack_secret is None else slack_secret)
        self.feishu_key = os.getenv("BOOM_FEISHU_ENCRYPT_KEY", "") if feishu_key is None else feishu_key
        self.line_secret = (os.getenv("BOOM_LINE_CHANNEL_SECRET", "")
                            if line_secret is None else line_secret)
        self._server: Optional[asyncio.AbstractServer] = None
        # Each adapter verifies with the secret resolved here (None in the adapter means
        # "read the environment at call time", so constructor values always win).
        self.registry = build_default_registry()
        self.registry.get("slack").signing_secret = self.slack_secret
        self.registry.get("feishu").encrypt_key = self.feishu_key
        self.registry.get("line").channel_secret = self.line_secret

    # ------------------------------------------------------------------ authentication
    def _verify(self, channel: str, headers: dict, body: bytes) -> Optional[str]:
        """None when the request is authentic, else why it is refused.

        A channel with a signature secret configured needs that signature; every other channel
        needs the shared token when one is set. Both are compared in constant time, and the
        Slack timestamp is checked against a replay window so a captured request cannot be
        replayed later.
        """
        return self.registry.verify(channel, headers, body, shared_token=self.token)

    async def _read_request(self, reader: asyncio.StreamReader):
        """Request line, lowercased headers, body. Raises on a malformed or oversized request."""
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15.0)
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = (lines[0].split(" ") + ["", "", ""])[:3]
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length") or 0)
        if length > MAX_BODY:
            raise ValueError(f"body too large: {length}")
        body = await asyncio.wait_for(reader.readexactly(length), timeout=15.0) if length else b""
        return method, path, headers, body

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        status, payload = 500, {"error": "internal"}
        try:
            method, path, headers, body = await self._read_request(reader)
            status, payload = await self._route(method, path, headers, body)
        except ValueError as e:
            status, payload = 400, {"error": str(e)}
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            status, payload = 400, {"error": "malformed or incomplete request"}
        except Exception as e:  # a bad request must never kill the listener
            logger.warning("ingress request failed: %s: %s", type(e).__name__, e)
        finally:
            if not writer.is_closing():
                if isinstance(payload, str):
                    # Webhook verification challenges (Messenger's hub.challenge) must be
                    # answered as plain text, not JSON.
                    blob = payload.encode("utf-8")
                    content_type = "text/plain; charset=utf-8"
                else:
                    blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                writer.write(
                    f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\n"
                    f"Content-Type: {content_type}\r\n"
                    f"Content-Length: {len(blob)}\r\nConnection: close\r\n\r\n".encode("latin-1")
                    + blob
                )
                try:
                    await writer.drain()
                except Exception:
                    pass
            writer.close()

    async def _route(self, method: str, path: str, headers: dict, body: bytes):
        if path == "/health" and method == "GET":
            channels = ["ingress"]  # this listener is up whenever it answers at all
            if getattr(self.daemon, "tg_token", None):
                channels.insert(0, "telegram")
            if getattr(self.daemon, "dc_token", None):
                channels.append("discord")
            out = {"status": "ok", "channels": channels}
            # Heavy-load state belongs in health: how much is running, how much is waiting.
            dispatcher = getattr(self.daemon, "dispatcher", None)
            if dispatcher is not None:
                out["queue"] = dispatcher.status()
            return 200, out
        parts = urlsplit(path)
        if parts.path.startswith(_PREFIX) and len(parts.path) > len(_PREFIX):
            # GET on a webhook path is a verification handshake, but only for
            # channels that define one (Messenger's hub.challenge). Anything
            # else keeps the old 405, exactly like before.
            if method == "GET":
                channel = parts.path[len(_PREFIX):].strip("/")
                adapter = self.registry.get(channel)
                if (adapter is not None and type(adapter).verify_webhook
                        is not ChannelAdapter.verify_webhook):
                    challenge = adapter.verify_webhook(dict(parse_qsl(parts.query)))
                    if challenge is not None:
                        return 200, challenge
                    return 403, {"error": "webhook verification failed"}
        if method != "POST":
            return 405, {"error": "use POST"}
        if path == "/cancel":
            # The operator's handle on a turn that is already running; job ids come from /health.
            if self.token and headers.get("x-boom-token") != self.token:
                return 401, {"error": "bad or missing X-Boom-Token"}
            dispatcher = getattr(self.daemon, "dispatcher", None)
            if dispatcher is None:
                return 503, {"error": "no turn queue in this daemon"}
            try:
                request = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"error": "body must be a JSON object"}
            channel_to_stop = str((request or {}).get("channel") or "")
            if channel_to_stop:
                return 200, dispatcher.cancel_channel(channel_to_stop)
            job_id = str((request or {}).get("job_id") or "")
            if not job_id:
                return 400, {"error": "job_id or channel is required"}
            return 200, dispatcher.cancel(job_id)
        if not parts.path.startswith(_PREFIX) or len(parts.path) <= len(_PREFIX):
            return 404, {"error": f"unknown path {path}"}

        # Which secret authenticates a request depends on its channel, so the channel is resolved
        # before anything is parsed, and the raw body is what gets verified.
        channel = parts.path[len(_PREFIX):].strip("/")
        refusal = self._verify(channel, headers, body)
        if refusal:
            return 401, {"error": refusal}
        try:
            event = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, {"error": "body must be a JSON object"}
        if not isinstance(event, dict):
            return 400, {"error": "body must be a JSON object"}
        # With an Encrypt Key set, Feishu encrypts the whole body: it has to be opened before
        # anything can be read from it, and a body we cannot open is an error, not a turn.
        if channel == "feishu" and self.feishu_key and "encrypt" in event:
            try:
                event = _decrypt_feishu(self.feishu_key, str(event.get("encrypt") or ""))
            except Exception as e:
                return 400, {"error": f"cannot decrypt Feishu event: {type(e).__name__}: {e}"}
            if not isinstance(event, dict):
                return 400, {"error": "decrypted Feishu event is not an object"}
        # A platform's own ping is answered here, without a turn; its own payload shape is read
        # into the canonical event, so no translation bridge is needed in front of this route.
        handshake = _handshake(channel, event)
        if handshake is not None:
            return 200, handshake
        event = _normalize(channel, event)
        if "error" in event:
            return 400, event
        if "ignore" in event:
            # 200 on purpose: a platform retries anything it does not see accepted, and there is
            # nothing to retry about a delivery receipt.
            return 200, {"channel": channel, "ignored": event["ignore"]}
        text = str(event.get("text") or "").strip()
        if not text:
            return 400, {"error": "text is required"}

        # Reusing the caller's message_id is what makes a bridge retry safe: the ledger drops it.
        message_id = str(event.get("message_id") or "")
        if not message_id:
            return 400, {"error": "message_id is required (it is the idempotency key)"}
        sender_id = str(event.get("sender_id") or "")
        if not sender_id:
            return 400, {"error": "sender_id is required"}

        reply_url = str(event.get("reply_url") or "")

        async def deliver(reply_text: str, reply_url=reply_url):
            """Used later when the turn had to wait for a slot; the bridge learns it is queued."""
            error = await self._deliver(reply_url, channel, sender_id, message_id, reply_text)
            if error:
                # Raising is what puts this in the dispatcher's log: a reply the bridge never
                # received must not read as delivered.
                raise RuntimeError(error)

        # With no callback there is nowhere to put a later reply, so that turn runs in-band.
        reply = await self.daemon.handle_incoming_message(
            channel, sender_id, str(event.get("sender_name") or sender_id), text, message_id,
            deliver=deliver if reply_url else None,
            # The reply's destination, when the platform names one (Slack's channel, Feishu's
            # chat): a reply goes back where the message came from, not to its author.
            target=str(event.get("target") or sender_id),
            # A bridge that knows a message is urgent can say so; anything else is normal priority.
            priority=int(event.get("priority") or 0),
        )
        if reply and reply_url:
            error = await self._deliver(reply_url, channel, sender_id, message_id, reply)
            if error:
                # The bridge is down but the turn happened; hand the text back instead of losing it.
                return 200, {"channel": channel, "reply": reply, "delivered": False, "error": error}
            return 200, {"channel": channel, "reply": None, "delivered": True}
        return 200, {"channel": channel, "reply": reply}

    async def _deliver(self, url: str, channel: str, sender_id: str, message_id: str, text: str):
        """POST the reply to a bridge callback. Returns an error string, or None on success.

        Loopback only: this endpoint is reachable by whatever can reach the ingress, and an
        unrestricted URL here would let a caller make the agent POST its replies wherever it likes.
        """
        host = (urlsplit(url).hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            return f"reply_url must be loopback, got {host or url!r}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json={
                    "channel": channel, "sender_id": sender_id,
                    "message_id": message_id, "text": text,
                })
            if resp.status_code >= 300:
                return f"bridge returned HTTP {resp.status_code}"
            return None
        except Exception as e:
            return f"{type(e).__name__}: {e}"

    async def start(self) -> int:
        """Bind and take the real port (0 asks the OS for a free one). Returns it."""
        if self.host not in LOOPBACK and not (self.token or self.slack_secret or self.feishu_key
                                              or self.line_secret):
            # Anything that can reach this port can talk to the agent, so a listener that is not
            # on loopback must have something to authenticate callers with.
            raise ValueError(
                f"refusing to bind {self.host} with no BOOM_INGRESS_TOKEN or channel signature "
                "secret: anything on the network could talk to the agent"
            )
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def serve(self):
        await self.start()
        logger.info("Ingress listening on http://%s:%s%s<channel>", self.host, self.port, _PREFIX)
        await self._server.serve_forever()

    def stop(self):
        if self._server is not None:
            self._server.close()

    async def aclose(self):
        self.stop()
        if self._server is not None:
            await self._server.wait_closed()


_REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
            405: "Method Not Allowed", 500: "Internal Server Error",
            503: "Service Unavailable"}
