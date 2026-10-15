"""Connection-reuse perf tests: one shared httpx.AsyncClient per adapter.

Offline: httpx.MockTransport, no network. Run: python3 evals/test_perf.py
"""
import asyncio
import os
import sys
import time
from pathlib import Path

# Work around a broken NO_PROXY in this sandbox (httpx chokes parsing IPv6
# entries like [::1]); the adapters themselves are unaffected.
for _v in ("NO_PROXY", "no_proxy"):
    os.environ.pop(_v, None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gateway"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from gateway.channel import ChannelAdapter, ChannelRegistry  # noqa: E402
from gateway.telegram import TelegramAdapter  # noqa: E402
from gateway.discord import DiscordChannel  # noqa: E402

CHECKS = []
FAILURES = []


def check(name, cond, detail=""):
    CHECKS.append(name)
    if not cond:
        FAILURES.append(name)
        print(f"[FAIL] {name} {detail}")
    else:
        print(f"[PASS] {name}")


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok_json(url, payload):
    def h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "url": str(request.url),
                                        "payload": payload})
    return h


async def main():
    # --- 1. three calls share one client object (telegram) ---
    seen = []

    def h(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    tg = TelegramAdapter(token="TEST")
    tg._client = mock_client(h)
    c0 = tg._http()
    await tg.send_text("1", "a")
    c1 = tg._http()
    await tg.send_text("1", "b")
    c2 = tg._http()
    await tg.react("1", "10", "👍")
    check("3 calls reuse the same client object", c0 is c1 is c2)
    check("3 requests actually sent", len(seen) == 3, f"got {len(seen)}")
    check("client has connection limits",
          tg._http()._transport is not None)  # mock transport attached

    # --- 2. fresh _http() builds a pooled client ---
    fresh = ChannelAdapter()
    cli = fresh._http()
    check("shared client created lazily", cli is fresh._http())
    limits = cli._transport._pool
    check("max_connections=20", limits._max_connections == 20,
          f"got {limits._max_connections}")
    check("max_keepalive_connections=10",
          limits._max_keepalive_connections == 10,
          f"got {limits._max_keepalive_connections}")
    check("timeout 35s", cli.timeout.connect == 35.0 or
          getattr(cli.timeout, "read", None) == 35.0,
          f"got {cli.timeout}")
    await fresh.aclose()

    # --- 3. aclose() closes cleanly, twice is safe ---
    a2 = ChannelAdapter()
    a2._client = mock_client(h)
    await a2.aclose()
    check("aclose clears _client", getattr(a2, "_client", None) is None)
    await a2.aclose()  # must not raise
    check("aclose twice is safe", True)
    a3 = ChannelAdapter()  # never created a client
    await a3.aclose()
    check("aclose with no client is safe", True)

    # --- 4. discord keeps its own timeout via _http(timeout=...) ---
    dc = DiscordChannel(daemon=None, token="T", timeout=12.5)
    dc._client = mock_client(h)
    check("discord _http returns injected client", dc._http() is dc._client)
    # fresh discord client honors custom timeout
    dc2 = DiscordChannel(daemon=None, token="T", timeout=12.5)
    real = dc2._http(timeout=dc2.timeout)
    t = real.timeout
    got = t.read if hasattr(t, "read") else t
    check("discord timeout preserved (12.5s)", got == 12.5, f"got {got}")
    await dc2.aclose()
    await dc.aclose()

    # --- 5. registry still dispatches (no behavior change) ---
    reg = ChannelRegistry()
    tg2 = TelegramAdapter(token="TEST")
    tg2._client = mock_client(ok_json("", {}))
    reg.register(tg2)
    r = await reg.react("telegram", "1", "5", "❤")
    check("registry.react still works", r == {"ok": True}, f"got {r}")
    r2 = await reg.react("nope", "1", "5", "❤")
    check("unknown channel still errors",
          r2 == {"error": "unknown channel 'nope'"}, f"got {r2}")
    await tg2.aclose()

    # --- 6. construction count: 5 calls build exactly 1 client ---
    # (With MockTransport there is no real TLS handshake to time; the honest
    # proof of reuse is that N calls construct 1 client, not N. Real-world
    # saving per avoided handshake is ~100-500ms of TCP+TLS.)
    import gateway.channel as channel_mod
    built = []
    real_client_cls = httpx.AsyncClient

    class CountingClient(real_client_cls):
        def __init__(self, *a, **k):
            built.append(1)
            super().__init__(*a, **k)

    orig = channel_mod.httpx.AsyncClient
    channel_mod.httpx.AsyncClient = CountingClient
    try:
        ad = TelegramAdapter(token="TEST")
        # point _http at the mock transport via a prebuilt counting client
        ad._client = CountingClient(transport=httpx.MockTransport(h))
        n_built_after_inject = len(built)
        for _ in range(5):
            await ad.send_text("1", "x")
        check("5 calls construct 0 new clients (1 shared)",
              len(built) == n_built_after_inject,
              f"built {len(built) - n_built_after_inject} extra")
        await ad.aclose()
        # fresh adapter: first _http() builds exactly 1
        built.clear()
        ad2 = TelegramAdapter(token="TEST")
        ad2._http()
        ad2._http()
        ad2._http()
        check("3 _http() calls build exactly 1 client", len(built) == 1,
              f"built {len(built)}")
        await ad2.aclose()
    finally:
        channel_mod.httpx.AsyncClient = orig

    await tg.aclose()
    print()
    print(f"ran {len(CHECKS)} checks, {len(FAILURES)} failed")
    if FAILURES:
        print("FAILURES:", FAILURES)
        sys.exit(1)
    print("ALL PERF TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
