"""The Discord channel against a real peer: a real TLS WebSocket server speaking Discord's opcodes.

Nothing here stands in for our own code. The client opens a real `wss://` socket over a real
certificate (generated for this run, verified normally by the client), the server answers with the
frames Discord's gateway sends, and the REST calls hit a real HTTP listener that records them. So a
pass means the client speaks the protocol: identify, heartbeat, ignore its own messages, dispatch a
human message through the daemon, and post the reply back.

Out of scope here: the agent loop behind `runner_fn` (its own evals cover it).
"""

import asyncio
import base64
import datetime
import hashlib
import ipaddress
import json
import ssl
import struct
import sys
import tempfile
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))
sys.path.insert(0, str(root_dir / "agent" / "src"))

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from gateway.daemon import GatewayDaemon
from gateway.delivery_ledger import DeliveryLedger
from gateway.discord import INTENTS, MAX_MESSAGE, DiscordChannel
from tools.ws_client import GUID, OP_CLOSE, OP_TEXT, encode_frame

TOKEN = "discord-token-for-test"
BOT_ID = "900000000000000001"
HUMAN_ID = "900000000000000002"


def make_cert(cert_path: Path, key_path: Path):
    """A certificate for 127.0.0.1, so the client verifies it the normal way."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))


async def server_context(reader, writer):
    """Server-side RFC6455 handshake; raises on a request that is not an upgrade."""
    head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
    key = ""
    for line in head.split("\r\n")[1:]:
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    if not key:
        raise ValueError("no Sec-WebSocket-Key")
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
    writer.write(
        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
        b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept.encode("ascii") + b"\r\n\r\n"
    )
    await writer.drain()


async def read_frame(reader):
    """One client frame: (opcode, text). Client frames must be masked."""
    b0, b1 = await reader.readexactly(2)
    length, masked = b1 & 0x7F, bool(b1 & 0x80)
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    assert masked, "a client frame arrived unmasked"
    key = await reader.readexactly(4)
    payload = await reader.readexactly(length) if length else b""
    payload = bytes(b ^ key[i & 3] for i, b in enumerate(payload))
    return b0 & 0x0F, payload.decode("utf-8")


class DiscordPeer:
    """The gateway end Discord would run, for one session."""

    def __init__(self):
        self.identify = None
        self.heartbeats = 0
        self.closed = False
        self.done = asyncio.Event()  # set once the human message has been dispatched

    async def handle(self, reader, writer):
        try:
            await server_context(reader, writer)
            self._send(writer, {"op": 10, "d": {"heartbeat_interval": 100}})
            await writer.drain()

            while True:
                opcode, raw = await read_frame(reader)
                if opcode == OP_CLOSE:
                    break
                msg = json.loads(raw)
                op = msg.get("op")
                if op == 2:  # IDENTIFY -> READY, then the two messages Discord would dispatch
                    self.identify = msg.get("d") or {}
                    self._send(writer, {"op": 0, "s": 1, "t": "READY", "d": {
                        "session_id": "sess-1", "user": {"id": BOT_ID, "username": "boom"}}})
                    # Our own reply echoes back on the same socket; it must be ignored.
                    self._send(writer, {"op": 0, "s": 2, "t": "MESSAGE_CREATE", "d": {
                        "id": "m-bot", "channel_id": "chan-1", "content": "echo:hi",
                        "author": {"id": BOT_ID, "username": "boom", "bot": True}}})
                    self._send(writer, {"op": 0, "s": 3, "t": "MESSAGE_CREATE", "d": {
                        "id": "m-human", "channel_id": "chan-1", "content": "hi",
                        "author": {"id": HUMAN_ID, "username": "ryan"}}})
                    await writer.drain()
                elif op == 1:  # heartbeat -> ACK
                    self.heartbeats += 1
                    self._send(writer, {"op": 11, "d": None})
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, ValueError):
            pass
        finally:
            self.closed = True
            writer.close()

    @staticmethod
    def _send(writer, payload: dict):
        writer.write(encode_frame(json.dumps(payload), OP_TEXT, mask=False))


class RestSink:
    """Stands in for discord.com/api: serves the gateway lookup and records message posts."""

    def __init__(self):
        self.posts = []
        self.headers = []
        self.gateway_url = ""
        self.replied = asyncio.Event()

    async def handle(self, reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            lines = head.split("\r\n")
            method, path = lines[0].split(" ")[0], lines[0].split(" ")[1]
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length") or 0)
            body = json.loads(await reader.readexactly(length)) if length else {}
            if path == "/gateway/bot":
                status, out = 200, {"url": self.gateway_url}
            elif method == "POST" and path.endswith("/messages"):
                self.posts.append((path, body))
                self.headers.append(headers.get("authorization"))
                self.replied.set()
                status, out = 200, {"id": "sent-1"}
            else:
                status, out = 404, {"error": "not found"}
            blob = json.dumps(out).encode()
            writer.write(
                f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(blob)}\r\nConnection: close\r\n\r\n".encode() + blob
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, ValueError):
            pass
        finally:
            writer.close()


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        cert_path, key_path = Path(tmp) / "cert.pem", Path(tmp) / "key.pem"
        make_cert(cert_path, key_path)

        peer = DiscordPeer()
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(str(cert_path), str(key_path))
        ws_server = await asyncio.start_server(peer.handle, "127.0.0.1", 0, ssl=server_ctx)
        ws_port = ws_server.sockets[0].getsockname()[1]

        sink = RestSink()
        sink.gateway_url = f"wss://127.0.0.1:{ws_port}"
        rest_server = await asyncio.start_server(sink.handle, "127.0.0.1", 0)
        rest_port = rest_server.sockets[0].getsockname()[1]

        prompts = []

        async def runner(text: str, channel: str, sender: str) -> str:
            prompts.append((text, channel, sender))
            return "x" * 3000 if text == "long" else f"echo:{text}"

        daemon = GatewayDaemon(runner_fn=runner, delivery_ledger=DeliveryLedger(":memory:"))
        # The client verifies the certificate normally; only the trust anchor is local.
        channel = DiscordChannel(
            daemon, TOKEN, api_base=f"http://127.0.0.1:{rest_port}",
            ssl_context=ssl.create_default_context(cafile=str(cert_path)), timeout=5.0,
        )
        task = asyncio.create_task(channel.run())
        await asyncio.wait_for(sink.replied.wait(), 20)

        assert peer.identify, "client never sent IDENTIFY"
        assert peer.identify["token"] == TOKEN, peer.identify["token"]
        assert peer.identify["intents"] == INTENTS, peer.identify["intents"]
        assert peer.identify.get("properties"), peer.identify
        print("[PASS] wss:// connect over a verified certificate; IDENTIFY carried token+intents.")

        assert prompts == [("hi", "discord", "ryan")], prompts
        assert sink.headers == [f"Bot {TOKEN}"], sink.headers
        assert sink.posts == [("/channels/chan-1/messages", {"content": "echo:hi"})], sink.posts
        print("[PASS] Bot's own message ignored; the human's ran the daemon and replied via REST.")

        assert peer.heartbeats >= 1, peer.heartbeats
        print(f"[PASS] {peer.heartbeats} heartbeat(s) sent, ACKed by the peer.")

        # A reply longer than Discord's limit must be cut, not rejected by the API.
        reply = "x" * 3000
        await channel.send_message("chan-2", reply)
        assert sink.posts[-1] == ("/channels/chan-2/messages",
                                  {"content": "x" * MAX_MESSAGE}), len(sink.posts[-1][1]["content"])
        print(f"[PASS] A {len(reply)}-char reply was truncated to {MAX_MESSAGE}.")

        channel.stop()
        ws_server.close()
        rest_server.close()
        await asyncio.wait_for(task, 15)
        assert peer.closed, "the peer never saw the session end"

    print("ALL DISCORD CHANNEL TESTS PASSED.")


if __name__ == "__main__":
    asyncio.run(main())
