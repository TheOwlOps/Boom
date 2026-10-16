# Boom 🦉

<p align="center">
  <strong>The multi-channel AI agent harness that powers Muse.</strong><br/>
  Prompt in → guardrailed ReAct loops → tools dispatched across 8 channels.
</p>

<p align="center">
  <a href="https://github.com/TheOwlOps/Boom/actions/workflows/ci.yml"><img src="https://github.com/TheOwlOps/Boom/actions/workflows/ci.yml/badge.svg" alt="CI"/></a>
  <img src="https://img.shields.io/badge/python-3.12-blue.svg" alt="Python 3.12"/>
  <img src="https://img.shields.io/badge/node-22-green.svg" alt="Node 22"/>
  <img src="https://img.shields.io/badge/license-MIT-yellow.svg" alt="MIT"/>
  <img src="https://img.shields.io/badge/Muse-harness-orange.svg" alt="Muse harness"/>
</p>

<p align="center">
  <img src="docs/boom_harness.gif" width="100%" alt="Boom harness orchestrating Muse agent loops"/>
</p>

**Boom** is an autonomous, multi-channel AI agent harness. Chat with it through Telegram, Discord, Slack, Zalo, LINE, WhatsApp, Feishu, or Messenger — or use the web dashboard. It plans, codes, researches, and runs your business operations on its own, with a human in the approval loop for anything risky.

> **Harness, not a model.** Boom orchestrates frontier LLMs (OpenAI, Anthropic, and others) — think OpenClaw, rebuilt for multi-channel business operations.

## ✨ Features

| Category | Highlights |
|----------|------------|
| 🧠 **Brain** | ReAct + DAG + MASS unified planning |
| 💬 **Channels** | 8 platforms: Telegram, Discord, Slack, Zalo, LINE, WhatsApp, Feishu, Messenger |
| 🔍 **DeepSearch** | Multi-hop research with cross-source verification |
| 🤔 **DeepThink** | Multi-perspective analysis, no topic avoidance |
| 🤖 **Dynamic Agents** | Auto-spawn/retire subagents by context (Coder, Researcher, Data Scientist…) |
| 💻 **Autonomous Coding** | Plan → execute → verify → debug loop with AST refactoring |
| 🏢 **Business** | Multi-group management, RBAC, approval flows, auto reports |
| 🎤 **Voice** | STT + TTS voice conversations |
| 🖼️ **Media** | Image generation, OCR (Vietnamese + English), video analysis |
| 📊 **Documents** | Native XLSX (formulas/charts), PPTX, PDF generation |
| 🔌 **Plugins** | Manifest-based, with lifecycle, sandboxing, and dependency resolution |
| 🛡️ **Production** | Circuit breakers, rate limiting, structured logging, graceful shutdown |

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

```text
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

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.

## 📄 License

MIT — see [LICENSE](LICENSE).
