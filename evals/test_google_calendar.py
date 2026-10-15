"""Offline tests for the Google Calendar connector: no network, ever.

A fake httpx transport replays realistic Calendar API v3 fixtures. Covers: status()
honesty without a token, list parsing (timed + all-day events), request params and
the Authorization header, create body + response parsing, delete 204/404, API error
bodies surfaced as {"error"}, and validation short-circuits before any HTTP.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

import httpx

from connectors.google_calendar import GoogleCalendarConnector

LIST_FIXTURE = {
    "kind": "calendar#events",
    "summary": "primary",
    "items": [
        {
            "kind": "calendar#event",
            "id": "evt_abc123",
            "status": "confirmed",
            "summary": "Họp team",
            "start": {"dateTime": "2026-10-16T09:00:00+07:00", "timeZone": "Asia/Ho_Chi_Minh"},
            "end": {"dateTime": "2026-10-16T10:00:00+07:00", "timeZone": "Asia/Ho_Chi_Minh"},
        },
        {
            "kind": "calendar#event",
            "id": "evt_day456",
            "status": "confirmed",
            "summary": "Nghỉ lễ",
            "start": {"date": "2026-10-20"},
            "end": {"date": "2026-10-21"},
        },
    ],
}

ERR_401 = {
    "error": {
        "code": 401,
        "message": "Request had invalid authentication credentials.",
        "errors": [{"message": "Invalid Credentials", "domain": "global", "reason": "authError"}],
        "status": "UNAUTHENTICATED",
    }
}

ERR_404 = {
    "error": {
        "code": 404,
        "message": "Not Found",
        "errors": [{"message": "Not Found", "domain": "global", "reason": "notFound"}],
    }
}

CREATE_FIXTURE = {
    "kind": "calendar#event",
    "id": "evt_new789",
    "status": "confirmed",
    "htmlLink": "https://www.google.com/calendar/event?eid=ZXZ0X25ldzc4OQ",
    "summary": "Họp 1-1",
    "start": {"dateTime": "2026-10-16T14:00:00", "timeZone": "Asia/Ho_Chi_Minh"},
    "end": {"dateTime": "2026-10-16T14:30:00", "timeZone": "Asia/Ho_Chi_Minh"},
}


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(f"FAILED: {name}")


def test_status_without_token():
    import os
    os.environ.pop("BOOM_GOOGLE_CALENDAR_ACCESS_TOKEN", None)
    c = GoogleCalendarConnector()
    st = c.status()
    check("status disconnected without token",
          st["connected"] is False and "BOOM_GOOGLE_CALENDAR_ACCESS_TOKEN" in st["detail"])


def test_tool_errors_without_token_and_no_http():
    import os
    os.environ.pop("BOOM_GOOGLE_CALENDAR_ACCESS_TOKEN", None)

    def boom_handler(request):
        raise AssertionError("no HTTP should happen without a token")

    c = GoogleCalendarConnector(transport=httpx.MockTransport(boom_handler))
    tools = c.build_tools()
    check("three tools", set(tools) == {"calendar_list", "calendar_create", "calendar_delete"})
    r = asyncio.run(tools["calendar_list"]["func"]("2026-10-15T00:00:00+07:00",
                                                    "2026-10-16T00:00:00+07:00"))
    check("list errors without token", "error" in r)
    r = asyncio.run(tools["calendar_delete"]["func"]("evt_x"))
    check("delete errors without token", "error" in r)


def test_list_parses_and_sends_params():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=LIST_FIXTURE)

    c = GoogleCalendarConnector(token="tok123", transport=httpx.MockTransport(handler))
    r = asyncio.run(c.build_tools()["calendar_list"]["func"](
        "2026-10-15T00:00:00+07:00", "2026-10-21T00:00:00+07:00", max_results=5))
    check("list ok", r.get("total") == 2 and len(r["events"]) == 2)
    e1, e2 = r["events"]
    check("timed event parsed",
          e1["id"] == "evt_abc123" and e1["summary"] == "Họp team"
          and e1["start"] == "2026-10-16T09:00:00+07:00"
          and e1["end"] == "2026-10-16T10:00:00+07:00")
    check("all-day event parsed",
          e2["id"] == "evt_day456" and e2["start"] == "2026-10-20" and e2["end"] == "2026-10-21")
    check("bearer header", seen["auth"] == "Bearer tok123")
    check("query params",
          seen["params"].get("timeMin") == "2026-10-15T00:00:00+07:00"
          and seen["params"].get("timeMax") == "2026-10-21T00:00:00+07:00"
          and seen["params"].get("singleEvents") == "true"
          and seen["params"].get("orderBy") == "startTime"
          and "/calendars/primary/events" in seen["url"])


def test_list_api_error_surfaced():
    def handler(request):
        return httpx.Response(401, json=ERR_401)

    c = GoogleCalendarConnector(token="bad", transport=httpx.MockTransport(handler))
    r = asyncio.run(c.build_tools()["calendar_list"]["func"]("a", "b"))
    check("401 becomes error, not raise",
          "error" in r and "invalid authentication credentials" in r["error"].lower())


def test_create_sends_body_and_parses():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=CREATE_FIXTURE)

    c = GoogleCalendarConnector(token="tok123", transport=httpx.MockTransport(handler))
    r = asyncio.run(c.build_tools()["calendar_create"]["func"](
        "Họp 1-1", "2026-10-16T14:00:00", "2026-10-16T14:30:00",
        description="trao đổi", timezone="Asia/Ho_Chi_Minh"))
    check("create ok",
          r.get("created") is True and r["id"] == "evt_new789" and r["summary"] == "Họp 1-1")
    b = seen["body"]
    check("create body",
          seen["method"] == "POST"
          and b["summary"] == "Họp 1-1" and b["description"] == "trao đổi"
          and b["start"] == {"dateTime": "2026-10-16T14:00:00", "timeZone": "Asia/Ho_Chi_Minh"}
          and b["end"] == {"dateTime": "2026-10-16T14:30:00", "timeZone": "Asia/Ho_Chi_Minh"})


def test_create_validation_before_http():
    def boom_handler(request):
        raise AssertionError("validation must short-circuit before HTTP")

    c = GoogleCalendarConnector(token="tok", transport=httpx.MockTransport(boom_handler))
    r = asyncio.run(c.build_tools()["calendar_create"]["func"]("", "s", "e"))
    check("empty summary rejected", "error" in r)
    r = asyncio.run(c.build_tools()["calendar_create"]["func"]("x", "", ""))
    check("empty start/end rejected", "error" in r)


def test_delete_ok_and_not_found():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        if request.url.path.endswith("evt_gone"):
            return httpx.Response(404, json=ERR_404)
        return httpx.Response(204)

    c = GoogleCalendarConnector(token="tok", transport=httpx.MockTransport(handler))
    r = asyncio.run(c.build_tools()["calendar_delete"]["func"]("evt_abc123"))
    check("delete 204", r == {"deleted": "evt_abc123"})
    check("delete url", seen["method"] == "DELETE"
          and seen["url"].endswith("/calendars/primary/events/evt_abc123"))
    r = asyncio.run(c.build_tools()["calendar_delete"]["func"]("evt_gone"))
    check("delete 404 becomes error", "error" in r and "404" in r["error"])


def test_network_error_becomes_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    c = GoogleCalendarConnector(token="tok", transport=httpx.MockTransport(handler))
    r = asyncio.run(c.build_tools()["calendar_list"]["func"]("a", "b"))
    check("network error not raised", "error" in r and "ConnectError" in r["error"])


def test_schemas():
    tools = GoogleCalendarConnector(token="x").build_tools()
    check("list schema requires time_min/time_max",
          set(tools["calendar_list"]["schema"]["required"]) == {"time_min", "time_max"})
    check("create schema requires summary/start/end",
          set(tools["calendar_create"]["schema"]["required"]) == {"summary", "start", "end"})
    check("delete schema requires event_id",
          tools["calendar_delete"]["schema"]["required"] == ["event_id"])
    check("names carry connector prefix",
          all(n.startswith("calendar_") for n in tools))


if __name__ == "__main__":
    print("== google calendar connector ==")
    test_status_without_token()
    test_tool_errors_without_token_and_no_http()
    test_list_parses_and_sends_params()
    test_list_api_error_surfaced()
    test_create_sends_body_and_parses()
    test_create_validation_before_http()
    test_delete_ok_and_not_found()
    test_network_error_becomes_error()
    test_schemas()
    print("ALL GOOGLE CALENDAR TESTS PASSED")
