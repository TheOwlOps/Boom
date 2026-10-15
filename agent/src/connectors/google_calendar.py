"""Google Calendar connector for Boom.

Talks to the Google Calendar REST API v3 with a Bearer access token taken ONLY from
the environment (`BOOM_GOOGLE_CALENDAR_ACCESS_TOKEN`). This module never performs an
OAuth flow and never prompts for secrets: drop a valid token in the env and the
tools below work against the real API; without one, status() honestly reports
disconnected and every tool returns {"error": ...}.

Import root is agent/src, so this lives at agent/src/connectors/google_calendar.py
and is imported as `from connectors.google_calendar import GoogleCalendarConnector`.
"""

from typing import Any, Dict, List, Optional

import httpx

from connectors.base import AsyncBearerConnector, _schema

DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"


def _event_brief(item: Dict[str, Any]) -> Dict[str, Any]:
    """The fields an agent needs, tolerant of all-day events (date instead of dateTime)."""
    start = item.get("start") or {}
    end = item.get("end") or {}
    return {
        "id": item.get("id", ""),
        "summary": item.get("summary", "(no title)"),
        "start": start.get("dateTime") or start.get("date") or "",
        "end": end.get("dateTime") or end.get("date") or "",
    }


def _api_error_message(resp: httpx.Response) -> str:
    """Best-effort human message out of a Google API error body."""
    try:
        body = resp.json()
    except ValueError:
        text = (resp.text or "").strip()
        return f"HTTP {resp.status_code}" + (f": {text[:200]}" if text else "")
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or ""
        details = err.get("errors") or []
        first = details[0].get("message") if details and isinstance(details[0], dict) else ""
        detail = first if first and first != msg else ""
        return f"HTTP {err.get('code', resp.status_code)}: {msg}" + (f" ({detail})" if detail else "")
    return f"HTTP {resp.status_code}: unexpected response"


class GoogleCalendarConnector(AsyncBearerConnector):
    """Calendar API v3 over httpx. `transport` is injectable so tests never touch the network."""

    name = "google_calendar"

    TOKEN_ENV = "BOOM_GOOGLE_CALENDAR_ACCESS_TOKEN"
    API_BASE = "https://www.googleapis.com/calendar/v3"
    SERVICE = "Calendar"

    # ------------------------------------------------------------------ contract

    def status(self) -> Dict[str, Any]:
        if self.token:
            return {"connected": True,
                    "detail": f"Bearer token present (env {self.TOKEN_ENV})"}
        return {"connected": False,
                "detail": f"missing env {self.TOKEN_ENV}"}

    def build_tools(self) -> Dict[str, Dict[str, Any]]:
        return {
            "calendar_list": {
                "name": "calendar_list",
                "func": self._list,
                "description": (
                    "List events on the primary calendar between two ISO 8601 timestamps "
                    "(e.g. '2026-10-15T00:00:00+07:00'). Returns id/summary/start/end per event."
                ),
                "schema": _schema(
                    {
                        "time_min": {"type": "string",
                                     "description": "ISO 8601 lower bound (inclusive)."},
                        "time_max": {"type": "string",
                                     "description": "ISO 8601 upper bound (exclusive)."},
                        "max_results": {"type": "integer",
                                        "description": "Max events to return (default 20)."},
                    },
                    ["time_min", "time_max"],
                ),
            },
            "calendar_create": {
                "name": "calendar_create",
                "func": self._create,
                "description": (
                    "Create an event on the primary calendar. start/end are ISO 8601 "
                    "datetimes; timezone defaults to Asia/Ho_Chi_Minh."
                ),
                "schema": _schema(
                    {
                        "summary": {"type": "string", "description": "Event title."},
                        "start": {"type": "string",
                                  "description": "ISO 8601 start, e.g. '2026-10-16T09:00:00'."},
                        "end": {"type": "string",
                                "description": "ISO 8601 end, e.g. '2026-10-16T10:00:00'."},
                        "description": {"type": "string",
                                        "description": "Optional event description."},
                        "timezone": {"type": "string",
                                     "description": "IANA timezone (default Asia/Ho_Chi_Minh)."},
                    },
                    ["summary", "start", "end"],
                ),
            },
            "calendar_delete": {
                "name": "calendar_delete",
                "func": self._delete,
                "description": "Delete an event from the primary calendar by its id.",
                "schema": _schema(
                    {"event_id": {"type": "string", "description": "Event id from calendar_list."}},
                    ["event_id"],
                ),
            },
        }


    async def _list(self, time_min: str, time_max: str, max_results: int = 20) -> Dict[str, Any]:
        if not (time_min or "").strip() or not (time_max or "").strip():
            return {"error": "time_min and time_max are required"}
        try:
            max_results = max(1, min(int(max_results), 250))
        except (TypeError, ValueError):
            return {"error": "max_results must be an integer"}
        resp = await self._arequest("GET", "/calendars/primary/events", params={
                        "timeMin": time_min,
                        "timeMax": time_max,
                        "maxResults": max_results,
                        "singleEvents": "true",
                        "orderBy": "startTime",
                    })
        if isinstance(resp, dict):
            return resp
        if resp.status_code != 200:
            return {"error": _api_error_message(resp)}
        try:
            items = resp.json().get("items", [])
        except ValueError:
            return {"error": "could not parse the API response"}
        events: List[Dict[str, Any]] = [_event_brief(i) for i in items if isinstance(i, dict)]
        return {"events": events, "total": len(events)}

    async def _create(self, summary: str, start: str, end: str,
                      description: str = "", timezone: str = DEFAULT_TIMEZONE) -> Dict[str, Any]:
        if not (summary or "").strip():
            return {"error": "summary is required"}
        if not (start or "").strip() or not (end or "").strip():
            return {"error": "start and end are required (ISO 8601)"}
        body = {
            "summary": summary.strip(),
            "description": description or "",
            "start": {"dateTime": start, "timeZone": timezone or DEFAULT_TIMEZONE},
            "end": {"dateTime": end, "timeZone": timezone or DEFAULT_TIMEZONE},
        }
        resp = await self._arequest("POST", "/calendars/primary/events", json_body=body)
        if isinstance(resp, dict):
            return resp
        if resp.status_code not in (200, 201):
            return {"error": _api_error_message(resp)}
        try:
            created = _event_brief(resp.json())
        except ValueError:
            return {"error": "event created but the API response could not be parsed"}
        created["created"] = True
        return created

    async def _delete(self, event_id: str) -> Dict[str, Any]:
        if not (event_id or "").strip():
            return {"error": "event_id is required"}
        resp = await self._arequest("DELETE", f"/calendars/primary/events/{event_id.strip()}")
        if isinstance(resp, dict):
            return resp
        if resp.status_code == 204:
            return {"deleted": event_id.strip()}
        return {"error": _api_error_message(resp)}


# Module-level instance picked up by connectors.registry.load_default_registry().
CONNECTOR = GoogleCalendarConnector()
