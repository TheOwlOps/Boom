---
name: notify-hub
description: Send multi-channel notifications via webhook URLs from env.
version: 1.0.0
author: boom-overnight
license: MIT
platforms: [linux, macos]
tags: [notification, telegram, discord, slack, webhook]
category: communication
---

# Notify Hub

Send one message to multiple channels at once. Each channel reads its webhook URL
from an environment variable; a channel with missing config or an error fails
gracefully (recorded in the result, no raise, no blocking other channels):

| Channel    | Env var           | Notes                          |
|------------|-------------------|--------------------------------|
| telegram   | `TELEGRAM_WEBHOOK`| Telegram bot webhook URL       |
| discord    | `DISCORD_WEBHOOK` | Discord incoming webhook       |
| slack      | `SLACK_WEBHOOK`   | Slack incoming webhook         |
| webhook    | `GENERIC_WEBHOOK` | Arbitrary webhook, JSON `{"text": ...}` |

## Usage

```python
from notify import send, broadcast

# One channel — returns dict {ok, channel, error}
send("telegram", "Backup finished at 02:00")

# All channels — failed channels still return results, no raise
results = broadcast("Deploy succeeded", channels=["telegram", "discord", "slack"])
```

`broadcast` is useful after long cron jobs: report status even if only one channel
is configured successfully. Never hardcode webhook URLs in code — always via env.
