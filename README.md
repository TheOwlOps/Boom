# Boom 🦉

<p align="center">
  <strong>The multi-channel AI agent harness that powers Muse.</strong><br/>
  Prompt in → guardrailed ReAct loops → tools dispatched across 8 channels.
</p>

<p align="center">
  <a href="https://github.com/TheOwlOps/Boom/actions/workflows/ci.yml"><img src="https://github.com/TheOwlOps/Boom/actions/workflows/ci.yml/badge.svg" alt="CI"/></a>
  <img src="https://img.shields.io/badge/rust-1.85-orange.svg" alt="Rust"/>
  <img src="https://img.shields.io/badge/node-22-green.svg" alt="Node 22"/>
  <img src="https://img.shields.io/badge/license-MIT-yellow.svg" alt="MIT"/>
  <img src="https://img.shields.io/badge/Muse-harness-orange.svg" alt="Muse harness"/>
</p>

<p align="center">
  <img src="docs/boom_harness.gif" width="100%" alt="Boom harness orchestrating Muse agent loops"/>
</p>

**Boom** is an autonomous, multi-channel AI agent harness. Chat with it through Telegram, Discord, Slack, Zalo, LINE, WhatsApp, Feishu, or Messenger — or use the desktop or mobile app. It plans, codes, researches, and runs your business operations on its own, with a human in the approval loop for anything risky.

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
| 🖥️ **Clients** | Desktop (Tauri + React + Rust) · Mobile (Android, Kotlin) |

## 🚀 Quick Start

### Desktop (Tauri + React + Rust)

Prerequisites: **Node.js >= 20 + npm** and **Rust** (via rustup).

```bash
git clone https://github.com/TheOwlOps/Boom.git
cd Boom
devbox shell    # optional reproducible environment: Node 22, Rust
cd apps/desktop
npm install
npm run tauri dev     # Tauri window with hot reload
```

Production installers:

```bash
cd apps/desktop
npm run tauri build   # bundles land in src-tauri/target/release/bundle/
```

The desktop app runs the **boom-core** Rust backend in-process (default
`:18080`) — no Python, no sidecar. Configure your LLM provider in
Settings → Providers, then chat. See
[docs/DESKTOP_BUILD.md](docs/DESKTOP_BUILD.md) for system requirements and
auto-update.

### Mobile (Android, Kotlin)

Native Android app (Jetpack Compose) — UI only for now; its backend comes later.

```bash
# Build a debug APK with Android SDK installed
cd apps/mobile/android
./gradlew assembleDebug
```

CI also builds `boom-android-debug.apk` on every push (see
`.github/workflows/mobile.yml`).

## 🛡️ Windows SmartScreen

Boom's Windows installers (`.msi`) are currently **unsigned**, so Windows SmartScreen
may show a blue warning — *"Windows protected your PC"* — with the publisher listed as
**Unknown publisher**.

This warning appears only because the installer has no code-signing certificate; it is
**not** a malware report. Boom is open source: every line of code is public, and anyone
can inspect or build it themselves.

**How to install anyway:**

1. When the blue SmartScreen dialog appears, click **"More info"**.
2. Click **"Run anyway"** to continue the installation.

See [docs/CODE_SIGNING.md](docs/CODE_SIGNING.md) for why the warning appears, how to
verify your download with SHA256 checksums, and the roadmap to signed installers.

## 📖 Documentation

| Doc | Description |
|-----|-------------|
| [docs/TOOLS.md](docs/TOOLS.md) | Tool catalog |
| [docs/BUILD.md](docs/BUILD.md) | Build guide |
| [docs/DESKTOP_BUILD.md](docs/DESKTOP_BUILD.md) | Desktop build details |

## 🏗️ Architecture

```text
┌──────────────────────────────────────────┐
│  apps/desktop (Tauri + React + Rust)     │
│  ┌────────────┐      ┌─────────────────┐  │
│  │  React UI  │─────▶│  boom-core      │  │
│  │            │ HTTP │  (Rust, in-     │  │
│  │            │◀─────│  process)       │  │
│  └────────────┘      └─────────────────┘  │
└──────────────────────────────────────────┘
┌──────────────────────────────────────────┐
│  apps/mobile (Kotlin, Android)           │
│  UI only — backend arrives later          │
└──────────────────────────────────────────┘
```

- **Rust** (`crates/boom-core/`): backend — LLM providers, tools, chat,
  storage (SQLite), served in-process by the desktop app
- **TypeScript** (`apps/desktop/`): React desktop app (Tauri)
- **Kotlin** (`apps/mobile/`): Android app (Jetpack Compose)
- **Markdown** (`skills/`): skill definitions shared across clients

## 🧪 Testing

```bash
# Rust backend
cargo test -p boom-core

# Desktop UI typecheck
cd apps/desktop && npx tsc --noEmit
```

## 🤝 Contributing

1. Fork the repo
2. Create a feature branch (`git checkout -b feat/amazing`)
3. Commit your changes
4. Push and open a Pull Request

See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) for the full guide.

## 📄 License

MIT — see [LICENSE](LICENSE).
