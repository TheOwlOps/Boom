# Boom — System Architecture

A high-level view of the Boom architecture (TheOwlOps/Boom), following one message from entry to exit.

## Architecture diagram (text)

```text
 ┌──────────────────────────────────────────────────────────────────────┐
 │  LAYER 1 — CHANNELS (gateway/channels/)                              │
 │  telegram · discord · slack · messenger · whatsapp · zalo            │
 │  feishu · line                                                       │
 │  → Adapter: ChannelAdapter + HmacWebhookMixin + RetryMixin           │
 │  → gateway/api.py: REST API (:18080) · gateway/broadcast.py: bulk send│
 └──────────────────────────────┬───────────────────────────────────────┘
                                │ message / webhook
 ┌──────────────────────────────▼───────────────────────────────────────┐
 │  LAYER 2 — BRAIN (agent/src/orchestration + loop + reasoning)         │
 │  ┌──────────────────────────────────────────────────────────────┐    │
 │  │ Brain — one execution path, three strategies:                 │    │
 │  │  • react (default): a single ReActLoop                        │    │
 │  │  • dag: planner splits a goal → TaskNodes → DAGScheduler runs │    │
 │  │    them in parallel, each node is its own ReActLoop           │    │
 │  │  • mass: specialist PLANNER/CODER/REVIEWER/CRITIC/LEARNER run  │    │
 │  │    in parallel, merged via labelled join                     │    │
 │  │  Routing auto = heuristic rule-based (not an AI classifier)   │    │
 │  └──────────────────────────────────────────────────────────────┘    │
 │  loop/: ReActLoop, prompt_builder, context_compressor, selfheal,      │
 │          trajectory, token_accounting                                │
 │  reasoning/: CoT (cot.py), vote/self-consistency, adversarial verify │
 │  autonomous/: controller plan→execute→verify→debug (max 10 rounds,   │
 │          crash-resumable), surgery (exact-match patch + auto-       │
 │          rollback), debugger (classify + mechanical fix ≤3 rounds),  │
 │          guard (14 destructive patterns)                            │
 └──────────────────────────────┬───────────────────────────────────────┘
                                │ tool calls (function calls)
 ┌──────────────────────────────▼───────────────────────────────────────┐
 │  LAYER 3 — TOOLS (agent/src/environment.py → AgentEnvironment)        │
 │  158 tools, registered via: direct + build_tools() of each module   │
 │  (tools.web, cronjob_tool, browser_tool, artifacts, goals, digest,  │
 │   research, scraper, people, tasks, verify, triggers, ocr_tool,      │
 │   authz, audit, broadcast, tools.web/code/security/db refactored)   │
 │  + connector registry (connectors/registry.py) + plugin registry      │
 │  (extensibility/) + MCP servers (mcp.json)                           │
 │  Filesystem · terminal · git · web/browser · code · security · db    │
 │  Per-tool detail: see TOOLS.md                                       │
 └──────────────────────────────┬───────────────────────────────────────┘
                                │ every dangerous tool goes through this gate
 ┌──────────────────────────────▼───────────────────────────────────────┐
 │  LAYER 4 — SECURITY                                                  │
 │  security/: guardrail, approval (ApprovalGate — tools like gmail_send│
 │      absent from SAFE_TOOLS are always prompted), cmdval,            │
 │      sanitize, secretscan, ratelimit (token bucket), redact          │
 │      (auto-redacts secrets in logs), envguard, toolsandbox          │
 │  authz/: RBAC with 3 roles (admin/operator/viewer) + wildcard        │
 │      permissions                                                     │
 │  audit/: append-only journal + middleware; every tool event is logged │
 │  lifecycle/: shutdown, log, circuit breaker, ratelimit, validate,    │
 │      timeout                                                         │
 │  Rust sandbox (crates/): spawn argv directly, symlink-aware cwd       │
 │      confinement, env allowlist, kill process group — via sandbox_exec│
 └──────────────────────────────┬───────────────────────────────────────┘
                                │ read/write state
 ┌──────────────────────────────▼───────────────────────────────────────┐
 │  LAYER 5 — MEMORY (agent/src/memory + learning + goals + people)      │
 │  state_db.py: SQLite — session, timeline, checkpoint                 │
 │  longterm.py: long-term FTS5 memory (full-text search)              │
 │  checkpoints.py · curated_memory.py · reflexion.py                   │
 │  learning/: learn_from_conversation / recall_learned / forget        │
 │  goals/ · people/ · tasks/: goals, people, durable tasks             │
 │  memories/: cron_jobs.json, workflows.json (delegate_task replay)    │
 └──────────────────────────────────────────────────────────────────────┘
```

Beyond the 5 main layers:

