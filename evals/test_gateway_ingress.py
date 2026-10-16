"""Offline check of the HTTP ingress: real loopback socket, real requests over it.

The point of this route is that a bridge we did not write (Zalo, Slack, anything) can push a
message in and get the agent's reply back. So the assertions are about the wire contract: the
ledger drops a retried message_id, a bad token or body is refused, and the happy path actually
reaches the runner.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import socket
import sys
import time
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))
sys.path.insert(0, str(root_dir / "agent" / "src"))

import httpx

# Hermetic localhost tests: the code under test builds its own httpx clients
# (gateway/channel.py, mcp/client.py), and a default httpx client reads the
# ambient proxy env. This sandbox's no_proxy contains *bracketed* IPv6 entries
# ([::1], [fd8b:...]); httpx turns those into mount keys like "all://*[::1]"
# which fail URL parsing at Client construction
# (httpx.InvalidURL: Invalid port: ':1]'). Unbracket valid IPv6 literals so
# httpx parses them; the set of bypassed hosts is unchanged.
def _sanitize_no_proxy():
    import ipaddress as _ipaddress
    import os as _os

    def _norm(entry):
        e = entry.strip()
        if e.startswith("[") and e.endswith("]"):
            try:
                _ipaddress.ip_address(e[1:-1])
                return e[1:-1]
            except ValueError:
                pass
        return e

    for _var in ("no_proxy", "NO_PROXY"):
        _val = _os.environ.get(_var)
        if _val:
            _os.environ[_var] = ",".join(_norm(p) for p in _val.split(","))


_sanitize_no_proxy()

from gateway.daemon import GatewayDaemon
from gateway.delivery_ledger import DeliveryLedger
from gateway.dispatcher import TurnDispatcher
from gateway.ingress import SLACK_WINDOW, IngressServer

TOKEN = "s3cret"
HDR = {"X-Boom-Token": TOKEN}


async def main():
    seen = []

    async def runner(text: str, channel: str, sender: str) -> str:
        seen.append((text, channel, sender))
        return f"echo:{text}"

    daemon = GatewayDaemon(runner_fn=runner, delivery_ledger=DeliveryLedger(":memory:"))
    server = IngressServer(daemon, port=0, token=TOKEN)
    port = await server.start()
    base = f"http://127.0.0.1:{port}"

    def event(**over):
        base_event = {"sender_id": "u1", "sender_name": "Ryan", "text": "hi", "message_id": "m1"}
        base_event.update(over)
        return base_event

    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        r = await client.get(f"{base}/health")
        assert r.status_code == 200 and r.json()["status"] == "ok", r.text
        print("[PASS] GET /health answered.")

        # 1. Happy path: the message reaches the runner and its reply comes back.
        r = await client.post(f"{base}/webhook/discord", json=event(), headers=HDR)
        assert r.status_code == 200, r.text
        assert r.json()["reply"] == "echo:hi", r.json()
        assert seen == [("hi", "discord", "Ryan")], seen
        print("[PASS] POST /webhook/discord reached the runner and returned its reply.")

        # 2. The same message_id again is a bridge retry, not a second turn.
        r = await client.post(f"{base}/webhook/discord", json=event(), headers=HDR)
        assert r.status_code == 200 and r.json()["reply"] is None, r.json()
        assert len(seen) == 1, seen
        print("[PASS] Retried message_id suppressed by the DeliveryLedger.")

        # 3. The idempotency key includes the channel, so two platforms do not collide.
        r = await client.post(f"{base}/webhook/zalo", json=event(), headers=HDR)
        assert r.status_code == 200 and r.json()["reply"] == "echo:hi", r.json()
        assert len(seen) == 2, seen
        print("[PASS] Same message_id on another channel is a separate turn.")

        # 4. Refusals: auth, routing, verb, and an unusable body.
        r = await client.post(f"{base}/webhook/discord", json=event(message_id="m2"))
        assert r.status_code == 401, r.text
        r = await client.post(f"{base}/nope", json=event(message_id="m3"), headers=HDR)
        assert r.status_code == 404, r.text
        r = await client.get(f"{base}/webhook/discord", headers=HDR)
        assert r.status_code == 405, r.text
        for bad, why in (
            (event(message_id="m4", text="   "), "empty text"),
            (event(message_id=""), "missing message_id"),
            (event(message_id="m5", sender_id=""), "missing sender_id"),
        ):
            r = await client.post(f"{base}/webhook/discord", json=bad, headers=HDR)
            assert r.status_code == 400, f"{why}: {r.status_code} {r.text}"
        r = await client.post(f"{base}/webhook/discord", content=b"not json", headers=HDR)
        assert r.status_code == 400, r.text
        r = await client.post(f"{base}/webhook/discord", json=event(message_id="m6", text="x" * 70000),
                              headers=HDR)
        assert r.status_code == 400, r.status_code
        assert len(seen) == 2, seen  # nothing bad got through to the runner
        print("[PASS] 401/404/405/400 refusals: no bad request reached the runner.")

        # 5. A bridge that cannot hold the connection gives us a callback instead.
        received = []

        async def sink(reader, writer):
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            length = 0
            for line in head.split("\r\n")[1:]:
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1])
            received.append(json.loads(await reader.readexactly(length)))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
            writer.close()

        sink_server = await asyncio.start_server(sink, "127.0.0.1", 0)
        sink_port = sink_server.sockets[0].getsockname()[1]
        hook = f"http://127.0.0.1:{sink_port}/reply"

        r = await client.post(f"{base}/webhook/zalo",
                              json=event(message_id="m7", reply_url=hook), headers=HDR)
        assert r.status_code == 200 and r.json() == {"channel": "zalo", "reply": None,
                                                     "delivered": True}, r.json()
        assert received == [{"channel": "zalo", "sender_id": "u1", "message_id": "m7",
                             "text": "echo:hi"}], received
        print("[PASS] reply_url delivered the reply to the bridge callback.")

        # A callback off this machine would let a caller aim the agent's replies anywhere.
        r = await client.post(f"{base}/webhook/zalo",
                              json=event(message_id="m8", reply_url="http://example.com/hook"),
                              headers=HDR)
        assert r.status_code == 200 and r.json()["delivered"] is False, r.json()
        assert "loopback" in r.json()["error"], r.json()
        assert r.json()["reply"] == "echo:hi", r.json()
        print("[PASS] Non-loopback reply_url refused, reply kept in the response.")

        # A dead bridge must not swallow the turn: the text comes back in the response.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        r = await client.post(f"{base}/webhook/zalo",
                              json=event(message_id="m9",
                                         reply_url=f"http://127.0.0.1:{dead_port}/reply"),
                              headers=HDR)
        assert r.status_code == 200 and r.json()["delivered"] is False, r.json()
        assert r.json()["reply"] == "echo:hi" and r.json()["error"], r.json()
        print("[PASS] Unreachable bridge reported, not silently dropped.")

        sink_server.close()
        await sink_server.wait_closed()

        # 6. The cancel handle needs a queue; this daemon has none, and says so instead of
        # pretending. It is behind the same token as everything else.
        r = await client.post(f"{base}/cancel", json={"job_id": "x"}, headers=HDR)
        assert r.status_code == 503 and "queue" in r.json()["error"], r.text
        r = await client.post(f"{base}/cancel", json={"job_id": "x"})
        assert r.status_code == 401, r.text
        print("[PASS] POST /cancel is token-guarded and refuses when there is no turn queue.")

        # 7. With the real turn queue attached, /cancel reaches it, and a turn for a channel the
        # daemon cannot reach on its own waits for its reply instead of being acked.
        queue = TurnDispatcher(runner, concurrency=1, max_queued=4)
        await queue.start()
        daemon.dispatcher = queue
        try:
            r = await client.post(f"{base}/cancel", json={"channel": "zalo"}, headers=HDR)
            assert r.json() == {"channel": "zalo", "queued": 0, "running": 0}, r.json()
            r = await client.post(f"{base}/cancel", json={"job_id": "nosuchjob"}, headers=HDR)
            assert r.json()["status"] == "unknown", r.json()
            r = await client.post(f"{base}/cancel", json={}, headers=HDR)
            assert r.status_code == 400, r.text

            r = await client.post(f"{base}/webhook/zalo",
                                  json=event(message_id="m10", text="after", priority=3), headers=HDR)
            assert r.json()["reply"] == "echo:after", r.json()
            job = list(queue.jobs.values())[-1]
            assert (job.text, job.priority, job.target) == ("after", 3, "u1"), job
        finally:
            daemon.dispatcher = None
            await queue.stop()
        print("[PASS] /cancel reached the queue; a waiting turn answered in-band with its priority.")

        # 8. A platform that signs its own webhooks is authenticated by that signature, over the
        # raw body, not by the shared token.
        signed = IngressServer(daemon, port=0, token="", slack_secret="slack-secret",
                               feishu_key="feishu-key")
        sbase = f"http://127.0.0.1:{await signed.start()}"

        def slack_headers(raw: bytes, stamp=None, secret="slack-secret") -> dict:
            stamp = stamp or str(int(time.time()))
            mac = hmac.new(secret.encode(), b"v0:" + stamp.encode() + b":" + raw,
                           hashlib.sha256).hexdigest()
            return {"X-Slack-Signature": f"v0={mac}", "X-Slack-Request-Timestamp": stamp}

        raw = json.dumps(event(message_id="sig1", text="signed")).encode()
        r = await client.post(f"{sbase}/webhook/slack", content=raw, headers=slack_headers(raw))
        assert r.status_code == 200 and r.json()["reply"] == "echo:signed", r.text

        stale = str(int(time.time()) - SLACK_WINDOW - 5)
        r = await client.post(f"{sbase}/webhook/slack", content=raw,
                              headers=slack_headers(raw, stamp=stale))
        assert r.status_code == 401 and "stale" in r.json()["error"], r.text
        r = await client.post(f"{sbase}/webhook/slack", content=raw,
                              headers=slack_headers(raw, secret="wrong-secret"))
        assert r.status_code == 401 and "signature" in r.json()["error"], r.text
        r = await client.post(f"{sbase}/webhook/slack", content=raw)
        assert r.status_code == 401, r.text
        # A signature lifted from one body must not authenticate another.
        r = await client.post(f"{sbase}/webhook/slack", content=b'{"text":"evil"}',
                              headers=slack_headers(raw))
        assert r.status_code == 401, r.text

        lark = json.dumps(event(message_id="sig2", text="lark")).encode()
        stamp, nonce = str(int(time.time())), "nonce1"
        lark_sig = hashlib.sha256((stamp + nonce + "feishu-key").encode() + lark).hexdigest()
        r = await client.post(f"{sbase}/webhook/feishu", content=lark, headers={
            "X-Lark-Signature": lark_sig, "X-Lark-Request-Timestamp": stamp,
            "X-Lark-Request-Nonce": nonce})
        assert r.status_code == 200 and r.json()["reply"] == "echo:lark", r.text
        r = await client.post(f"{sbase}/webhook/feishu", content=lark, headers={
            "X-Lark-Signature": "0" * 64, "X-Lark-Request-Timestamp": stamp,
            "X-Lark-Request-Nonce": nonce})
        assert r.status_code == 401 and "Feishu" in r.json()["error"], r.text
        assert len([s for s in seen if s[0] in ("signed", "lark")]) == 2, seen
        await signed.aclose()
        print("[PASS] Slack and Feishu signatures verified; stale, wrong and borrowed ones refused.")

        # 9. A listener that is not on loopback must have something to authenticate callers with.
        unsafe = IngressServer(daemon, host="0.0.0.0", port=0, token="", slack_secret="",
                               feishu_key="")
        try:
            await unsafe.start()
            raise AssertionError("binding 0.0.0.0 with no secret must be refused")
        except ValueError as e:
            assert "refusing to bind" in str(e), e
        finally:
            await unsafe.aclose()
        print("[PASS] Binding 0.0.0.0 refused without a token or a signature secret.")

        # 10. A channel that only pushes to us can still be answered, when its token is set:
        # Slack's chat.postMessage and the WhatsApp Cloud API, posted to their real endpoints.
        posts = []
        answer = {"body": {"ok": True}, "status": 200}
        answers = {}  # per-path replies, for an API whose token call and send call differ

        async def api_sink(reader, writer):
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            length = 0
            for line in head.split("\r\n")[1:]:
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1])
            lines = head.split("\r\n")
            path = lines[0].split(" ")[1]
            posts.append((
                path,
                next((v.split(":", 1)[1].strip() for v in lines
                      if v.lower().startswith("authorization:")), ""),
                json.loads(await reader.readexactly(length)) if length else {},
            ))
            reply = answers.get(path, answer)
            blob = json.dumps(reply["body"]).encode()
            writer.write(
                f"HTTP/1.1 {reply['status']} X\r\nContent-Length: {len(blob)}\r\n"
                f"Connection: close\r\n\r\n".encode("latin-1") + blob
            )
            await writer.drain()
            writer.close()

        api_server = await asyncio.start_server(api_sink, "127.0.0.1", 0)
        api_base = f"http://127.0.0.1:{api_server.sockets[0].getsockname()[1]}"

        # No token means no route: a Slack turn then waits for its reply instead of being acked.
        assert daemon.can_deliver("slack") is False
        os.environ.update({
            "BOOM_SLACK_BOT_TOKEN": "xoxb-test", "BOOM_SLACK_API_BASE": api_base,
            "BOOM_WHATSAPP_TOKEN": "wa-test", "BOOM_WHATSAPP_PHONE_ID": "1234",
            "BOOM_WHATSAPP_API_BASE": api_base,
        })
        try:
            assert daemon.can_deliver("slack") and daemon.can_deliver("whatsapp")
            assert await daemon.send_to("slack", "C42", "hello slack") is True
            assert posts[-1] == ("/chat.postMessage", "Bearer xoxb-test",
                                 {"channel": "C42", "text": "hello slack"}), posts[-1]

            # Slack answers 200 with {"ok": false} when it refused: that is not a delivery.
            answer["body"] = {"ok": False, "error": "channel_not_found"}
            assert await daemon.send_to("slack", "C42", "again") is False
            answer.update({"body": {"ok": True}, "status": 500})
            assert await daemon.send_to("slack", "C42", "again") is False

            answer.update({"body": {"messages": [{"id": "wamid"}]}, "status": 200})
            assert await daemon.send_to("whatsapp", "8490", "hello wa") is True
            assert posts[-1] == ("/1234/messages", "Bearer wa-test",
                                 {"messaging_product": "whatsapp", "to": "8490", "type": "text",
                                  "text": {"body": "hello wa"}}), posts[-1]
            answer["status"] = 400
            assert await daemon.send_to("whatsapp", "8490", "nope") is False
        finally:
            for name in ("BOOM_SLACK_BOT_TOKEN", "BOOM_SLACK_API_BASE", "BOOM_WHATSAPP_TOKEN",
                         "BOOM_WHATSAPP_PHONE_ID", "BOOM_WHATSAPP_API_BASE"):
                os.environ.pop(name, None)
            api_server.close()
            await api_server.wait_closed()
        print("[PASS] Slack and WhatsApp replies posted with the real payload; a refused one is False.")

        # 11. A real Slack, WhatsApp, Feishu or LINE app can point its webhook straight at this
        # route: the platform's own body is read into the same event, and the platform's own
        # signature authenticates it. A payload needing no turn is accepted and ignored, because
        # a platform retries anything it does not see accepted.
        live = IngressServer(daemon, port=0, token="", slack_secret="slack-secret",
                             feishu_key="feishu-key", line_secret="line-secret")
        lbase = f"http://127.0.0.1:{await live.start()}"
        handled = len(seen)

        raw = json.dumps({"type": "url_verification", "challenge": "chal-1"}).encode()
        r = await client.post(f"{lbase}/webhook/slack", content=raw, headers=slack_headers(raw))
        assert r.status_code == 200 and r.json() == {"challenge": "chal-1"}, r.text
        assert len(seen) == handled, "a handshake must not run a turn"

        # Slack's message event goes through the turn queue, and the reply is addressed to the
        # channel it came from rather than to its author.
        queue2 = TurnDispatcher(runner, concurrency=1, max_queued=4)
        await queue2.start()
        daemon.dispatcher = queue2
        try:
            raw = json.dumps({"type": "event_callback", "event": {
                "type": "message", "user": "U7", "text": "hello from slack",
                "ts": "1700000000.0001", "channel": "C42"}}).encode()
            r = await client.post(f"{lbase}/webhook/slack", content=raw, headers=slack_headers(raw))
            assert r.status_code == 200 and r.json()["reply"] == "echo:hello from slack", r.text
            assert seen[-1] == ("hello from slack", "slack", "U7"), seen[-1]
            job = list(queue2.jobs.values())[-1]
            assert (job.channel, job.target, job.message_id) == ("slack", "C42", "1700000000.0001"), job
        finally:
            daemon.dispatcher = None
            await queue2.stop()

        # Our own reply arrives as a message event too; acting on it would loop forever.
        for ignored in ({"type": "message", "bot_id": "B1", "text": "mine"},
                        {"type": "message", "subtype": "message_changed", "text": "edited"}):
            raw = json.dumps({"type": "event_callback", "event": ignored}).encode()
            r = await client.post(f"{lbase}/webhook/slack", content=raw, headers=slack_headers(raw))
            assert r.status_code == 200 and "ignored" in r.json(), r.text
        assert len(seen) == handled + 1, seen[handled:]

        raw = json.dumps({"entry": [{"changes": [{"value": {
            "metadata": {"display_phone_number": "1555"},
            "contacts": [{"profile": {"name": "Ryan"}, "wa_id": "8490"}],
            "messages": [{"from": "8490", "id": "wamid.1", "type": "text",
                          "text": {"body": "hello from whatsapp"}}]}}]}]}).encode()
        r = await client.post(f"{lbase}/webhook/whatsapp", content=raw)
        assert r.status_code == 200 and r.json()["reply"] == "echo:hello from whatsapp", r.text
        assert seen[-1] == ("hello from whatsapp", "whatsapp", "Ryan"), seen[-1]

        # The payload Slack/WhatsApp really send when nothing happened.
        raw = json.dumps({"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid"}]}}]}]}).encode()
        r = await client.post(f"{lbase}/webhook/whatsapp", content=raw)
        assert r.status_code == 200 and "ignored" in r.json(), r.text

        # Feishu's v2 event carries the text as a JSON string inside a JSON body.
        stamp, nonce = str(int(time.time())), "nonce2"

        def feishu_headers(body: bytes) -> dict:
            return {"X-Lark-Signature": hashlib.sha256(
                (stamp + nonce + "feishu-key").encode() + body).hexdigest(),
                "X-Lark-Request-Timestamp": stamp, "X-Lark-Request-Nonce": nonce}

        raw = json.dumps({"schema": "2.0", "header": {"event_type": "im.message.receive_v1"},
                          "event": {"sender": {"sender_id": {"open_id": "ou_1"}},
                                    "message": {"message_id": "om_1", "chat_id": "oc_1",
                                                "message_type": "text",
                                                "content": json.dumps({"text": "hello from feishu"})}}
                          }).encode()
        r = await client.post(f"{lbase}/webhook/feishu", content=raw, headers=feishu_headers(raw))
        assert r.status_code == 200 and r.json()["reply"] == "echo:hello from feishu", r.text
        assert seen[-1] == ("hello from feishu", "feishu", "ou_1"), seen[-1]

        # With an Encrypt Key set, Feishu seals the whole body. The key is already here, so the
        # event must be opened and answered rather than refused.
        def feishu_encrypt(payload: dict, key: str = "feishu-key") -> bytes:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            body = json.dumps(payload).encode()
            pad = 16 - len(body) % 16
            iv = os.urandom(16)
            encryptor = Cipher(algorithms.AES(hashlib.sha256(key.encode()).digest()),
                               modes.CBC(iv)).encryptor()
            sealed = encryptor.update(body + bytes([pad]) * pad) + encryptor.finalize()
            return json.dumps({"encrypt": base64.b64encode(iv + sealed).decode()}).encode()

        raw = feishu_encrypt({"schema": "2.0", "header": {"event_type": "im.message.receive_v1"},
                              "event": {"sender": {"sender_id": {"open_id": "ou_9"}},
                                        "message": {"message_id": "om_9", "chat_id": "oc_9",
                                                    "message_type": "text",
                                                    "content": json.dumps({"text": "sealed"})}}})
        r = await client.post(f"{lbase}/webhook/feishu", content=raw, headers=feishu_headers(raw))
        assert r.status_code == 200 and r.json()["reply"] == "echo:sealed", r.text
        assert seen[-1] == ("sealed", "feishu", "ou_9"), seen[-1]
        assert len(seen) == handled + 4, seen[handled:]

        # The endpoint-verification ping arrives sealed too, and is answered without a turn.
        raw = feishu_encrypt({"type": "url_verification", "challenge": "chal-sealed"})
        r = await client.post(f"{lbase}/webhook/feishu", content=raw, headers=feishu_headers(raw))
        assert r.status_code == 200 and r.json() == {"challenge": "chal-sealed"}, r.text
        assert len(seen) == handled + 4, seen[handled:]

        # A body the key cannot open is an error: a turn answered from garbage is worse than none.
        raw = json.dumps({"encrypt": base64.b64encode(bytes(16)).decode()}).encode()
        r = await client.post(f"{lbase}/webhook/feishu", content=raw, headers=feishu_headers(raw))
        assert r.status_code == 400 and "cannot decrypt" in r.json()["error"], r.text
        assert len(seen) == handled + 4, seen[handled:]

        # LINE signs with its channel secret, not with the shared token.
        raw = json.dumps({"destination": "Ubot", "events": [{
            "type": "message", "replyToken": "rt", "timestamp": 1700000000000,
            "source": {"type": "user", "userId": "U9"},
            "message": {"type": "text", "id": "line-1", "text": "hello from line"}}]}).encode()

        def line_headers(body: bytes, secret: str = "line-secret") -> dict:
            return {"X-Line-Signature": base64.b64encode(
                hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()}

        r = await client.post(f"{lbase}/webhook/line", content=raw,
                              headers=line_headers(raw, "wrong-secret"))
        assert r.status_code == 401 and "LINE" in r.json()["error"], r.text
        r = await client.post(f"{lbase}/webhook/line", content=raw)
        assert r.status_code == 401, r.text
        r = await client.post(f"{lbase}/webhook/line", content=raw, headers=line_headers(raw))
        assert r.status_code == 200 and r.json()["reply"] == "echo:hello from line", r.text
        assert seen[-1] == ("hello from line", "line", "U9"), seen[-1]

        # LINE wants the webhook answered fast and the reply pushed, so a LINE turn is acked and
        # answered through the push API.
        line_server = await asyncio.start_server(api_sink, "127.0.0.1", 0)
        line_base = f"http://127.0.0.1:{line_server.sockets[0].getsockname()[1]}"
        answer.update({"body": {"sentMessages": [{"id": "1"}]}, "status": 200})
        os.environ.update({"BOOM_LINE_CHANNEL_TOKEN": "line-token", "BOOM_LINE_API_BASE": line_base})
        try:
            assert daemon.can_deliver("line") is True
            assert await daemon.send_to("line", "U9", "pushed to line") is True
            assert posts[-1] == ("/v2/bot/message/push", "Bearer line-token",
                                 {"to": "U9", "messages": [{"type": "text",
                                                            "text": "pushed to line"}]}), posts[-1]
            answer["status"] = 400
            assert await daemon.send_to("line", "U9", "nope") is False
        finally:
            for name in ("BOOM_LINE_CHANNEL_TOKEN", "BOOM_LINE_API_BASE"):
                os.environ.pop(name, None)
            line_server.close()
            await line_server.wait_closed()

        # Feishu takes a tenant_access_token it issues itself, and answers HTTP 200 with a
        # nonzero code when it refused the send: neither may read as delivered.
        feishu_server = await asyncio.start_server(api_sink, "127.0.0.1", 0)
        feishu_base = f"http://127.0.0.1:{feishu_server.sockets[0].getsockname()[1]}"
        answers.clear()
        answers["/open-apis/auth/v3/tenant_access_token/internal"] = {
            "status": 200, "body": {"code": 0, "tenant_access_token": "t-1", "expire": 7200}}
        answer.update({"body": {"code": 0}, "status": 200})
        os.environ.update({"BOOM_FEISHU_APP_ID": "cli_1", "BOOM_FEISHU_APP_SECRET": "app-secret",
                           "BOOM_FEISHU_API_BASE": feishu_base})
        try:
            assert daemon.can_deliver("feishu") is True
            assert await daemon.send_to("feishu", "oc_1", "pushed to feishu") is True
            assert posts[-2] == ("/open-apis/auth/v3/tenant_access_token/internal", "",
                                 {"app_id": "cli_1", "app_secret": "app-secret"}), posts[-2]
            assert posts[-1] == ("/open-apis/im/v1/messages?receive_id_type=chat_id", "Bearer t-1",
                                 {"receive_id": "oc_1", "msg_type": "text",
                                  "content": json.dumps({"text": "pushed to feishu"})}), posts[-1]
            # A second send reuses the token: Feishu would rate-limit a fetch per message.
            before = len(posts)
            assert await daemon.send_to("feishu", "oc_1", "again") is True
            assert len(posts) == before + 1, posts[before:]
            answer["body"] = {"code": 99991663, "msg": "no permission"}
            assert await daemon.send_to("feishu", "oc_1", "nope") is False
        finally:
            for name in ("BOOM_FEISHU_APP_ID", "BOOM_FEISHU_APP_SECRET", "BOOM_FEISHU_API_BASE"):
                os.environ.pop(name, None)
            feishu_server.close()
            await feishu_server.wait_closed()
        await live.aclose()
        print("[PASS] Real Slack/WhatsApp/Feishu/LINE bodies read; LINE and Feishu replies pushed.")

    await server.aclose()
    print("ALL GATEWAY INGRESS TESTS PASSED.")


if __name__ == "__main__":
    asyncio.run(main())
