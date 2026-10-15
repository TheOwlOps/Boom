# Harness & Hermes Agent Master Architecture Specification

This documentation suite provides a complete technical analysis and architecture blueprint of the Hermes Agent system, detailing its design patterns, turn lifecycle, multi-channel gateway, tools engine, memory mechanisms, and extensibility protocols.

## Specification Index

1. **[Core Architecture & Turn Lifecycle](ARCHITECTURE_SPECS.md)**
   - Topography, ReAct loop mechanics, SQLite WAL State DB, Context Engineering, Micro-compaction, Trajectory compression.

2. **[Gateway & Multi-Channel Networking](GATEWAY_AND_CHANNELS.md)**
   - Ingress event normalization (`IncomingEvent`), token-bucket rate limiting, platform adapters (Telegram, Zalo, Discord, Slack, WhatsApp), and reliable egress delivery.

3. **[Tools & Execution Engine](TOOLS_AND_EXECUTION.md)**
   - Core tools vs dynamic catalog (`tool_search`), programmatic Python execution (`execute_code`), sandbox isolation, and security guardrails (`tirith`).

4. **[Memory & Closed Learning Loop](MEMORY_AND_LEARNING_LOOP.md)**
   - Tri-tier memory model (Semantic, Episodic, Procedural), Progressive Disclosure of skills, and autonomous self-learning (Reflexion) to generate `SKILL.md`.

5. **[Extensibility & ACP Adapter](EXTENSIBILITY_AND_ACP.md)**
   - The `register(ctx)` plugin architecture, host isolation mode, and the Agent Client Protocol (`acp_adapter`) for IDE integrations.
