# Boom — Autonomous Agent Harness

Boom is a multi-channel autonomous AI agent: chat via Telegram/Discord/Slack/Zalo/LINE/WhatsApp/Feishu/Messenger, with a web dashboard, event triggers, durable long tasks, self-debugging code agent, RBAC, audit logs, and broadcast.

## Quick Start

### Option 1: Local (recommended for dev)

```bash
# 1. Clone
git clone https://github.com/TheOwlOps/Boom.git
cd Boom

# 2. Python deps
pip install httpx pydantic

# 3. System deps (for OCR)
# Ubuntu/Debian:
sudo apt install tesseract-ocr tesseract-ocr-vie tesseract-ocr-eng

# 4. Configure (optional — for channels & LLM)
python3 boom.py setup

# 5. Run everything
./start.sh all
```

Open:
- **Web UI:** http://127.0.0.1:5173
- **API:** http://127.0.0.1:18080

Stop: `./stop.sh`

### Option 2: Docker

```bash
docker compose up --build
```

Set `TELEGRAM_BOT_TOKEN` in `.env` or environment to connect Telegram.

### Individual services

```bash
./start.sh api      # API server only (:18080)
./start.sh web      # Web UI only (:5173)
./start.sh gateway  # Gateway daemon (:8788)
python3 boom.py             # Interactive CLI
python3 boom.py --gateway  # Gateway daemon
```

## Features

- **Brain**: ReAct + DAG + MASS unified planning (`agent/src/orchestration/brain.py`)
- **Autonomous agent**: plan → execute → verify → debug loop (`agent/src/autonomous/`)
- **Channels**: Telegram, Discord, Slack, LINE, Zalo, WhatsApp, Feishu, Messenger
- **Triggers**: event-driven automation — Gmail, price drops, news keywords, webhooks
- **Tasks**: durable long tasks with SQLite checkpoints + crash/resume
- **Verify**: critic, self-debug, fact-checking
- **Learning**: learns from conversations (SQLite)
- **Enterprise**: RBAC, append-only audit log, broadcast
- **OCR**: Tesseract (vie+eng) with queue + concurrency limit
- **Web UI**: React + Vite dashboard (chat, DAG viewer, channel monitor)

## Environment Variables

| Var | Description |
|-----|-------------|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `BOOM_API_PORT` | API port (default 18080) |
| `BOOM_INGRESS_PORT` | Gateway port (default 8788) |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | LLM provider keys |

## Project Layout

```
boom.py              # Main entrypoint
agent/src/           # Python agent (brain, tools, tasks, triggers, ...)
gateway/             # Multi-channel gateway (adapters, daemon, API)
apps/web/            # React dashboard
acp/                 # ACP adapter
evals/               # Test suites
skills/              # Agent skills
```

## Tests

```bash
python3 evals/test_triggers.py
python3 evals/test_autonomous.py
# ... see evals/ for all suites
```
