# Boom — Kiến trúc hệ thống

Sơ đồ kiến trúc tổng quan của Boom (TheOwlOps/Boom), theo luồng xử lý một message từ khi vào đến khi ra.

## Sơ đồ kiến trúc (text)

```
 ┌──────────────────────────────────────────────────────────────────────┐
 │  LAYER 1 — CHANNELS (gateway/channels/)                              │
 │  telegram · discord · slack · messenger · whatsapp · zalo            │
 │  feishu · line                                                       │
 │  → Adapter: ChannelAdapter + HmacWebhookMixin + RetryMixin           │
 │  → gateway/api.py: REST API (:18080) · gateway/broadcast.py: gửi loạt│
 └──────────────────────────────┬───────────────────────────────────────┘
                                │ message / webhook
 ┌──────────────────────────────▼───────────────────────────────────────┐
 │  LAYER 2 — BRAIN (agent/src/orchestration + loop + reasoning)         │
 │  ┌──────────────────────────────────────────────────────────────┐    │
 │  │ Brain — một đường chạy duy nhất, 3 chiến lược:                │    │
 │  │  • react (mặc định): một vòng ReActLoop                       │    │
 │  │  • dag: planner tách goal → TaskNode → DAGScheduler chạy song │    │
 │  │    song, mỗi node là 1 ReActLoop riêng                        │    │
 │  │  • mass: specialist PLANNER/CODER/REVIEWER/CRITIC/LEARNER chạy │    │
 │  │    song song, hợp nhất bằng labelled join                    │    │
 │  │  Routing auto = heuristic rule-based (không phải AI classifier│    │
 │  └──────────────────────────────────────────────────────────────┘    │
 │  loop/: ReActLoop, prompt_builder, context_compressor, selfheal,      │
 │          trajectory, token_accounting                                │
 │  reasoning/: CoT (cot.py), vote/self-consistency, verify adversarial │
 │  autonomous/: controller plan→execute→verify→debug (tối đa 10 vòng,   │
 │          crash/resume được), surgery (patch exact-match + auto-      │
 │          rollback), debugger (classify + fix cơ học ≤3 vòng),        │
 │          guard (14 pattern destructive)                             │
 └──────────────────────────────┬───────────────────────────────────────┘
                                │ gọi tool (function call)
 ┌──────────────────────────────▼───────────────────────────────────────┐
 │  LAYER 3 — TOOLS (agent/src/environment.py → AgentEnvironment)        │
 │  158 tools, đăng ký qua: trực tiếp + build_tools() của các module    │
 │  (tools.web, cronjob_tool, browser_tool, artifacts, goals, digest,   │
 │   research, scraper, people, tasks, verify, triggers, ocr_tool,      │
 │   authz, audit, broadcast, tools.web/code/security/db refactored)    │
 │  + connector registry (connectors/registry.py) + plugin registry      │
 │  (extensibility/) + MCP servers (mcp.json)                           │
 │  Filesystem · terminal · git · web/browser · code · security · db    │
 │  Chi tiết từng tool: xem TOOLS.md                                    │
 └──────────────────────────────┬───────────────────────────────────────┘
                                │ mọi tool nguy hiểm đi qua cổng này
 ┌──────────────────────────────▼───────────────────────────────────────┐
 │  LAYER 4 — SECURITY                                                  │
 │  security/: guardrail, approval (ApprovalGate — tool như gmail_send  │
 │      vắng mặt trong SAFE_TOOLS nên luôn bị prompt), cmdval,          │
 │      sanitize, secretscan, ratelimit (token bucket), redact          │
 │      (tự động che secret trong log), envguard, toolsandbox           │
 │  authz/: RBAC 3 role (admin/operator/viewer) + wildcard permission   │
 │  audit/: nhật ký append-only + middleware; mọi sự kiện tool đều log  │
 │  lifecycle/: shutdown, log, circuit breaker, ratelimit, validate,    │
 │      timeout                                                         │
 │  Rust sandbox (crates/): spawn argv trực tiếp, cwd confinement       │
 │      symlink-aware, env allowlist, kill process group — sandbox_exec  │
 └──────────────────────────────┬───────────────────────────────────────┘
                                │ đọc/ghi trạng thái
 ┌──────────────────────────────▼───────────────────────────────────────┐
 │  LAYER 5 — MEMORY (agent/src/memory + learning + goals + people)      │
 │  state_db.py: SQLite — session, timeline, checkpoint                 │
 │  longterm.py: long-term memory FTS5 (full-text search)              │
 │  checkpoints.py · curated_memory.py · reflexion.py                   │
 │  learning/: learn_from_conversation / recall_learned / forget        │
 │  goals/ · people/ · tasks/: mục tiêu, người, task bền vững           │
 │  memories/: cron_jobs.json, workflows.json (delegate_task replay)    │
 └──────────────────────────────────────────────────────────────────────┘
```

Ngoài 5 layer chính còn có:

