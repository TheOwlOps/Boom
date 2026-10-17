# Boom Gateway: Multi-Channel Ingress & Egress Architecture

## 1. Architectural Role

The Boom Gateway is a unified multiplexing server that bridges external communication channels into the agent reasoning core. It decouples network protocols, rate limits, and platform-specific message formatting from the internal ReAct engine.

---

## 2. Platform Ingress Topography

```
+-----------------------------------------------------------------------------------+
|                            External Ingress Endpoints                             |
|  - Telegram Polling / Webhook     - Discord Gateway Websocket                     |
|  - Slack Socket Mode / Webhook    - WhatsApp Cloud API Webhook                    |
|  - Zalo Local Bridge (:8787)      - Signal CLI REST API                           |
|  - WeChat / Weixin Official API   - BlueBubbles (iMessage) Webhook                |
+-----------------------------------------------------------------------------------+
                                          |
                              (Platform Event Ingestion)
                                          v
+-----------------------------------------------------------------------------------+
|                        Platform Adapter Normalization                             |
|  - Maps platform payload -> Unified `IncomingEvent`                               |
|  - Extracts: `channel`, `sender_id`, `session_id`, `content`, `media_urls`        |
|  - Normalizes media: Downloads audio/voice memos, images to local cache           |
+-----------------------------------------------------------------------------------+
                                          |
                               (Authorization & Pairing)
                                          v
+-----------------------------------------------------------------------------------+
|                            Ingress Security Guard                                 |
|  - Whitelist / Pairing Code Verification (`pairing.py`)                           |
|  - Rate-limit per sender (Anti-flood token bucket)                                |
|  - Bot-loop prevention (Ignores self-echo and mirrored bot messages)              |
+-----------------------------------------------------------------------------------+
                                          |
                               (Push to Run Queue)
                                          v
+-----------------------------------------------------------------------------------+
|                       Agent Dispatcher (`run_inbound.py`)                         |
|  - Resolves or resumes session in SQLite State DB                                 |
|  - Dispatches job to Turn Runner (`run_turn.py`)                                  |
+-----------------------------------------------------------------------------------+
```

---

## 3. Data Contract: `IncomingEvent` & `OutgoingEvent`

### Inbound Contract (`IncomingEvent`)
All incoming platform messages are mapped to this canonical shape:
```json
{
  "channel": "telegram | zalo | discord | slack | whatsapp",
  "channel_type": "dm | group | thread",
  "session_id": "conv-uuid-v4",
  "sender_id": "platform-user-id",
  "sender_name": "Display Name",
  "message_id": "native-message-id",
  "content": "Raw user prompt text",
  "attachments": [
    {
      "type": "image | audio | document",
      "url": "https://...",
      "local_path": "/cache/media/uuid.ogg"
    }
  ],
  "timestamp": 1728950400
}
```

### Outbound Contract (`OutgoingEvent`)
```json
{
  "channel": "telegram | zalo | discord",
  "recipient_id": "platform-user-id",
  "session_id": "conv-uuid-v4",
  "content": "Response markdown text",
  "reply_to_message_id": "native-message-id",
  "media_paths": ["/cache/audio/reply.mp3"],
  "format_mode": "markdown | plain | native_blocks"
}
```

---

## 4. Egress Delivery & Reliability Engine

### A. Token-Bucket Rate Limiting
Different channels impose strict API limits (e.g. Telegram: 30 msg/s global, 1 msg/s per chat; Discord: 5 msg/5s). The gateway implements:
- **Per-Channel Leaky Bucket**: Outgoing messages are queued and dequeued according to platform tier specs.
- **Exponential Backoff**: HTTP 429 Too Many Requests triggers automatic retry with jittered backoff.

### B. Message Chunking & Adaptation
- **Discord**: Automatically chunks text exceeding 2,000 characters along paragraph/newline boundaries without breaking code fences (```).
- **Telegram**: Converts standard Markdown to Telegram MarkdownV2 or HTML entities; splits at 4,096 characters.
- **Voice Synthesis Pipeline**: If audio output is configured, passes text through TTS provider (e.g. EdgeTTS, OpenAI TTS) and transmits as native voice bubbles (`MEDIA:` tag resolution).

### C. Live Typing Indicator
While the ReAct loop is iterating or calling external tools, the gateway emits continuous typing/status events to the external channel at 4-second intervals to maintain user engagement.
