"""The whole path in one process, with only the model faked.

A webhook arrives over a real loopback socket, goes through the daemon's own ingress, owner
guard, delivery ledger, turn queue and ReAct loop, and reaches a loopback HTTP server speaking
the real OpenAI wire format — so every component the daemon runs is the real one, and the only
stub is the model itself. What is asserted is the reply the caller gets and what the model was
actually asked.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
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

from environment import AgentEnvironment
from gateway.daemon import GatewayDaemon
from gateway.delivery_ledger import DeliveryLedger
from loop.react import ReActLoop
from providers.llm import LLMProvider

TOKEN = "e2e-token"
HDR = {"X-Boom-Token": TOKEN}


class FakeModel:
    """OpenAI-compatible /chat/completions on loopback. Records what it was asked."""

    def __init__(self, reply: str):
        self.reply = reply
        self.requests = []
        self._server = None

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}/v1"

    async def _handle(self, reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        length = 0
        for line in head.split("\r\n")[1:]:
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1])
        raw = await reader.readexactly(length) if length else b""
        self.requests.append((head.split(" ")[1], json.loads(raw or b"{}")))
        blob = json.dumps({
            "id": "chatcmpl-fake", "object": "chat.completion", "model": "fake-model",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": self.reply}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        }).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                     + str(len(blob)).encode() + b"\r\nConnection: close\r\n\r\n" + blob)
        await writer.drain()
        writer.close()

    async def aclose(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


async def wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def main():
    work = Path(tempfile.mkdtemp(prefix="boom_e2e_"))
    model = FakeModel("pong from the model")
    base_url = await model.start()

    # The daemon must not try to poll a real platform: these come from the environment.
    for name in ("TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN"):
        os.environ.pop(name, None)
    os.environ["BOOM_INGRESS_PORT"] = "0"  # the OS picks the port, so the suite can run twice

    env = AgentEnvironment(root_dir=work, db_path=work / "state.db", yolo=True)
    llm = LLMProvider(base_url=base_url, api_key="test-key", model="fake-model", timeout=15.0)
    react = ReActLoop(env, llm, accountant=env.token_accountant, session_id="e2e-session")
    env.loop = react  # cron_run needs one; the gateway path does not, but the env is shared

    async def runner(text: str, channel: str, sender: str) -> str:
        result = await react.step(text)
        return result.get("action") or result.get("observation") or "Done."

    daemon = GatewayDaemon(runner_fn=runner, delivery_ledger=DeliveryLedger(":memory:"),
                           root_dir=work)
    serving = asyncio.create_task(daemon.start())
    try:
        assert await wait_until(lambda: daemon.ingress is not None and daemon.ingress.port != 0), \
            "the daemon never bound its ingress"
        base = f"http://127.0.0.1:{daemon.ingress.port}"

        def event(**over):
            payload = {"sender_id": "u1", "sender_name": "Ryan", "text": "ping",
                       "message_id": "e2e-1"}
            payload.update(over)
            return payload

        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            r = await client.get(f"{base}/health")
            assert r.status_code == 200 and r.json()["status"] == "ok", r.text

            # The whole chain: webhook -> guard -> ledger -> queue -> loop -> provider -> reply.
            r = await client.post(f"{base}/webhook/zalo", json=event(), headers=HDR)
            assert r.status_code == 200, r.text
            assert r.json()["reply"] == "pong from the model", r.json()

            assert len(model.requests) == 1, model.requests
            path, body = model.requests[0]
            assert path == "/v1/chat/completions", path
            assert body["model"] == "fake-model" and body["stream"] is False, body
            assert any("ping" in json.dumps(m) for m in body["messages"]), body["messages"]
            assert body.get("tools"), "the loop sent no tool schema to the model"
            # The caller's words are not the model's to invent: the reply is what came back.
            print("[PASS] Webhook -> ingress -> queue -> ReAct loop -> provider -> reply.")

            # Same message_id again: the ledger drops it, and the model is never asked twice.
            r = await client.post(f"{base}/webhook/zalo", json=event(), headers=HDR)
            assert r.status_code == 200 and r.json()["reply"] is None, r.json()
            assert len(model.requests) == 1, model.requests
            print("[PASS] A retried bridge message reached the model once.")

            # A second, different turn answers too, and the queue reports what it did.
            r = await client.post(f"{base}/webhook/zalo",
                                  json=event(message_id="e2e-2", text="again"), headers=HDR)
            assert r.json()["reply"] == "pong from the model", r.json()
            assert len(model.requests) == 2, model.requests
            state = await client.get(f"{base}/health")
            queue = state.json()["queue"]
            assert queue["delivered"] == 2 and queue["failed"] == 0, queue
            assert queue["queued"] == 0 and queue["running"] == 0, queue
            print("[PASS] A second turn answered; the queue reports 2 delivered, 0 failed.")

        # The session is real: the turns were persisted, not just answered.
        messages = env.state_db.get_session_messages("e2e-session")
        assert messages and any(m["role"] == "user" and "ping" in m["content"] for m in messages), \
            messages
    finally:
        daemon.stop()
        if daemon.dispatcher is not None:
            await daemon.dispatcher.stop()
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        await model.aclose()
        for closer in ("memory_store", "state_db", "delivery_ledger"):
            if hasattr(env, closer):
                getattr(env, closer).close()
        for name in ("BOOM_INGRESS_PORT",):
            os.environ.pop(name, None)
        shutil.rmtree(work, ignore_errors=True)

    print("ALL GATEWAY END-TO-END TESTS PASSED.")


if __name__ == "__main__":
    asyncio.run(main())
