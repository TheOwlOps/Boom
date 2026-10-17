# Channels — real platform integrations

Boom connects to chat platforms for real — no fake "connected" states.
Every credential is verified **live** against the platform API before it is
stored, and tokens live in `$BOOM_HOME/channels/<id>.json` (mode `0600`).

| Channel | Verify | Send | Receive (ingress) |
|---|---|---|---|
| Telegram | `getMe` | `sendMessage` + media | `getUpdates` polling ✅ |
| Discord | `GET /users/@me` | `POST /channels/{id}/messages` + embeds | channel message polling ✅ |
| Messenger | `GET /me` (Page token) | `POST /me/messages` + rich | **webhook** ✅ |
| Slack | `auth.test` | `chat.postMessage` + Block Kit | Socket Mode / Events API (see below) |
| WhatsApp | `GET /{phone-number-id}` | `POST /{pn-id}/messages` | **webhook** ✅ |
| LINE | `GET /v2/bot/profile` | `POST /v2/bot/message/push` | **webhook** ✅ |
| Zalo | `GET /v2.0/oa/getoa` | `POST /v3.0/oa/message/cs` | **webhook** ✅ |
| Feishu | — | — | honest `unsupported` |

## API

- `GET /api/channels` — inventory with `status` (`connected` /
  `not-configured` / `unsupported`).
- `POST /api/channels/{id}/connect` — body `{"token": "..."}`. The token
  is verified live first; a rejected credential is never saved.
  - WhatsApp credential format: `<access_token>:<phone_number_id>`.
- `POST /api/channels/{id}/disconnect` — idempotent.
- `POST /api/channels/{id}/test` — body `{"chat_id": "...", "message":
  "..."}`. With `chat_id` sends a real message; without it re-verifies.
- `GET /api/channels/{id}/updates?chat_id=...&after=...` — poll inbound
  messages. Response: `{messages: [{id, chat_id, sender, text, ts}]}`.
  - Telegram: `after` = last seen `update_id` (exclusive). Poll
    repeatedly and advance the cursor to avoid reprocessing.
  - Discord: `chat_id` is the **channel id** (required); `after` = last
    seen message id (exclusive).
  - Messenger / WhatsApp / LINE / Zalo: drains the **webhook queue**
    (messages pushed by `POST /api/channels/{id}/webhook`).
  - Slack: returns `501` — no polling API (use Socket Mode or Events API).
- `POST /api/channels/{id}/ops/{operation}` — extended channel operations
  (see below). Body varies per operation.

## Channel operations

Every operation is also a **bot tool** (`<channel>_<operation>`), so bots
can use them when the channel is connected.

### Telegram (`POST /api/channels/telegram/ops/...`)
| Operation | Body | Tool |
|---|---|---|
| `send_photo` | `{chat_id, photo, caption?}` | `telegram_send_photo` |
| `send_document` | `{chat_id, document, caption?}` | `telegram_send_document` |
| `send_sticker` | `{chat_id, sticker}` | `telegram_send_sticker` |
| `edit_message` | `{chat_id, message_id, text}` | `telegram_edit_message` |
| `delete_message` | `{chat_id, message_id}` | `telegram_delete_message` |
| `get_chat_info` | `{chat_id}` | `telegram_get_chat_info` |
| `get_chat_members` | `{chat_id}` | `telegram_get_chat_members` |
| `pin_message` | `{chat_id, message_id}` | `telegram_pin_message` |
| `unpin_message` | `{chat_id, message_id?}` | — |

### Discord (`POST /api/channels/discord/ops/...`)
| Operation | Body | Tool |
|---|---|---|
| `send_embed` | `{channel_id, title, description, color?}` | `discord_send_embed` |
| `edit_message` | `{channel_id, message_id, text}` | `discord_edit_message` |
| `delete_message` | `{channel_id, message_id}` | `discord_delete_message` |
| `add_reaction` | `{channel_id, message_id, emoji}` | `discord_add_reaction` |
| `remove_reaction` | `{channel_id, message_id, emoji}` | — |
| `get_channel_info` | `{channel_id}` | `discord_get_channel_info` |
| `get_guild_info` | `{guild_id}` | `discord_get_guild_info` |
| `list_members` | `{guild_id}` | `discord_list_members` |

### Messenger (`POST /api/channels/messenger/ops/...`)
| Operation | Body | Tool |
|---|---|---|
| `send_image` | `{psid, image_url}` | `messenger_send_image` |
| `send_quick_replies` | `{psid, text, buttons: [{title, payload}]}` | `messenger_send_quick_replies` |
| `send_buttons` | `{psid, title, buttons: [{title, payload}]}` | `messenger_send_buttons` |
| `get_user_profile` | `{psid}` | `messenger_get_user_profile` |
| `set_typing` | `{psid, action?}` (`typing_on`/`typing_off`/`mark_seen`) | `messenger_set_typing` |

### Slack (`POST /api/channels/slack/ops/...`)
| Operation | Body | Tool |
|---|---|---|
| `send_blocks` | `{channel, text, blocks?}` | `slack_send_blocks` |
| `update_message` | `{channel, ts, text}` | `slack_update_message` |
| `delete_message` | `{channel, ts}` | `slack_delete_message` |
| `list_channels` | `{}` | `slack_list_channels` |
| `get_user_info` | `{user_id}` | `slack_get_user_info` |

## Webhook ingress (Messenger / WhatsApp / LINE / Zalo)

Platforms without a polling API deliver inbound messages to a webhook URL
you register on their developer console. Boom exposes one endpoint per
platform; verified events are queued and `GET /api/channels/{id}/updates`
drains the queue.

