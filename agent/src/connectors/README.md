# Service connectors

Boom talks to external services through connectors in `agent/src/connectors/`.
Each connector reads its credential from the environment — no OAuth flow in code,
no prompts, no hardcoded secrets. Without a credential the connector honestly
reports `connected: false` and its tools return `{"error": ...}`.

## Available connectors

| Connector | Env var | Tools |
|-----------|---------|-------|
| Gmail | `BOOM_GMAIL_ACCESS_TOKEN` | `gmail_search`, `gmail_read`, `gmail_send` |
| Google Calendar | `BOOM_GOOGLE_CALENDAR_ACCESS_TOKEN` | `calendar_list`, `calendar_create`, `calendar_delete` |
| Google Drive | `BOOM_GOOGLE_DRIVE_ACCESS_TOKEN` | `drive_list`, `drive_search`, `drive_download`, `drive_upload` |
| Generic HTTP API | `BOOM_HTTP_API_DEFS` (path, default `memories/http_apis.json`) | `http_<api>_<endpoint>` per definition |

## Getting a token

Use a Google OAuth 2.0 access token with the scopes you need
(`gmail.modify` for Gmail send, `gmail.readonly` to read only,
`https://www.googleapis.com/auth/calendar`, `.../auth/drive`).
The fastest way for personal use:

- Google OAuth 2.0 Playground (https://developers.google.com/oauthplayground/),
  or
- `gcloud auth print-access-token` (after `gcloud auth login`).

Then:

```bash
export BOOM_GMAIL_ACCESS_TOKEN="ya29...."
export BOOM_GOOGLE_CALENDAR_ACCESS_TOKEN="ya29...."
export BOOM_GOOGLE_DRIVE_ACCESS_TOKEN="ya29...."
```

## Custom HTTP APIs

Copy `http_apis.example.json` to `memories/http_apis.json` (or point
`BOOM_HTTP_API_DEFS` at your file) and define your endpoints:

```json
{
  "apis": [
    {
      "name": "crm",
      "base_url": "https://crm.example.com/api",
      "headers": {"Authorization": "Bearer <token>"},
      "endpoints": [
        {"name": "get_contact", "method": "GET", "path": "/contacts/{id}"}
      ]
    }
  ]
}
```

This creates a tool `http_crm_get_contact(id=...)`. `{param}` in the path is
URL-encoded from arguments; remaining arguments become the query string
(GET/DELETE) or JSON body (POST/PUT/PATCH).

## Adding a connector

1. Subclass `connectors.base.Connector` (`name`, `status()`, `build_tools()`),
   prefix tool names with the connector name.
2. Expose a module-level `CONNECTOR = MyConnector()` instance.
3. `connectors.registry.load_default_registry()` picks it up automatically —
   no wiring changes needed.