- **Scheduling**: `cron/` + `triggers/` (4 kind: gmail_new, price_below, keyword_news, webhook) — cron wiring chạy mỗi 5 phút với build_ctx thật; job xong có thể tự `deliver_to` ra channel (`cron_add` nhận `deliver_to: {channel, target}`).
- **Skills**: `skills/` (80 skills) + `extensibility/skill_loader.py` (cache, search-ranking, versioning) — xem SKILLS.md.
- **Plugins**: `plugins/` (manifest/lifecycle/sandbox v2) + tool `install_skill`/`list_skills`/`view_skill`.
- **Channels mở rộng**: `gateway/channels/` 8 kênh; broadcast gửi loạt có delay + dry_run.
- **Entry points**: `boom.py` → `agent/src/cli.py` (CLI + wizard `boom setup`), `gateway/api.py` (REST :18080), web UI (`apps/`).
- **Reproducible env**: `devbox.json` (Python 3.12, Node 22, tesseract) là cách chạy khuyến nghị; `Dockerfile` + `docker-compose.yml` cho deploy; `flake.nix` + `nix/` cho Nix.
- **DeepSearch/DeepThink**: `agent/src/research/deepsearch.py` (loop search→fetch→entities→query mới, ≤5 hops), `crossref.py` (đối chiếu claims), `deepthink.py` (phân tích đa chiều, độ tin cậy source) — dùng qua skill `research/*` và tool `research_brief`.
- **Orchestration nặng**: `orchestration/dynamic.py` — spawn/retire agent theo ngữ cảnh (Coder, Data Scientist, Researcher, Visual Analyst, Financial Modeler, Validator).

## Mô tả từng layer

### Layer 1 — Channels (cổng vào)

Mọi tương tác người dùng đi qua `gateway/channels/`: 8 adapter (telegram, discord, slack, messenger, whatsapp, zalo, feishu, line) kế thừa `ChannelAdapter` chung với `HmacWebhookMixin` (verify webhook signature) và `RetryMixin`. Ngoài webhook, `gateway/api.py` phục vụ REST API và `gateway/broadcast.py` gửi tin nhắn hàng loạt (có delay chống rate-limit, hỗ trợ `dry_run`).

### Layer 2 — Brain (bộ não)

`agent/src/orchestration/brain.py` hợp nhất 3 mảnh rời rạc trước đây thành một đường chạy: **ReActLoop** (vòng thought→tool→observe, `loop/react.py`) là mặc định; **DAGScheduler** (`orchestration/dag.py`) chạy các TaskNode như ReActLoop riêng có phụ thuộc; **MASSSwarm** (`orchestration/mass.py`) fan-out 5 specialist (PLANNER/CODER/REVIEWER/CRITIC/LEARNER) song song rồi hợp nhất. Routing chọn chiến lược bằng heuristic rule-based (explicit strategy luôn thắng; dependency keywords → DAG; list nhiều item → MASS). Brain không sở hữu provider, không gọi mạng: mọi model call đi qua factory `make_loop` do `cli.py` đóng lại. `verify=True` chạy thêm module `verify` (adversarial critic + Self-Refine) trên kết quả — thiếu module thì degrade trung thực, không giả verify.

Xung quanh brain: `loop/selfheal.py` (self-healing loop), `context_compressor.py` (quản lý context window), `reasoning/` (CoT, self-consistency/vote, verify), và `autonomous/` — controller plan→execute→verify→debug tối đa 10 vòng (log task để crash/resume), surgery patch exact-match có backup + validate syntax + auto-rollback, guard chặn 14 pattern destructive.

### Layer 3 — Tools (tay chân)

`AgentEnvironment._init_tools()` (`agent/src/environment.py`) đăng ký **158 tools**: một phần đăng ký trực tiếp, phần còn lại qua factory `build_tools()` của các module (web, cron, browser, artifacts, goals, people, digest, research, tasks, verify, triggers, OCR, authz, audit, broadcast, 4 package refactored `tools.web`/`tools.code`/`tools.security`/`tools.db`), cộng connector registry (Gmail/Calendar/Drive/Spotify...), plugin registry và MCP servers. Tool tìm tool: `search_tools` (tìm theo keyword, kèm schema), `list_toolsets`. Chi tiết đầy đủ: xem TOOLS.md.

### Layer 4 — Security (cổng kiểm soát)

Mọi tool nguy hiểm phải qua nhiều lớp: `security/guardrail.py` + `approval.py` (ApprovalGate — `gmail_send` vắng mặt trong SAFE_TOOLS nên luôn bị prompt trước khi gửi thật; Telegram approval qua nút Đồng ý/Từ chối, SQLite store), `cmdval.py` (validate lệnh), `sanitize.py` (phát hiện prompt injection, validate path), `secretscan.py`, `ratelimit.py` (token bucket, `check_rate_limit`), `redact.py` (tự động che secret trong AuditStore.log), `toolsandbox.py`. `authz/` là RBAC 3 role (admin/operator/viewer) với wildcard permission; `audit/` là nhật ký append-only có middleware; `lifecycle/` lo shutdown/circuit/timeout/validate. Lớp cứng nhất: Rust sandbox (`crates/`) — spawn argv trực tiếp, cwd confinement symlink-aware, env allowlist, kill cả process group — được gọi qua tool `sandbox_exec`.

### Layer 5 — Memory (trí nhớ)

`agent/src/memory/`: `state_db.py` (SQLite — session, message, timeline event, checkpoint), `longterm.py` (long-term memory FTS5 tìm full-text), `checkpoints.py`, `curated_memory.py` (curation nền tách khỏi loop), `reflexion.py`. Vòng học: `learning/` (`learn_from_conversation` trích preference/fact/correction bền vững, `recall_learned`, `forget_learning`). `goals/` (mục tiêu dài hạn + progress log), `people/` (ledger người quen), `tasks/` (task bền vững, step checkpoint vào SQLite), `memories/` (cron_jobs.json, workflows.json cho `delegate_task` replay, session search FTS).

---

*Cập nhật 2026-10-16 từ `main` (commit `f4ed006`). Số liệu tools/skills lấy bằng cách chạy/quét code thật — không ước lượng.*
