"""Spotify connector for Boom.

Talks to the Spotify Web API with a Bearer access token taken ONLY from the
environment (`BOOM_SPOTIFY_ACCESS_TOKEN`). This module never performs an OAuth
flow and never prompts for secrets: drop a valid user token in the env and the
tools below work against the real API; without one, status() honestly reports
disconnected and every tool returns {"error": ...}.

Import root is agent/src, so this lives at agent/src/connectors/spotify.py
and is imported as `from connectors.spotify import SpotifyConnector`.

The token needs the user scopes the tools touch: `user-read-playback-state`
for now-playing/search, `user-modify-playback-state` for play/pause.
"""

from typing import Any, Dict, List

import httpx

from connectors.base import AsyncBearerConnector, _schema


_SEARCH_TYPES = ("track", "artist", "album")


def _brief_track(item: Dict[str, Any]) -> Dict[str, Any]:
    """The fields an agent needs from a track search hit."""
    artists = item.get("artists") or []
    album = item.get("album") or {}
    return {
        "type": "track",
        "name": item.get("name", ""),
        "uri": item.get("uri", ""),
        "artists": [a.get("name", "") for a in artists if isinstance(a, dict)],
        "album": album.get("name", "") if isinstance(album, dict) else "",
        "duration_ms": item.get("duration_ms", 0),
    }


def _brief_artist(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "artist",
        "name": item.get("name", ""),
        "uri": item.get("uri", ""),
        "genres": item.get("genres", []) or [],
    }


def _brief_album(item: Dict[str, Any]) -> Dict[str, Any]:
    artists = item.get("artists") or []
    return {
        "type": "album",
        "name": item.get("name", ""),
        "uri": item.get("uri", ""),
        "artists": [a.get("name", "") for a in artists if isinstance(a, dict)],
        "release_date": item.get("release_date", ""),
    }


_BRIEFERS = {"track": _brief_track, "artist": _brief_artist, "album": _brief_album}


def _api_error_message(resp: httpx.Response) -> str:
    """Best-effort human message out of a Spotify API error body."""
    try:
        body = resp.json()
    except ValueError:
        text = (resp.text or "").strip()
        return f"HTTP {resp.status_code}" + (f": {text[:200]}" if text else "")
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return f"HTTP {err.get('status', resp.status_code)}: {err.get('message', 'unknown error')}"
    return f"HTTP {resp.status_code}: unexpected response"


class SpotifyConnector(AsyncBearerConnector):
    """Spotify Web API over httpx. `transport` is injectable so tests never touch the network."""

    name = "spotify"

    TOKEN_ENV = "BOOM_SPOTIFY_ACCESS_TOKEN"
    API_BASE = "https://api.spotify.com/v1"
    SERVICE = "Spotify"

    # ------------------------------------------------------------------ contract

    def status(self) -> Dict[str, Any]:
        if self.token:
            return {"connected": True,
                    "detail": f"Bearer token present (env {self.TOKEN_ENV})"}
        return {"connected": False,
                "detail": f"missing env {self.TOKEN_ENV}: export a Spotify user OAuth access token "
                          f"(scopes user-read-playback-state, user-modify-playback-state)"}

    def build_tools(self) -> Dict[str, Dict[str, Any]]:
        return {
            "spotify_search": {
                "name": "spotify_search",
                "func": self._search,
                "description": (
                    "Search Spotify for tracks, artists or albums. Returns name/uri "
                    "plus artists (tracks/albums) or genres (artists) per hit."
                ),
                "schema": _schema(
                    {
                        "query": {"type": "string",
                                  "description": "Search query, e.g. 'da lab' or 'artist:son tung'."},
                        "type": {"type": "string",
                                 "description": "One of 'track', 'artist', 'album' (default 'track')."},
                        "limit": {"type": "integer",
                                  "description": "Max hits (1-50, default 10)."},
                    },
                    ["query"],
                ),
            },
            "spotify_now_playing": {
                "name": "spotify_now_playing",
                "func": self._now_playing,
                "description": (
                    "What is currently playing on the user's Spotify. Returns "
                    '{"playing": false} honestly when nothing is playing.'
                ),
                "schema": _schema(),
            },
            "spotify_play": {
                "name": "spotify_play",
                "func": self._play,
                "description": (
                    "Start playback of a Spotify track URI on the user's active device. "
                    "Needs an active Spotify device; otherwise the API reports an error."
                ),
                "schema": _schema(
                    {"uri": {"type": "string",
                             "description": "Track URI, e.g. 'spotify:track:4uLU6hMCjMI75M1A2tKUQ'."}},
                    ["uri"],
                ),
            },
            "spotify_pause": {
                "name": "spotify_pause",
                "func": self._pause,
                "description": "Pause the user's Spotify playback.",
                "schema": _schema(),
            },
        }


    async def _search(self, query: str, type: str = "track",
                      limit: int = 10) -> Dict[str, Any]:
        if not (query or "").strip():
            return {"error": "query is required"}
        kind = (type or "track").strip().lower()
        if kind not in _SEARCH_TYPES:
            return {"error": f"type must be one of {list(_SEARCH_TYPES)}, got {type!r}"}
        try:
            limit = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            return {"error": "limit must be an integer"}
        resp = await self._arequest(
            "GET", "/search",
            params={"q": query.strip(), "type": kind, "limit": limit},
        )
        if isinstance(resp, dict):
            return resp
        if resp.status_code != 200:
            return {"error": _api_error_message(resp)}
        try:
            items = (resp.json().get(kind + "s") or {}).get("items", [])
        except ValueError:
            return {"error": "could not parse the API response"}
        briefer = _BRIEFERS[kind]
        results: List[Dict[str, Any]] = [briefer(i) for i in items if isinstance(i, dict)]
        return {"type": kind, "query": query.strip(), "results": results,
                "total": len(results)}

    async def _now_playing(self) -> Dict[str, Any]:
        resp = await self._arequest("GET", "/me/player/currently-playing")
        if isinstance(resp, dict):
            return resp
        if resp.status_code == 204:
            # 204 has no body: nothing is playing right now.
            return {"playing": False}
        if resp.status_code != 200:
            return {"error": _api_error_message(resp)}
        try:
            data = resp.json()
        except ValueError:
            return {"error": "could not parse the API response"}
        item = data.get("item") or {}
        track = _brief_track(item) if item else None
        return {
            "playing": bool(data.get("is_playing", False)),
            "track": track,
            "progress_ms": data.get("progress_ms", 0),
        }

    async def _play(self, uri: str) -> Dict[str, Any]:
        uri = (uri or "").strip()
        if not uri:
            return {"error": "uri is required, e.g. 'spotify:track:...'"}
        resp = await self._arequest("PUT", "/me/player/play", json_body={"uris": [uri]})
        if isinstance(resp, dict):
            return resp
        if resp.status_code != 204:
            return {"error": _api_error_message(resp)}
        return {"ok": True, "uri": uri}

    async def _pause(self) -> Dict[str, Any]:
        resp = await self._arequest("PUT", "/me/player/pause")
        if isinstance(resp, dict):
            return resp
        if resp.status_code != 204:
            return {"error": _api_error_message(resp)}
        return {"ok": True}


# Module-level instance picked up by connectors.registry.load_default_registry().
CONNECTOR = SpotifyConnector()
