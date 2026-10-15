"""Offline tests for the Spotify connector.

No network: all HTTP goes through httpx.MockTransport with fixtures shaped like
the real Spotify Web API. Run: python3 evals/test_spotify.py
"""

import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# repo layout: evals/test_spotify.py, agent/src is the import root
sys.path.insert(0, str(HERE.parent / "agent" / "src"))

import httpx

from connectors.spotify import CONNECTOR, SpotifyConnector

PASS = 0


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        raise SystemExit(f"FAIL {name} {extra}")


def run(coro):
    return asyncio.run(coro)


def boom_if_called(request):
    raise AssertionError(f"no HTTP expected, got {request.method} {request.url}")


TRACK_FIXTURE = {
    "tracks": {
        "items": [
            {
                "name": "Nguoi Hay Quen Em Di",
                "uri": "spotify:track:4uLU6hMCjMI75M1A2tKUQ",
                "duration_ms": 245000,
                "artists": [{"name": "Son Tung M-TP"}],
                "album": {"name": "Single"},
            }
        ]
    }
}

ARTIST_FIXTURE = {
    "artists": {
        "items": [
            {"name": "Da LAB", "uri": "spotify:artist:abc123",
             "genres": ["vietnamese pop", "rap"]}
        ]
    }
}

NOW_PLAYING_FIXTURE = {
    "is_playing": True,
    "progress_ms": 61000,
    "item": {
        "name": "Chung Ta Cua Hien Tai",
        "uri": "spotify:track:xyz789",
        "duration_ms": 300000,
        "artists": [{"name": "Son Tung M-TP"}],
        "album": {"name": "Single"},
    },
}


def test_status_and_guard():
    c = SpotifyConnector(token="")
    st = c.status()
    check("disconnected without token",
          st == {"connected": False, "detail": st["detail"]} and not st["connected"])
    check("detail mentions env", "BOOM_SPOTIFY_ACCESS_TOKEN" in st["detail"])
    c2 = SpotifyConnector(token="tok")
    check("connected with token", c2.status()["connected"] is True)

    seen = []

    def guard_transport(request):
        seen.append(request)
        return httpx.Response(200, json={})

    c3 = SpotifyConnector(token="", transport=httpx.MockTransport(guard_transport))
    tools = c3.build_tools()
    for name in ("spotify_search", "spotify_now_playing", "spotify_play", "spotify_pause"):
        kwargs = {"query": "x"} if name == "spotify_search" else (
            {"uri": "spotify:track:x"} if name == "spotify_play" else {})
        res = run(tools[name]["func"](**kwargs))
        check(f"{name} errors without token", res.get("error", "").startswith("not connected"))
    check("no HTTP without token", seen == [])


def test_search_track():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        check("search path", request.url.path == "/v1/search")
        return httpx.Response(200, json=TRACK_FIXTURE)

    c = SpotifyConnector(token="tok", transport=httpx.MockTransport(handler))
    res = run(c.build_tools()["spotify_search"]["func"]("da lab", limit=5))
    check("search method", captured["method"] == "GET")
    check("search q param", "q=da+lab" in captured["url"] or "q=da%20lab" in captured["url"],
          captured["url"])
    check("search type param", "type=track" in captured["url"], captured["url"])
    check("search limit param", "limit=5" in captured["url"], captured["url"])
    check("bearer header", captured["auth"] == "Bearer tok")
    check("search total", res["total"] == 1 and res["type"] == "track")
    hit = res["results"][0]
    check("track name", hit["name"] == "Nguoi Hay Quen Em Di")
    check("track uri", hit["uri"] == "spotify:track:4uLU6hMCjMI75M1A2tKUQ")
    check("track artists", hit["artists"] == ["Son Tung M-TP"])
    check("track album", hit["album"] == "Single")


