# Boom 🤖

[![CI](https://github.com/TheOwlOps/Boom/actions/workflows/ci.yml/badge.svg)](https://github.com/TheOwlOps/Boom/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Node 22](https://img.shields.io/badge/node-22-green.svg)](https://nodejs.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Boom** is an autonomous multi-channel AI agent harness. Chat with it via Telegram, Discord, Slack, Zalo, LINE, WhatsApp, Feishu, or Messenger — or use the web dashboard. It plans, codes, researches, and manages your business groups autonomously.

> **Harness, not a model.** Boom orchestrates frontier LLMs (OpenAI, Anthropic, etc.) — like OpenClaw, but built for multi-channel business operations.

## ✨ Features

| Category | Highlights |
|----------|------------|
| 🧠 **Brain** | ReAct + DAG + MASS unified planning |
| 💬 **Channels** | 8 platforms: Telegram, Discord, Slack, Zalo, LINE, WhatsApp, Feishu, Messenger |
| 🔍 **DeepSearch** | Multi-hop research with cross-source verification |
| 🤔 **DeepThink** | Multi-perspective analysis, no topic avoidance |
| 🤖 **Dynamic Agents** | Auto spawn/retire subagents by context (Coder, Researcher, Data Scientist...) |
| 💻 **Autonomous Coding** | Plan → execute → verify → debug loop with AST refactoring |
| 🏢 **Business** | Multi-group management, RBAC, approval flows, auto reports |
| 🎤 **Voice** | STT + TTS voice conversations |
| 🖼️ **Media** | Image generation, OCR (Vietnamese + English), video analysis |
| 📊 **Documents** | Native XLSX (formulas/charts), PPTX, PDF generation |
| 🔌 **Plugins** | Manifest-based with lifecycle, sandboxing, dependency resolution |
| 🛡️ **Production** | Circuit breaker, rate limiting, structured logging, graceful shutdown |

## 🚀 Quick Start

### Devbox (recommended)

```bash
git clone https://github.com/TheOwlOps/Boom.git
cd Boom
devbox shell    # reproducible environment (Nix-powered)
./start.sh all
```

### Manual

```bash
git clone https://github.com/TheOwlOps/Boom.git
cd Boom
pip install httpx pydantic
./start.sh all
```

### Docker

```bash
docker compose up --build
```

Open **http://127.0.0.1:5173** for the web dashboard.

## 📖 Documentation

| Doc | Description |
|-----|-------------|
| [docs/TOOLS.md](docs/TOOLS.md) | Full tool catalog (131 tools) |
| [docs/PLUGINS.md](docs/PLUGINS.md) | Plugin system guide |
| [docs/DEEPSEARCH.md](docs/DEEPSEARCH.md) | DeepSearch architecture |
| [docs/CHANNELS.md](docs/CHANNELS.md) | Adding new channels |

## 🏗️ Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Channels   │────▶│    Brain     │────▶│    Tools    │
│ (8 platforms)│     │ (ReAct+DAG+  │     │ (131 tools) │
└─────────────┘     │  MASS)       │     └─────────────┘
                    └──────────────┘
                           │
                    ┌──────┴──────┐
                    │   Memory    │
                    │ (FTS5+long) │
                    └─────────────┘
```

- **Python** (`agent/`): brain, tools, orchestration, business logic
- **TypeScript** (`apps/web/`): React dashboard
- **Rust** (`crates/`): sandbox host

## 🧪 Testing

```bash
# Run all evals
for f in evals/test_*.py; do python3 "$f"; done

# Or with pytest
python3 -m pytest evals/ -q
```

## 🤝 Contributing

1. Fork the repo
2. Create a feature branch (`git checkout -b feat/amazing`)
3. Commit your changes
4. Push and open a Pull Request

See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## 📄 License

MIT — see [LICENSE](LICENSE).
