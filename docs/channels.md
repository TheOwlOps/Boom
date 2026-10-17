# Channels — real platform integrations

Boom connects to chat platforms for real — no fake "connected" states.
Every credential is verified **live** against the platform API before it is
stored, and tokens live in `$BOOM_HOME/channels/<id>.json` (mode `0600`).

| Channel | Verify | Send | Receive (ingress) |
|---|---|---|---|
| Telegram | `getMe` | `sendMessage` | `getUpdates` polling ✅ |
| Discord | `GET /users/@me` | `POST /channels/{id}/messages` | channel message polling ✅ |
| Messenger | `GET /me` (Page token) | `POST /me/messages` | **webhook only** (see below) |
| Slack / WhatsApp / Line / Feishu / Zalo | — | — | honest `unsupported` |

## API

- `GET /api/channels` — inventory with `status` (`connected` /
  `not-configured` / `unsupported`).
- `POST /api/channels/{id}/connect` — body `{"token": "..."}`. The token
  is verified live first; a rejected credential is never saved.
- `POST /api/channels/{id}/disconnect` — idempotent.
- `POST /api/channels/{id}/test` — body `{"chat_id": "...", "message":
  "..."}`. With `chat_id` sends a real message; without it re-verifies.
- `GET /api/channels/{id}/updates?chat_id=...&after=...` — poll inbound
  messages. Response: `{messages: [{id, chat_id, sender, text, ts}]}`.
  - Telegram: `after` = last seen `update_id` (exclusive). Poll
    repeatedly and advance the cursor to avoid reprocessing.
  - Discord: `chat_id` is the **channel id** (required); `after` = last
    seen message id (exclusive).
  - Messenger: returns `501` — Messenger has no polling API.

## Telegram ingress

Two options:

1. **Polling (built in):** `GET /api/channels/telegram/updates?after=<id>`
   wraps `getUpdates` with `timeout=0` (returns immediately). Poll on an
   interval and store the highest `update_id` you have processed, then
   pass it as `after`.
2. **Webhook (alternative):** `setWebhook` to your public HTTPS endpoint,
   then forward updates into Boom yourself. Do **not** use webhook and
   `getUpdates` at the same time — Telegram delivers to only one.

## Discord ingress

Polling is built in: `GET
/api/channels/discord/updates?chat_id=<channel-id>&after=<message-id>`.

Setup notes:

1. Create an application at <https://discord.com/developers/applications>,
   add a Bot, copy the token.
2. **Privileged intent:** to read message *content*, enable **Message
   Content Intent** under Bot → Privileged Gateway Intents. Without it,
   `content` comes back empty (Discord's rule, not a Boom bug).
3. Invite the bot with `bot` scope (+ `Send Messages`, `Read Message
   History` permissions).
4. Get a channel id: Discord client → Settings → Advanced → Developer
   Mode → right-click channel → Copy Channel ID.
5. For realtime push instead of polling, use the Gateway WebSocket
   (`wss://gateway.discord.gg`) — not implemented in Boom yet; polling
   covers most automation needs.

Skip your own bot's messages when processing (compare `sender` against
your bot id) to avoid echo loops.

## Messenger ingress (webhook required)

Messenger offers **no polling API**. Receiving user messages requires a
webhook subscription:

1. Create an app at <https://developers.facebook.com/apps> → add the
   **Messenger** product.
2. Create/link a Facebook Page; generate a **Page access token**
   (this is the token you paste into Boom's Messenger connect).
3. In the app dashboard → Messenger → Webhooks → subscribe to the
   **Page** with callback URL `https://<your-host>/api/channels/messenger/webhook`
   and the `messages` field. (Serving this endpoint from Boom is not
   implemented yet — run a small forwarder or proxy it.)
4. Subscribe the app to the Page (`POST /{page-id}/subscribed_apps`).
5. Verification: Meta sends `GET` with `hub.verify_token` /
   `hub.challenge`; your endpoint must echo the challenge when the token
   matches.

Until the webhook endpoint exists in Boom, Messenger is **send-only**
from Boom's side — the UI reports this honestly instead of pretending.

## Security notes

- Tokens are never returned by any API, never logged, and never appear
  in error messages (transport errors degrade to generic text because
  some platforms put the token in the URL).
- Channel files are written atomically with `0600` permissions
  (Unix); the legacy single-file `$BOOM_HOME/channels.json` is still
  read as a fallback and migrated on next connect/disconnect.