def test_search_artist_and_validation():
    def handler(request):
        return httpx.Response(200, json=ARTIST_FIXTURE)

    c = SpotifyConnector(token="tok", transport=httpx.MockTransport(handler))
    fn = c.build_tools()["spotify_search"]["func"]
    res = run(fn("da lab", type="artist"))
    hit = res["results"][0]
    check("artist parsed", hit["type"] == "artist" and hit["name"] == "Da LAB"
          and hit["genres"] == ["vietnamese pop", "rap"])

    seen = []

    def no_call(request):
        seen.append(request)
        return httpx.Response(200, json={})

    c2 = SpotifyConnector(token="tok", transport=httpx.MockTransport(no_call))
    fn2 = c2.build_tools()["spotify_search"]["func"]
    check("bad type rejected", "error" in run(fn2("x", type="podcast")))
    check("empty query rejected", "error" in run(fn2("   ")))
    check("bad limit rejected", "error" in run(fn2("x", limit="many")))
    check("no HTTP on validation errors", seen == [])


def test_now_playing():
    def playing(request):
        check("now playing path", request.url.path == "/v1/me/player/currently-playing")
        return httpx.Response(200, json=NOW_PLAYING_FIXTURE)

    c = SpotifyConnector(token="tok", transport=httpx.MockTransport(playing))
    res = run(c.build_tools()["spotify_now_playing"]["func"]())
    check("playing true", res["playing"] is True)
    check("track parsed", res["track"]["name"] == "Chung Ta Cua Hien Tai"
          and res["track"]["artists"] == ["Son Tung M-TP"])
    check("progress", res["progress_ms"] == 61000)

    def idle(request):
        return httpx.Response(204)

    c2 = SpotifyConnector(token="tok", transport=httpx.MockTransport(idle))
    res2 = run(c2.build_tools()["spotify_now_playing"]["func"]())
    check("204 -> playing false", res2 == {"playing": False})


def test_play_pause():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode()) if request.content else None
        return httpx.Response(204)

    c = SpotifyConnector(token="tok", transport=httpx.MockTransport(handler))
    tools = c.build_tools()
    res = run(tools["spotify_play"]["func"]("spotify:track:4uLU6hMCjMI75M1A2tKUQ"))
    check("play PUT", captured["method"] == "PUT")
    check("play path", captured["path"] == "/v1/me/player/play")
    check("play body", captured["body"] == {"uris": ["spotify:track:4uLU6hMCjMI75M1A2tKUQ"]},
          captured["body"])
    check("play ok", res == {"ok": True, "uri": "spotify:track:4uLU6hMCjMI75M1A2tKUQ"})

    res2 = run(tools["spotify_pause"]["func"]())
    check("pause path", captured["path"] == "/v1/me/player/pause")
    check("pause ok", res2 == {"ok": True})

    check("empty uri rejected", "error" in run(tools["spotify_play"]["func"]("  ")))


def test_api_and_network_errors():
    def unauthorized(request):
        return httpx.Response(401, json={
            "error": {"status": 401, "message": "Invalid access token"}})

    c = SpotifyConnector(token="bad", transport=httpx.MockTransport(unauthorized))
    res = run(c.build_tools()["spotify_search"]["func"]("x"))
    check("401 becomes error", res.get("error") == "HTTP 401: Invalid access token", res)

    def conn_fail(request):
        raise httpx.ConnectError("dns down")

    c2 = SpotifyConnector(token="tok", transport=httpx.MockTransport(conn_fail))
    tools = c2.build_tools()
    check("network error not raised (search)",
          "error" in run(tools["spotify_search"]["func"]("x")))
    check("network error not raised (pause)",
          "error" in run(tools["spotify_pause"]["func"]()))


def test_tool_shapes_and_connector_instance():
    check("module CONNECTOR name", CONNECTOR.name == "spotify")
    check("CONNECTOR is SpotifyConnector", isinstance(CONNECTOR, SpotifyConnector))
    tools = CONNECTOR.build_tools()
    check("four tools", set(tools) == {"spotify_search", "spotify_now_playing",
                                       "spotify_play", "spotify_pause"})
    for name, t in tools.items():
        check(f"{name} prefix", name.startswith("spotify_"))
        check(f"{name} dict shape",
              t["name"] == name and callable(t["func"])
              and t["description"] and t["schema"]["type"] == "object")


if __name__ == "__main__":
    print("== spotify connector ==")
    test_status_and_guard()
    test_search_track()
    test_search_artist_and_validation()
    test_now_playing()
    test_play_pause()
    test_api_and_network_errors()
    test_tool_shapes_and_connector_instance()
    print(f"ALL SPOTIFY TESTS PASSED ({PASS} checks)")
