# Tài liệu API — Boom Gateway

> Tài liệu này mô tả HTTP API của **Boom gateway** (module `gateway/api.py`, version `0.1.0`).
> Server mặc định chạy tại `http://127.0.0.1:18080` (đổi qua biến môi trường `BOOM_API_PORT`).
>
> **Lưu ý bảo mật (v1):** API v1 **không có auth**. Server chỉ bind `127.0.0.1` (localhost-only),
> không tiếp xúc mạng ngoài. CORS chỉ phản hồi cho 2 origin dev:
> `http://localhost:5173` (Vite) và `http://localhost:1420` (Tauri) — không bao giờ `*`.
> Giới hạn request body: **1 MB** (vượt quá → `400`).

## Tổng quan endpoints

| # | Method | Path | Mô tả |
|---|--------|------|-------|
| 1 | GET | `/api/health` | Trạng thái server, version, model, uptime, số lượng tools/skills |
| 2 | GET | `/api/logs` | Lấy log buffer trong bộ nhớ (mới nhất trước) |
| 3 | GET | `/api/sessions` | Danh sách session chat (hiện tại luôn rỗng — chưa có listing API) |
| 4 | GET | `/api/channels/status` | Trạng thái kết nối 8 kênh (Telegram, Discord, Slack, WhatsApp, Line, Feishu, Zalo, Messenger) |
| 5 | GET | `/api/dag/status` | Trạng thái nodes của lần chạy DAG gần nhất (hiện tại luôn `[]`) |
| 6 | GET | `/api/tools` | Liệt kê tất cả tools thật từ environment |
| 7 | GET | `/api/skills` | Quét thư mục `skills/` thật của repo |
| 8 | POST | `/api/chat` | Gửi 1 chat turn cho Brain (hỗ trợ SSE streaming) |
| 9 | POST | `/api/tools/run` | Chạy thử 1 tool (qua sandbox nếu có) |

Ngoài ra server xử lý `OPTIONS` trên mọi path (CORS preflight → `204`).

## Base URL

```
http://127.0.0.1:18080
```

---

## 1. GET /api/health — Kiểm tra trạng thái server

Trả về trạng thái cơ bản. `tools_count`/`skills_count` được đếm **thật** từ
environment và thư mục `skills/` của repo.

**Response 200** — `application/json`:

| Field | Type | Mô tả |
|-------|------|-------|
| `status` | string | Luôn `"ok"` |
| `version` | string | API version, ví dụ `"0.1.0"` |
| `model` | string | Tên LLM model, hoặc `"unconfigured"` khi backend chưa cấu hình |
| `uptime_sec` | integer | Số giây kể từ khi server start |
| `tools_count` | integer | Số tools thật load từ `agent/src/environment.py` |
| `skills_count` | integer | Số thư mục con trong `skills/` |

**Ví dụ:**

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

## 2. GET /api/logs — Lấy log buffer

Đọc log buffer trong bộ nhớ (tối đa 200 entries mới nhất). Mỗi entry có `ts` (ISO-8601)
và `msg` (cắt ở 500 ký tự).

**Query params:**

| Param | Type | Default | Mô tả |
|-------|------|---------|-------|
| `limit` | integer | 50 | Số dòng log, kẹp trong khoảng 1–200 |

**Response 200:**

| Field | Type | Mô tả |
|-------|------|-------|
| `logs` | array | Mảng entries, mỗi entry `{"ts": string, "msg": string}` |

**Ví dụ:**

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

## 3. GET /api/sessions — Danh sách session

> **Lưu ý:** `ReActLoop` chỉ có `load_session()` mà không có listing API, nên endpoint này
> trả về `[]` một cách trung thực — không bịa session.

**Response 200:**

```json
{ "sessions": [] }
```

**Ví dụ:**

```bash
curl http://127.0.0.1:18080/api/sessions
```

---

## 4. GET /api/channels/status — Trạng thái các kênh

Kiểm tra khả năng gửi tin của **8 kênh cố định** (contract): `telegram`, `discord`,
`slack`, `whatsapp`, `line`, `feishu`, `zalo`, `messenger`.
Trạng thái `connected` được hỏi trực tiếp từ daemon (`daemon.can_deliver(name)`).

**Response 200:**

| Field | Type | Mô tả |
|-------|------|-------|
| `channels` | array | Mỗi item: `{"name": string, "connected": boolean, "detail": string \| null}` |

`detail` là gợi ý credential còn thiếu (ví dụ `"TELEGRAM_BOT_TOKEN not set"`),
`"gateway daemon not wired"` khi daemon chưa được nối, hoặc `null` khi đã connected.

**Ví dụ:**

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

## 5. GET /api/dag/status — Trạng thái DAG

> **Lưu ý:** Brain hiện tại không lưu trạng thái lần chạy cuối, nên endpoint này trả về
> `{"nodes": []}`. Nếu Brain tương lai expose `last_result` (nodes + node statuses),
> server sẽ trả về đúng contract shape thay vì tự bịa.

**Response 200:**

| Field | Type | Mô tả |
|-------|------|-------|
| `nodes` | array | Mỗi item: `{"id": string, "name": string, "status": string, "dependencies": string[]}` |

`status` có thể là: `completed`, `idle`, hoặc giá trị do Brain cung cấp.

**Ví dụ:**

```bash
curl http://127.0.0.1:18080/api/dag/status
```

```json
{ "nodes": [] }
```

---

## 6. GET /api/tools — Liệt kê tools

Liệt kê **tools thật** được build từ `agent/src/environment.py` (mỗi tool có `name`,
`description`, `group`).

**Response 200:**

| Field | Type | Mô tả |
|-------|------|-------|
| `tools` | array | Mỗi item: `{"name": string, "description": string, "group": string}` |
| `count` | integer | Số lượng tools |

