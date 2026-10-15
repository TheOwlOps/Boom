# Hermes Parity & Architecture Analysis

This document provides a comprehensive analysis of the architectural mechanisms adapted from **Hermes Agent** (`NousResearch/hermes-agent`) into **Boom Harness**.

---

## 1. Architecture Alignment Overview

| Capability | Hermes Agent (`NousResearch`) | Boom Harness Implementation | Status |
| :--- | :--- | :--- | :--- |
| **State Storage** | SQLite WAL with FTS5 table indexing all historical conversation content | `agent/src/memory/state_db.py` (`StateDB` with FTS5 search) | Verified — CJK included: `segment_cjk` spaces CJK/Hiragana/Katakana/Hangul per character before indexing and before searching, since `unicode61` does not segment them (`schema_version = 2` backfills an older DB). Per-character tokens mean a Chinese query matches those characters anywhere in a row, not as a contiguous phrase |
| **Checkpoint / Rewind** | Session checkpoint and rewind | `agent/src/memory/checkpoints.py` (`CheckpointStore`) + loop hook in `ReActLoop.execute_step` | Verified — the loop snapshots a file's bytes the first time the session writes it (first snapshot wins, so a rewind goes back to before the session), and the `rewind` tool restores the overwritten files and deletes the created ones (`dry_run` first if you want the list). Paths outside the project root are refused. A real rewind is recorded in the transcript — a system message plus a `rewind` timeline event with the file lists — so later turns do not act on edits that no longer exist; a `dry_run` records nothing. Limits: only `write_file`/`patch_file` are tracked (they are the only file mutators this harness exposes), a shell command's edits are not, and the transcript is annotated rather than truncated — earlier turns stay readable, and the note is what tells the model the files went back |
| **MCP Client** | `tools/mcp_tool*.py` on the `mcp` SDK: stdio + Streamable HTTP + SSE, OAuth, reconnect loop | `agent/src/mcp/` (`MCPClient`, `register_mcp_tools`), stdlib JSON-RPC over stdio, tools named `mcp__<server>__<tool>` | Verified against `@modelcontextprotocol/server-everything` — 13 tools discovered and called. Two transports: a spawned stdio server, and streamable HTTP (`"url": ...`) where one POST carries a JSON-RPC message and the reply is a JSON body or an SSE stream, with the server's `Mcp-Session-Id` echoed back on every later request. An `mcp.json` header value may be `${VAR}`, so a token stays in the environment. A transport failure or a `404` on a session the server forgot costs one reconnect and the call is retried; the tool list is not refreshed until the next start. No OAuth flow, and a server-sent request needs no live stream here (`initialize` over SSE is read whole) |
| **Web Tools** | Managed web search/extract providers under `plugins/web/*` | `agent/src/tools/web_tools.py` (`web_search`, `web_extract`) | Verified — real DuckDuckGo HTML search (no API key) and real page fetch, parsed by a pure parser; visible text extracted with `html.parser`, script/style dropped, output capped and the truncation reported. Failures return `{"error": ...}`, never example rows. Verified live; the provider plugins under `plugins/web/*` still expect modules that never existed here |
| **Scheduled Jobs** | `cronjob_tool.py` scheduling | `agent/src/tools/cronjob_tool.py` (`parse_cron`, `next_run`, `CronStore`, `CronRunner`) | Verified — jobs in `memories/cron_jobs.json`, a real 5-field parser (`*`, `*/n`, ranges, lists, vixie day-field OR), and a runner that fires due prompts through the agent loop. Hosted by `cli.py`'s daemon (`_serve_cron`), not the blocking REPL. A prompt that raises is retried `BOOM_CRON_RETRIES` times (default 0) with a doubling `BOOM_CRON_RETRY_DELAY`; a job that was down replays `BOOM_CRON_CATCHUP` missed occurrences (default 1) when it is next met, and the record reports how many were missed. Schedule times are host-local: a cron expression carries no timezone |
| **Turn Queue / Backpressure** | Not present | `gateway/dispatcher.py` (`TurnDispatcher`) | Verified — every channel turn goes through one pool of `BOOM_GATEWAY_CONCURRENCY` (default 2) workers. Per channel at most `BOOM_GATEWAY_QUEUE` (default 32) turns wait, and at most 4× that in total, after which the caller is told the queue is full rather than the message being swallowed or the process growing. The pool picks round-robin across channels and `BOOM_GATEWAY_CHANNEL_CONCURRENCY` (default: the pool size) caps one channel's share, so a flooded chat cannot starve another. A caller that can hold its request open (`wait=True`; the ingress when the bridge gave no callback) gets the reply in-band; a channel that must not be stalled (Telegram's poll loop, Discord's socket) is acked immediately (`Yêu cầu đang được xử lý…`) and answered later through `send_to` on a client of its own, so a listener that reconnects cannot take a reply down with it. Accepted jobs are written to `memories/gateway_jobs.json` before they run, so a restart re-queues the unfinished ones. `POST /cancel {"job_id": …}` stops a waiting or running turn and `{"channel": …}` stops everything waiting or running on one channel; `GET /health` reports the running job ids, each channel's queue, and the retry counters. A turn that raises is retried `BOOM_GATEWAY_RETRIES` times (default 1) after a backoff of `BOOM_GATEWAY_RETRY_DELAY × 2^(attempt-1)` seconds (default 2s, then 4s), during which the worker is free and the job is still written as unfinished; the retry then goes to the front of its channel's line. A job may carry a priority 0-9 (the ingress takes `"priority"` from the bridge's event) and a head marked with the highest priority waiting anywhere is served before the round-robin's next channel, so urgency is the one exception to fair rotation; equal priority stays first-in-first-out |
| **Dynamic Prompt** | Identity, Environment Facts, Platform Tone, Memory Notes, `<available_skills>` block | `agent/src/loop/prompt_builder.py` (`SystemPromptBuilder`) | Verified |
| **Daemon Tracker** | `processes.json` tracking background PID, status, stdout/stderr streams | `agent/src/tools/process_manager.py` (`ProcessManager`) | Verified |
| **Token Accounting**| Usage pricing per model, cache read/write token counters | `agent/src/loop/token_accounting.py` (`TokenAccountant`) | Verified |
| **Browser Control**| CDP headless browser navigation and JS evaluation | `agent/src/tools/browser_tool.py` (`BrowserTool`) + `agent/src/tools/ws_client.py` (stdlib RFC6455) | Verified against real Chrome — launches headless with `--remote-debugging-port=0` (port read from `DevToolsActivePort`), then `Page.navigate` / `Runtime.evaluate` / `Page.captureScreenshot` / `Browser.close` over a hand-rolled WebSocket client. Tools: `browser_navigate`, `browser_eval`, `browser_screenshot`, `browser_close`, plus tabs (`browser_tabs`, `browser_open_tab`, `browser_use_tab`, `browser_close_tab` — the session keeps the target id it is attached to, and a tab closed under it falls back rather than failing) and input (`browser_click`, `browser_fill` — real `Input.dispatchMouseEvent` at the element's own box centre, and a field that kept a different value is an error, never a reported fill). No browser found → `{"error": ...}`. The shared client in `agent/src/tools/ws_client.py` now wraps the socket in `ssl` for `wss://` (the Discord channel connects over it); CDP itself is still loopback `ws://`. No network interception, no wait-for-selector, no element screenshot |
| **Reflex Router** | System 1 fast-path commands (`/ping`, `/status`) and injection guardrail | `agent/src/security/system_one_gate.py` (`SystemOneGate`) | Verified |
| **Multi-Agent** | Subagent task delegation, DAG dependency graph, MASS consensus | `agent/src/orchestration/{dag,mass}.py` & `tools/delegation.py` | Verified — the three are joined: `DAGScheduler` orders the subtasks, `MASSSwarm` gives each role its instructions, contract and toolset, and every node runs a real child `ReActLoop` whose transcript lands in StateDB. Dependencies' results are passed into their dependents. Parallel subtasks capped at 4, subagents capped by `token_budget`, optional `fail_fast`; subagents get no `delegate_task` and no `execute_code` |
| **Multi-Agent Self-Supervision** | Not present | `agent/src/orchestration/self_supervision.py`, `optimize_workflow` tool | Adapted from MASS (UC Berkeley + Sakana AI, [arXiv:2610.12176](https://arxiv.org/abs/2610.12176)) — adversarial critics falsify a deliverable (`verify` on a subtask, or the last hop of a workflow), the producer revises against the *evidenced* defects, and an unparsed verdict never counts as a pass. The optimizer leg searches candidate workflows, scores them and keeps the best in `memories/workflows.json` for replay. Boom has no weights to distill, so the outer loop lands in that file. Second-provider critics via `BOOM_EVALUATOR_BASE_URL` / `_MODEL` are the only defence here against the paper's own caveat (shared weights, shared blind spots) |
| **Protocol Adapter**| ACP (Agent Client Protocol) JSON-RPC standard for IDE integration | `acp_adapter/__init__.py` | Partial — the minimal adapter drives the real loop; the hermes-derived modules there need the `acp` SDK and do not import |
| **Ingress Gateway** | Rust Gateway with Token Bucket rate limiter & Webhook HTTP server | `gateway/ingress.py` (`IngressServer`) on `crates/gateway/`'s route | Verified — the daemon serves `POST /webhook/<channel>`, `GET /health` and `POST /cancel {"job_id": …}` over loopback with stdlib `asyncio.start_server`: a bridge posts `{sender_id, sender_name, text, message_id}` (and optionally a loopback `reply_url`) and the turn runs behind the owner guard and the `DeliveryLedger`, so a retried `message_id` is dropped. `BOOM_INGRESS_TOKEN` requires `X-Boom-Token` on every route; bodies are capped at 64 KB. This is the Python listener for the route `crates/gateway` defines, since nothing constructs its `GatewayServer`. Not implemented: TLS, per-channel auth, non-loopback callbacks |
| **Multi-Platform Channels** | Adapters for Telegram, Discord, Slack, Zalo, Feishu, WhatsApp, Teams, LINE, Matrix, IRC, … | `gateway/` at the repo root (`daemon.py`, `discord.py`, `ingress.py`) | **Telegram, Discord, plus any push-based bridge.** `gateway/` is a peer of `agent/` at the root, because every message from outside enters there. Telegram is long-polled (`getUpdates` / `sendMessage`); Discord is a real gateway client (`DISCORD_BOT_TOKEN`: `GET /gateway/bot`, `wss://` with TLS, op 2 IDENTIFY, op 1 heartbeats, `MESSAGE_CREATE` dispatch, replies over `POST /channels/<id>/messages`, reconnect on op 7/9, 2000-char replies truncated); every other platform arrives through the ingress webhook (no SDK per platform). All three paths go through the owner guard, the `DeliveryLedger` and the bounded turn queue; a queued reply is sent by the daemon's own `send_to` rather than a closure over the listener's socket, and a Discord reply is addressed to the channel the message arrived on (`TurnJob.target`), not to its author. A Slack or WhatsApp turn that arrives through the ingress can be answered without a reply_url: `send_to` posts `chat.postMessage` (`BOOM_SLACK_BOT_TOKEN`, and `{"ok": false}` in a 200 counts as undelivered) or the WhatsApp Cloud API (`BOOM_WHATSAPP_TOKEN` + `BOOM_WHATSAPP_PHONE_ID`), each with an overridable `*_API_BASE`. Slack and Feishu webhooks are authenticated by their own signatures (`BOOM_SLACK_SIGNING_SECRET`, `BOOM_FEISHU_ENCRYPT_KEY`). Not written: native Feishu/Teams/LINE/Matrix/IRC/Zalo clients (those need the bridge's reply_url), and Slack's Events API handshake. `plugins/platforms/*` holds 21 adapters copied from Hermes and **all 21 fail to import** — they need `gateway`, `hermes_cli`, `pm`, `agent.async_utils`, `agent.i18n`, `agent.compression_marker`, `agent.secret_scope`, none of which exist here |

---

## 2. Core Subsystems

### 2.1 State & Episodic Memory (`StateDB`)
- **WAL Mode (`PRAGMA journal_mode = WAL`)**: Guarantees zero read-write contention across async threads.
- **FTS5 Full-Text Search**: Automatically synchronizes inserted messages into an episodic index via SQLite triggers, allowing fast queries across months of conversation history.

### 2.2 System Prompt Assembly
- **Dynamic Context Injection**: Runtime OS detection, Python version, current UTC timestamp.
- **Platform-Specific Rules**:
  - `telegram` / `discord`: Ultra-terse, telegraphic, 1-line preferred.
  - `zalo`: Respectful, polite, formal sentences.
- **Progressive Skill Disclosure**: Injects name and description into the system prompt, loading full documentation only when requested via `view_skill`.

### 2.3 Process Management Ledger
- Tracks long-running background CLI jobs without blocking the main agent loop.
- Records PIDs, start timestamps, command arrays, and execution status to `processes.json`.

---

## 3. Verification Suite

Verification suites validating this architecture (run the Python ones with `py -3`):
```bash
cargo test --all
py -3 evals/test_loaders.py
py -3 evals/test_ipc.py
py -3 evals/test_dag_mass.py
py -3 evals/test_delegation.py
py -3 evals/test_self_supervision.py
py -3 evals/test_mcp.py
py -3 evals/test_web_tools.py
py -3 evals/test_cron.py
py -3 evals/test_checkpoints.py
py -3 evals/test_cjk_search.py
py -3 evals/test_browser_cdp.py
py -3 evals/test_memory_reflexion.py
py -3 evals/test_acp_adapter.py
py -3 evals/test_full_harness.py
py -3 evals/test_gateway_ingress.py
py -3 evals/test_gateway_e2e.py
py -3 evals/test_discord_channel.py
py -3 evals/test_gateway_dispatcher.py
py -3 evals/test_advanced_features.py
py -3 evals/test_backend_processing.py
py -3 evals/test_state_and_prompt.py
py -3 evals/test_parity_deep.py
py -3 evals/test_parity_tools_and_owner_guard.py
py -3 evals/test_production_operations.py
(cd apps/web && pnpm build)
```
`evals/test_live_loop.py` needs a live provider and is not part of the offline suite.
Result: **100% PASS across Rust, Python, and TypeScript**.