- **Scheduling**: `cron/` + `triggers/` (4 kinds: gmail_new, price_below, keyword_news, webhook) — cron wiring runs every 5 minutes with a real build_ctx; finished jobs can self-`deliver_to` a channel (`cron_add` accepts `deliver_to: {channel, target}`).
- **Skills**: `skills/` (80 skills) + `extensibility/skill_loader.py` (cache, search-ranking, versioning) — see SKILLS.md.
- **Plugins**: `plugins/` (manifest/lifecycle/sandbox v2) + `install_skill`/`list_skills`/`view_skill` tools.
- **Extended channels**: `gateway/channels/` for 8 channels; broadcast sends bulk messages with delay + dry_run.
- **Entry points**: `boom.py` → `agent/src/cli.py` (CLI + `boom setup` wizard), `gateway/api.py` (REST :18080), desktop app (`apps/desktop/`).
- **Reproducible env**: `devbox.json` (Python 3.12, Node 22, tesseract) is the recommended way to run; `Dockerfile` + `docker-compose.yml` for deploy; `flake.nix` + `nix/` for Nix.
- **DeepSearch/DeepThink**: `agent/src/research/deepsearch.py` (search→fetch→entities→new-query loop, ≤5 hops), `crossref.py` (claim cross-checking), `deepthink.py` (multi-perspective analysis, source reliability) — used via the `research/*` skills and the `research_brief` tool.
- **Heavy orchestration**: `orchestration/dynamic.py` — spawn/retire agents by context (Coder, Data Scientist, Researcher, Visual Analyst, Financial Modeler, Validator).

## Per-layer detail

### Layer 1 — Channels (entry points)

All user interaction flows through `gateway/channels/`: 8 adapters (telegram, discord, slack, messenger, whatsapp, zalo, feishu, line) inheriting a shared `ChannelAdapter` with `HmacWebhookMixin` (webhook signature verification) and `RetryMixin`. Besides webhooks, `gateway/api.py` serves the REST API and `gateway/broadcast.py` sends bulk messages (with rate-limit-friendly delays and `dry_run` support).

### Layer 2 — Brain

`agent/src/orchestration/brain.py` unifies three previously separate pieces into one execution path: **ReActLoop** (thought→tool→observe loop, `loop/react.py`) is the default; **DAGScheduler** (`orchestration/dag.py`) runs TaskNodes as dependent ReActLoops; **MASSSwarm** (`orchestration/mass.py`) fans out to 5 specialists (PLANNER/CODER/REVIEWER/CRITIC/LEARNER) in parallel and merges. Strategy routing is heuristic rule-based (explicit strategy always wins; dependency keywords → DAG; lists of multiple items → MASS). The Brain owns no provider and makes no network calls: all model calls go through the `make_loop` factory closed over by `cli.py`. `verify=True` additionally runs the `verify` module (adversarial critic + Self-Refine) on the result — if the module is missing it degrades honestly instead of faking verification.

Around the brain: `loop/selfheal.py` (self-healing loop), `context_compressor.py` (context window management), `reasoning/` (CoT, self-consistency/vote, verify), and `autonomous/` — a plan→execute→verify→debug controller (max 10 rounds, task logs for crash/resume), surgery (exact-match patching with backup + syntax validation + auto-rollback), debugger (classify + mechanical fix, ≤3 rounds), and guard (blocks 14 destructive patterns).

### Layer 3 — Tools

`AgentEnvironment._init_tools()` (`agent/src/environment.py`) registers **158 tools**: some registered directly, the rest via each module's `build_tools()` factory (web, cron, browser, artifacts, goals, people, digest, research, tasks, verify, triggers, OCR, authz, audit, broadcast, the 4 refactored `tools.web`/`tools.code`/`tools.security`/`tools.db` packages), plus the connector registry (Gmail/Calendar/Drive/Spotify...), the plugin registry, and MCP servers. Tool discovery tools: `search_tools` (keyword search with schemas), `list_toolsets`. Full detail: see TOOLS.md.

### Layer 4 — Security (control gate)

Every dangerous tool passes multiple gates: `security/guardrail.py` + `approval.py` (ApprovalGate — `gmail_send` is absent from SAFE_TOOLS so it's always prompted before sending for real; Telegram approval via Approve/Reject buttons, SQLite store), `cmdval.py` (command validation), `sanitize.py` (prompt-injection detection, path validation), `secretscan.py`, `ratelimit.py` (token bucket, `check_rate_limit`), `redact.py` (auto-redacts secrets in AuditStore.log), `toolsandbox.py`. `authz/` is RBAC with 3 roles (admin/operator/viewer) and wildcard permissions; `audit/` is an append-only journal with middleware; `lifecycle/` handles shutdown/circuit/timeout/validate. The hardest layer: the Rust sandbox (`crates/`) — spawns argv directly, symlink-aware cwd confinement, env allowlist, kills the whole process group — reached via the `sandbox_exec` tool.

### Layer 5 — Memory

`agent/src/memory/`: `state_db.py` (SQLite — session, message, timeline event, checkpoint), `longterm.py` (FTS5 long-term memory with full-text search), `checkpoints.py`, `curated_memory.py` (background curation kept out of the loop), `reflexion.py`. The learning loop: `learning/` (`learn_from_conversation` extracts durable preferences/facts/corrections, `recall_learned`, `forget_learning`). `goals/` (long-term goals + progress logs), `people/` (the people ledger), `tasks/` (durable tasks, steps checkpointed to SQLite), `memories/` (cron_jobs.json, workflows.json for `delegate_task` replay, FTS session search).

---

*Updated 2026-10-16 from `main` (commit `f4ed006`). Tool/skill counts taken by actually running/scanning the code — not estimated.*