**Ví dụ:**

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

## 7. GET /api/skills — Liệt kê skills

Quét **thật** thư mục `skills/` của repo (sắp xếp theo tên thư mục). `description`
lấy từ dòng text đầu tiên trong `SKILL.md` (cắt ở 200 ký tự), rỗng nếu không có file.

**Response 200:**

| Field | Type | Mô tả |
|-------|------|-------|
| `skills` | array | Mỗi item: `{"name": string, "description": string}` |
| `count` | integer | Số lượng skills |

**Ví dụ:**

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

## 8. POST /api/chat — Gửi chat turn

Chạy **một chat turn** qua Brain. Các turn được serialize bằng lock:
nếu có turn khác đang chạy, request trả về `429 "agent busy"`.

**Request body** — `application/json`:

| Field | Type | Required | Mô tả |
|-------|------|----------|-------|
| `prompt` | string | **bắt buộc** | Nội dung câu hỏi/lệnh |
| `strategy` | string | không | Một trong: `auto`, `react`, `dag`, `mass`. Default: `"auto"` |
| `session_id` | string | không | ID session. Nếu rỗng/không có, server tự sinh `"session_<unix_ts>"` |

**Response 200 (JSON thường)** — `application/json`:

| Field | Type | Mô tả |
|-------|------|-------|
| `reply` | string | Câu trả lời của agent |
| `session_id` | string | Session ID đã dùng (truyền lại cho turn tiếp theo) |
| `strategy` | string | Strategy thực sự được dùng |
| `errors` | object | Lỗi phụ theo node (thường `{}`) |

**Ví dụ:**

```bash
curl -X POST http://127.0.0.1:18080/api/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Tìm tin mới nhất về AI", "strategy": "react", "session_id": "demo-1"}'
```

```json
{
  "reply": "Đây là kết quả tìm kiếm...",
  "session_id": "demo-1",
  "strategy": "react",
  "errors": {}
}
```

### 8b. POST /api/chat — Streaming qua SSE

Nếu request có header `Accept: text/event-stream`, server trả về
`Content-Type: text/event-stream` với các SSE events:

| Event | Payload | Mô tả |
|-------|---------|-------|
| `status` | `{"phase": "thinking"}` | Gửi ngay khi bắt đầu xử lý |
| `message` | `{"text": "..."}` | Toàn bộ reply (v1 không stream token thật — client tự animate) |
| `done` | `{"session_id": "...", "strategy": "..."}` | Kết thúc |
| `error` | `{"error": "chat turn failed"}` | Khi turn lỗi |

**Ví dụ:**

```bash
curl -N -X POST http://127.0.0.1:18080/api/chat \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"prompt": "Xin chào"}'
```

```
event: status
data: {"phase": "thinking"}

event: message
data: {"text": "Chào bạn!"}

event: done
data: {"session_id": "session_1729041000", "strategy": "react"}
```

---

## 9. POST /api/tools/run — Chạy thử một tool

Chạy thử 1 tool từ environment. Nếu `agent/src/security/toolsandbox.py`
tồn tại thì chạy qua `SandboxedToolRunner` (ghi nhận `user="api"`),
không thì gọi trực tiếp `func(**args)`.

**Request body** — `application/json`:

| Field | Type | Required | Mô tả |
|-------|------|----------|-------|
| `tool` | string | **bắt buộc** | Tên tool (phải tồn tại trong environment) |
| `args` | object | không | Tham số truyền cho tool. Default: `{}` |

**Response 200** — luôn HTTP 200, phân biệt bằng `ok`:

| Field | Type | Mô tả |
|-------|------|-------|
| `ok` | boolean | `true` khi chạy xong không exception |
| `result` | string | Kết quả (stringify, cắt ở 2000 ký tự) — chỉ khi `ok: true` |
| `error` | string | Thông báo lỗi (cắt ở 500 ký tự) — chỉ khi `ok: false` |

**Ví dụ:**

```bash
curl -X POST http://127.0.0.1:18080/api/tools/run \
  -H "Content-Type: application/json" \
  -d '{"tool": "web_search", "args": {"query": "Boom AI"}}'
```

```json
{
  "ok": true,
  "result": "[{...kết quả search...}]"
}
```

Lỗi runtime (HTTP 200, `ok: false`):

```json
{
  "ok": false,
  "error": "connection timeout"
}
```

---

## Error codes chung

Mọi lỗi đều trả về JSON shape `{"error": "<message>"}`, **không bao giờ** HTML:

| HTTP | Khi nào | Ví dụ message |
|------|---------|---------------|
| 400 | Request body không hợp lệ | `"invalid Content-Length"`, `"request body too large"` (vượt 1MB), `"request body is not valid JSON"`, `"request body must be a JSON object"`, `"prompt is required"`, `"strategy must be one of: auto, react, dag, mass"`, `"tool is required"`, `"args must be an object"` |
| 404 | Path không tồn tại hoặc tool không tồn tại | `"not found"`, `"tool not found: <name>"` |
| 429 | Agent đang bận (turn khác chưa xong) | `"agent busy"` |
| 500 | Lỗi nội bộ khi xử lý | `"failed to load session"`, `"chat turn failed"` |
| 503 | Brain chưa được cấu hình (backend degraded) | `"agent brain not configured"` |

---

## CORS

- `OPTIONS` trên mọi path → `204` với `Access-Control-Allow-Methods: GET, POST, OPTIONS`,
  `Access-Control-Allow-Headers: Content-Type, Accept`, `Access-Control-Max-Age: 600`.
- Header `Access-Control-Allow-Origin` chỉ được set khi request `Origin` là một trong
  `http://localhost:5173` hoặc `http://localhost:1420` (kèm `Vary: Origin`).
