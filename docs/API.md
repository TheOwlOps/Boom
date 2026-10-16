# API Reference — Boom Gateway

> This document describes the HTTP API of the **Boom gateway** (module `gateway/api.py`, version `0.1.0`).
> The server runs by default at `http://127.0.0.1:18080` (change via the `BOOM_API_PORT` env var).
>
> **Security note (v1):** API v1 has **no auth**. The server only binds `127.0.0.1` (localhost-only),
> never exposed to the external network. CORS responds only for 2 dev origins:
> `http://localhost:5173` (Vite) and `http://localhost:1420` (Tauri) — never `*`.
> Request body limit: **1 MB** (exceeding it → `400`).

## Endpoint overview

| # | Method | Path | Description |
|---|--------|------|-------------|
| 1 | GET | `/api/health` | Server status, version, model, uptime, tool/skill counts |
| 2 | GET | `/api/logs` | In-memory log buffer (newest first) |
| 3 | GET | `/api/sessions` | Chat session listing (currently always empty — no listing API yet) |
| 4 | GET | `/api/channels/status` | Connection status of the 8 channels (Telegram, Discord, Slack, WhatsApp, Line, Feishu, Zalo, Messenger) |
| 5 | GET | `/api/dag/status` | Node status of the most recent DAG run (currently always `[]`) |
| 6 | GET | `/api/tools` | List all real tools from the environment |
| 7 | GET | `/api/skills` | Scan the repo's real `skills/` directory |
| 8 | POST | `/api/chat` | Send one chat turn to the Brain (supports SSE streaming) |
| 9 | POST | `/api/tools/run` | Run one tool (via sandbox when available) |

The server also handles `OPTIONS` on every path (CORS preflight → `204`).

## Base URL

```text
http://127.0.0.1:18080
```

---

## 1. GET /api/health — Server status

Returns basic status. `tools_count`/`skills_count` are counted **for real** from
the environment and the repo's `skills/` directory.

**Response 200** — `application/json`:

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Always `"ok"` |
| `version` | string | API version, e.g. `"0.1.0"` |
| `model` | string | LLM model name, or `"unconfigured"` when the backend isn't configured |
| `uptime_sec` | integer | Seconds since server start |
| `tools_count` | integer | Real tool count loaded from `agent/src/environment.py` |
| `skills_count` | integer | Number of subdirectories in `skills/` |

**Example:**

```bash
curl http://127.0.0.1:18080/api/health
```

```json
{
  "status": "ok",
  "version": "0.1.0",
  "model": "gpt-4o",
  "uptime_sec": 3600,
  "tools_count": 139,
  "skills_count": 5
}
```

---

## 2. GET /api/logs — Log buffer

Reads the in-memory log buffer (up to the newest 200 entries). Each entry has `ts` (ISO-8601)
and `msg` (truncated at 500 characters).

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | integer | 50 | Number of log lines, clamped to 1–200 |

**Response 200:**

| Field | Type | Description |
|-------|------|-------------|
| `logs` | array | Array of entries, each `{"ts": string, "msg": string}` |

**Example:**

```bash
curl "http://127.0.0.1:18080/api/logs?limit=10"
```

```json
{
  "logs": [
    {"ts": "2026-10-16T02:10:00", "msg": "tool_run: web_search ok"},
    {"ts": "2026-10-16T02:09:41", "msg": "tool_run: web_search error: timeout"}
  ]
}
```

---

## 3. GET /api/sessions — Session listing

> **Note:** `ReActLoop` only has `load_session()`, no listing API, so this endpoint
> honestly returns `[]` — it doesn't invent sessions.

**Response 200:**

```json
{ "sessions": [] }
```

**Example:**

```bash
curl http://127.0.0.1:18080/api/sessions
```

---

## 4. GET /api/channels/status — Channel status

Checks send capability of the **8 fixed channels** (contract): `telegram`, `discord`,
`slack`, `whatsapp`, `line`, `feishu`, `zalo`, `messenger`.
The `connected` state is asked directly from the daemon (`daemon.can_deliver(name)`).