### Messenger
1. Create an app at <https://developers.facebook.com/apps> → add the
   **Messenger** product.
2. Create/link a Facebook Page; generate a **Page access token**
   (paste into Boom's Messenger connect, or use the OAuth login flow).
3. `POST /api/channels/messenger/webhook/verify-token` with
   `{"token": "<your-verify-token>"}` — Boom echoes `hub.challenge`
   only when Meta's `hub.verify_token` matches.
4. In the app dashboard → Messenger → Webhooks → subscribe the Page with
   callback URL `https://<your-host>/api/channels/messenger/webhook`
   and the `messages` field.
5. `POST /api/channels/messenger/webhook` receives events → parsed via
   `messenger::parse_webhook` (skips echoes, keeps text + postbacks).

### WhatsApp
Same Meta webhook scheme as Messenger:
1. Meta App → add **WhatsApp** product → get a **phone number id**.
2. Generate a System User **access token**; Boom's connect credential is
   `<access_token>:<phone_number_id>`.
3. `POST /api/channels/whatsapp/webhook/verify-token` with your verify token.
4. Subscribe webhook `https://<your-host>/api/channels/whatsapp/webhook`
   to the `messages` field.
5. `POST /api/channels/whatsapp/webhook` receives events → parsed via
   `whatsapp::parse_webhook` (text messages only).

### LINE
1. <https://developers.line.biz/console> → create a **Messaging API**
   channel → issue a long-lived **Channel access token**.
2. Paste the token into Boom's LINE connect.
3. In the channel's **Messaging API** settings, set the Webhook URL to
   `https://<your-host>/api/channels/line/webhook` and enable it.
4. `POST /api/channels/line/webhook` receives events → parsed via
   `line::parse_webhook` (text messages only).
5. For production, verify the `X-Line-Signature` header with the channel
   secret (not yet enforced by Boom — put the endpoint behind your own
   check if exposed publicly).

### Zalo
1. <https://developers.zalo.me> → create an **Official Account (OA)**.
2. OA → get an **access token**; paste into Boom's Zalo connect.
3. In the OA settings, register the webhook URL
   `https://<your-host>/api/channels/zalo/webhook`.
4. `POST /api/channels/zalo/webhook` receives `user_send_text` events →
   parsed via `zalo::parse_webhook`.

### Slack ingress
Slack has no simple polling API. Two options:
1. **Socket Mode** (recommended for bots): enable in the app dashboard,
   get an **app-level token** (`xapp-...`), connect to
   `wss://wss-primary.slack.com` and listen for `message` events.
   (Not implemented in Boom yet — the `501` from `/updates` is honest.)
2. **Events API**: subscribe to `message.channels` etc. with a public
   HTTPS request URL. (Not implemented in Boom yet.)

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

## Messenger OAuth login (browser-based, no token paste)

Boom can log you in with Facebook instead of asking for a Page token:

1. Create a free Facebook App at <https://developers.facebook.com>
   → **Create App**, then add the **Facebook Login** product.
2. Copy the **App ID** and **App Secret** (App Settings → Basic) and
   paste them into Boom: Tools → Channels → Messenger → expand →
   they are stored at `$BOOM_HOME/channels/messenger_oauth.json` (`0600`).
3. Click **Connect with Facebook**: Boom opens the Facebook login page
   in your browser. After you approve, Facebook redirects back to
   `http://localhost:<port>/api/channels/messenger/oauth/callback`
   (this server) which exchanges the code, picks your first managed
   Page, verifies its Page token and stores it — the manual
   `POST /api/channels/messenger/connect` with a pasted token still
   works as a fallback.

Notes:

- While the app is in **Development** mode, `localhost` redirect URIs
  are exempt from the "Valid OAuth Redirect URIs" list; add the exact
  callback URL there if Facebook complains (required once the app is
  Live).
- In Development mode the login only works for users with a role on
  the app (admin/developer/tester) and Pages they manage.
- `pages_messaging` needs App Review before **Live**-mode strangers can
  use it; your own Pages work in dev mode without review.

API surface:

- `GET /api/channels/messenger/oauth/config` →
  `{configured, app_id}` (the secret is never returned).
- `POST /api/channels/messenger/oauth/config` — body
  `{app_id, app_secret}`.
- `GET /api/channels/messenger/oauth/start` → `{url}` — the Facebook
  Login URL (single-use CSRF `state` minted server-side).
- `GET /api/channels/messenger/oauth/callback?code=…&state=…` — returns
  a small HTML landing page (this URL loads in the browser tab).

## Messenger ingress (webhook required)

Messenger offers **no polling API**. Receiving user messages requires a
webhook subscription:

1. Create an app at <https://developers.facebook.com/apps> → add the
   **Messenger** product.
2. Create/link a Facebook Page; generate a **Page access token**
   (paste into Boom's Messenger connect, or use the OAuth login flow above).
3. `POST /api/channels/messenger/webhook/verify-token` with
   `{"token": "<your-verify-token>"}` — Boom echoes `hub.challenge`
   only when Meta's `hub.verify_token` matches.
4. In the app dashboard → Messenger → Webhooks → subscribe the Page with
   callback URL `https://<your-host>/api/channels/messenger/webhook`
   and the `messages` field.
5. Subscribe the app to the Page (`POST /{page-id}/subscribed_apps`).
6. Poll `GET /api/channels/messenger/updates` to drain received messages.

## Security notes

- Tokens are never returned by any API, never logged, and never appear
  in error messages (transport errors degrade to generic text because
  some platforms put the token in the URL).
- Channel files are written atomically with `0600` permissions
  (Unix); the legacy single-file `$BOOM_HOME/channels.json` is still
  read as a fallback and migrated on next connect/disconnect.
