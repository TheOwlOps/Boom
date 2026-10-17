# Boom Zalo Personal Bridge

A **minimal, lightweight** bridge that lets Boom send and receive messages
through a **personal Zalo account**.

It is intentionally small:

- **3 features only** — QR login, send text, receive text (HTTP polling).
- **No persistent WebSocket** — incoming messages are fetched every 5 seconds.
- **No extra web framework** — the HTTP API uses Python's stdlib
  (`http.server`). The only dependency is `zalo-api`.

This bridge runs as a **separate, optional process**. It is NOT bundled with
the Boom desktop app — start it only if you need Zalo Personal.

## How it works

```
Zalo servers  <-->  bridge.py (zalo-api, polling)  <-->  Boom core (HTTP)
```

- `GET  /status` → `{"status": "qr_pending"|"connected"|"error"}`
- `GET  /qr` → PNG QR code (scan with the Zalo mobile app, expires in 2 min)
- `POST /send` → `{"to": "<user/group id>", "text": "...", "thread_type": "user"|"group"}`
- `GET  /messages` → drains the inbound queue:
  `{"messages": [{"id", "thread_id", "thread_type", "author_id", "text", "ts"}]}`

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
| `BOOM_HOME`              | `~/.boom`            | Where the session file is stored |

## Security

- Listens on **127.0.0.1 only** — never expose this port to a network.
- Session file is written with mode **0600**.
- Request bodies are validated and length-capped; credentials never logged.
- QR codes expire after 2 minutes.

## ⚠️ Important — unofficial API

This bridge uses the **unofficial** `zalo-api` package (a reverse-engineered
Zalo web client). Zalo does not bless automation:

- **Use a secondary Zalo account**, never your main one.
- Zalo may rate-limit or lock accounts that automate.
- The protocol can break when Zalo updates their apps — if login stops
  working, check for a newer `zalo-api` release.

You accept that risk by running this bridge.