**Response 200:**

| Field | Type | Description |
|-------|------|-------------|
| `channels` | array | Each item: `{"name": string, "connected": boolean, "detail": string \| null}` |

`detail` is a hint about the missing credential (e.g. `"TELEGRAM_BOT_TOKEN not set"`),
`"gateway daemon not wired"` when the daemon isn't connected, or `null` when connected.

**Example:**

```bash
curl http://127.0.0.1:18080/api/channels/status
```

```json
{
  "channels": [
    {"name": "telegram", "connected": true, "detail": null},
    {"name": "discord", "connected": false, "detail": "discord token not given to the gateway daemon"},
    {"name": "slack", "connected": false, "detail": "BOOM_SLACK_BOT_TOKEN not set"},
    {"name": "whatsapp", "connected": false, "detail": "BOOM_WHATSAPP_TOKEN not set"},
    {"name": "line", "connected": false, "detail": "BOOM_LINE_CHANNEL_TOKEN not set"},
    {"name": "feishu", "connected": false, "detail": "BOOM_FEISHU_APP_ID / BOOM_FEISHU_APP_SECRET not set"},
    {"name": "zalo", "connected": false, "detail": "BOOM_ZALO_OA_ACCESS_TOKEN not set"},
    {"name": "messenger", "connected": false, "detail": "BOOM_MESSENGER_PAGE_TOKEN not set"}
  ]
}
```

---

## 5. GET /api/dag/status — DAG status

> **Note:** the Brain doesn't currently persist the last run's state, so this endpoint returns
> `{"nodes": []}`. If a future Brain exposes `last_result` (nodes + node statuses),
> the server will return the true contract shape instead of inventing data.

**Response 200:**

| Field | Type | Description |
|-------|------|-------------|
| `nodes` | array | Each item: `{"id": string, "name": string, "status": string, "dependencies": string[]}` |

`status` can be: `completed`, `idle`, or whatever the Brain reports.

**Example:**

```bash
curl http://127.0.0.1:18080/api/dag/status
```

```json
{ "nodes": [] }
```

---

## 6. GET /api/tools — List tools

Lists the **real tools** built from `agent/src/environment.py` (each with `name`,
`description`, `group`).

**Response 200:**

| Field | Type | Description |
|-------|------|-------------|
| `tools` | array | Each item: `{"name": string, "description": string, "group": string}` |
| `count` | integer | Number of tools |

**Example:**

```bash
curl http://127.0.0.1:18080/api/tools
```

```json
{
  "tools": [
    {"name": "web_search", "description": "Search the web", "group": "web"},
    {"name": "gmail_search", "description": "Search Gmail", "group": "gmail"}
  ],
  "count": 139
}
```

---

## 7. GET /api/skills — List skills

Scans the repo's **real** `skills/` directory (sorted by directory name). `description`
comes from the first text line of `SKILL.md` (truncated at 200 characters), empty if the file is missing.

**Response 200:**

| Field | Type | Description |
|-------|------|-------------|
| `skills` | array | Each item: `{"name": string, "description": string}` |
| `count` | integer | Number of skills |

**Example:**

```bash
curl http://127.0.0.1:18080/api/skills
```

```json
{
  "skills": [
    {"name": "rea", "description": "Reverse-engineer-anything skill bundle"}
  ],
  "count": 5
}
```

---

## 8. POST /api/chat — Send a chat turn

Runs **one chat turn** through the Brain. Turns are serialized with a lock:
if another turn is running, the request returns `429 "agent busy"`.

**Request body** — `application/json`:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `prompt` | string | **required** | The question/command content |
| `strategy` | string | optional | One of: `auto`, `react`, `dag`, `mass`. Default: `"auto"` |
| `session_id` | string | optional | Session ID. If empty/missing, the server generates `"session_<unix_ts>"` |

