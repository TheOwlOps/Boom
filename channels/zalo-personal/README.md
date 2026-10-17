# Boom Zalo Personal Bridge

A **lightweight** bridge that lets Boom drive a **personal Zalo account**:
send/receive messages, reactions, media, stickers, group admin, and lookup.

It is intentionally small:

- **No persistent WebSocket** — incoming messages are fetched every 5 seconds.
- **No extra web framework** — the HTTP API uses Python's stdlib
  (`http.server`). The only dependency is `zalo-api`.

This bridge runs as a **separate, optional process**. It is NOT bundled with
the Boom desktop app — start it only if you need Zalo Personal.

## How it works

```
Zalo servers  <-->  bridge.py (zalo-api, polling)  <-->  Boom core (HTTP)
```

## API reference

### Auth & status

- `GET  /status` → `{"status": "qr_pending"|"connected"|"error", "reactions": [...14 emojis]}`
- `GET  /qr` → PNG QR code (scan with the Zalo mobile app, expires in 2 min)

### Messages

- `POST /send` → `{"to", "text", "thread_type": "user"|"group", "mentions"?}`.
  `mentions` = `[{"uid", "name"}]` → blue @mentions (group only).
- `POST /react` → `{"msg_id", "cli_msg_id"?, "to", "thread_type", "emoji"}`
  (emoji must be one of the 14 in `/status`).
- `POST /recall` → `{"msg_id", "cli_msg_id"?, "to", "thread_type"}` — unsend.
- `GET  /messages` → drains inbound queue.

### Media

- `POST /send-image` → `{"to", "thread_type", "file_path", "caption"?}`
  (jpg/png/gif/webp, path must be inside `$BOOM_HOME`).
- `POST /send-file` → `{"to", "thread_type", "file_path", "caption"?}`
  (pdf/doc/xls/ppt/txt/csv/zip…).
- `POST /send-voice` → `{"to", "thread_type", "file_path"}` (.m4a AAC).
- `POST /send-sticker` → `{"to", "thread_type", "keyword"}` — searches Zalo's
  sticker store and sends the top animated sticker.

### Events (SSE)

- `GET /events` → Server-Sent Events stream: chat messages + group events
  (join/leave/reaction…). Heartbeat every 15s.
- When a member joins a group, the bridge auto-sends a welcome message
  (template via `BOOM_ZALO_WELCOME`, `{name}` placeholder; mention included).

### Group admin (requires deputy/owner rights)

- `POST /group/rename` → `{"group_id", "name"}`
- `POST /group/avatar` → `{"group_id", "file_path"}` (image in `$BOOM_HOME`)
- `POST /group/add-deputy` → `{"group_id", "uid"}`
- `POST /group/kick` → `{"group_id", "uid"}`
- `POST /group/block` → `{"group_id", "uid"}`
- `GET  /group/members?group_id=...` → `[{"uid", "name", "avatar"}]`

### Lookup

- `POST /find-user` → `{"phone"}` — find a user by phone number.
- `GET  /user-info?uid=...` → `{"uid", "name", "avatar", "phone"}`.

## Install & run

```bash
cd channels/zalo-personal
pip install -r requirements.txt
python bridge.py
```

Then open `http://127.0.0.1:17890/qr` in a browser and scan the QR code with
the Zalo mobile app. On first successful login the session is saved to
`$BOOM_HOME/zalo_personal/session.json`, so you only scan once.

Environment variables:

| Variable                 | Default              | Meaning                          |
|--------------------------|----------------------|----------------------------------|
| `BOOM_ZALO_BRIDGE_PORT`  | `17890`              | Local port to listen on          |
| `BOOM_ZALO_POLL_INTERVAL`| `5`                  | Incoming-message poll seconds    |
| `BOOM_ZALO_WELCOME`      | `Chào mừng {name}…`  | New-member welcome template      |
| `BOOM_HOME`              | `~/.boom`            | Session dir + allowed file root  |

## Security

- Listens on **127.0.0.1 only** — never expose this port to a network.
- Session file is written with mode **0600**.
- File paths must resolve inside `$BOOM_HOME` (traversal rejected).
- Request bodies validated and length-capped; message content never logged.
- QR codes expire after 2 minutes.

## ⚠️ Important — unofficial API

This bridge uses the **unofficial** `zalo-api` package (a reverse-engineered
Zalo web client). Zalo does not bless automation:

- **Use a secondary Zalo account**, never your main one.
- Zalo may rate-limit or lock accounts that automate.
- The protocol can break when Zalo updates their apps — if login stops
  working, check for a newer `zalo-api` release.

You accept that risk by running this bridge.