**Response 200 (plain JSON)** — `application/json`:

| Field | Type | Description |
|-------|------|-------------|
| `reply` | string | The agent's answer |
| `session_id` | string | The session ID used (pass back for the next turn) |
| `strategy` | string | The strategy actually used |
| `errors` | object | Per-node secondary errors (usually `{}`) |

**Example:**

```bash
curl -X POST http://127.0.0.1:18080/api/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Find the latest AI news", "strategy": "react", "session_id": "demo-1"}'
```

```json
{
  "reply": "Here are the search results...",
  "session_id": "demo-1",
  "strategy": "react",
  "errors": {}
}
```

### 8b. POST /api/chat — SSE streaming

If the request has the header `Accept: text/event-stream`, the server responds with
`Content-Type: text/event-stream` and these SSE events:

| Event | Payload | Description |
|-------|---------|-------------|
| `status` | `{"phase": "thinking"}` | Sent immediately when processing starts |
| `message` | `{"text": "..."}` | The full reply (v1 doesn't stream real tokens — the client animates it) |
| `done` | `{"session_id": "...", "strategy": "..."}` | End of stream |
| `error` | `{"error": "chat turn failed"}` | When the turn fails |

**Example:**

```bash
curl -N -X POST http://127.0.0.1:18080/api/chat \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"prompt": "Hello"}'
```

```
event: status
data: {"phase": "thinking"}

event: message
data: {"text": "Hello there!"}

event: done
data: {"session_id": "session_1729041000", "strategy": "react"}
```

---

## 9. POST /api/tools/run — Run one tool

Runs one tool from the environment. If `agent/src/security/toolsandbox.py`
exists, it runs via `SandboxedToolRunner` (recorded as `user="api"`);
otherwise `func(**args)` is called directly.

**Request body** — `application/json`:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `tool` | string | **required** | Tool name (must exist in the environment) |
| `args` | object | optional | Arguments passed to the tool. Default: `{}` |

**Response 200** — always HTTP 200, distinguished by `ok`:

| Field | Type | Description |
|-------|------|-------------|
| `ok` | boolean | `true` when the run finished without an exception |
| `result` | string | The result (stringified, truncated at 2000 characters) — only when `ok: true` |
| `error` | string | Error message (truncated at 500 characters) — only when `ok: false` |

**Example:**

```bash
curl -X POST http://127.0.0.1:18080/api/tools/run \
  -H "Content-Type: application/json" \
  -d '{"tool": "web_search", "args": {"query": "Boom AI"}}'
```

```json
{
  "ok": true,
  "result": "[{...search results...}]"
}
```

Runtime error (HTTP 200, `ok: false`):

```json
{
  "ok": false,
  "error": "connection timeout"
}
```

---

## Common error codes

All errors return JSON of shape `{"error": "<message>"}`, **never** HTML:

| HTTP | When | Example message |
|------|------|-----------------|
| 400 | Invalid request body | `"invalid Content-Length"`, `"request body too large"` (over 1MB), `"request body is not valid JSON"`, `"request body must be a JSON object"`, `"prompt is required"`, `"strategy must be one of: auto, react, dag, mass"`, `"tool is required"`, `"args must be an object"` |
| 404 | Path doesn't exist, or tool doesn't exist | `"not found"`, `"tool not found: <name>"` |
| 429 | The agent is busy (another turn hasn't finished) | `"agent busy"` |
| 500 | Internal processing error | `"failed to load session"`, `"chat turn failed"` |
| 503 | Brain not configured (backend degraded) | `"agent brain not configured"` |

---

## CORS

- `OPTIONS` on every path → `204` with `Access-Control-Allow-Methods: GET, POST, OPTIONS`,
  `Access-Control-Allow-Headers: Content-Type, Accept`, `Access-Control-Max-Age: 600`.
- The `Access-Control-Allow-Origin` header is only set when the request `Origin` is one of
  `http://localhost:5173` or `http://localhost:1420` (with `Vary: Origin`).
