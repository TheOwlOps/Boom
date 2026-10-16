# Boom — Skills Catalog

Boom's agent skills live in `skills/` — each skill is a directory containing a `SKILL.md` (frontmatter `description` + detailed instructions). The agent discovers skills via the `list_skills` tool, reads details with `view_skill`, and installs more with `install_skill`.

> **Source of truth:** scanned `skills/**/SKILL.md` on `main` (commit `f4ed006`).
> **Total: 80 skills** (one `SKILL.md` file = one skill). `skills/index-cache` has no `SKILL.md` yet, so it's excluded.

| Group | Skills |
|-------|--------|
| Programming & code quality | 17 |
| Reverse engineering (REA) | 4 |
| DevOps, deploy & operations | 7 |
| Scheduling & notifications | 2 |
| Security & data | 3 |
| Research & information | 6 |
| Office documents | 9 |
| Service integrations | 13 |
| Price tracking | 1 |
| Creative & media | 12 |
| Autonomous agents | 5 |
| Web recovery | 1 |

---

## 1. Programming & code quality (17)

| Skill | Description |
|-------|-------------|
| `ast-refactor` | Refactor code with AST and structural pattern matching. |
| `code-review` | Static Python code review: complexity, dead code, bare except, print, TODO (wraps Boom's `codereview.py`). |
| `refactor-assistant` | Safe refactor assistant: rename symbol, extract function, move symbol with real AST, always dry-run preview diff before applying. |
| `test-writer` | Generate pytest tests from AST: analyze functions, infer sample values, generate happy path + edge cases (None, empty, boundary). |
| `api-designer` | Design REST APIs from a description: generate OpenAPI 3.0 spec (paths, schemas, errors) + FastAPI/Flask route stubs, with REST-standard validation. |
| `software-development/codebase-inspection` | Inspect codebase with pygount: LOC, languages, ratios. |
| `software-development/dogfood` | Exploratory web app QA: find bugs, collect evidence, write reports. |
| `software-development/github` | GitHub via `gh` CLI: PRs, issues, reviews, repos, auth. |
| `software-development/boom-agent-skill-authoring` | Write SKILL.md in a repo: standard frontmatter and structure. |
| `software-development/inspecting-boom-desktop-dom` | Read live Boom desktop DOM/CSS via CDP. |
| `software-development/node-inspect-debugger` | Debug Node.js via `--inspect` + Chrome DevTools Protocol CLI. |
| `software-development/python-debugpy` | Debug Python: pdb REPL + debugpy remote (DAP). |
| `software-development/requesting-code-review` | Pre-commit review: security scan, quality gates, auto-fix. |
| `software-development/simplify-code` | Clean up recently changed code with 4 parallel agents. |
| `software-development/spike` | Throwaway experiments to validate ideas before building. |
| `software-development/systematic-debugging` | Root-cause debugging in 4 phases: understand the bug before fixing. |
| `software-development/test-driven-development` | TDD: enforce RED-GREEN-REFACTOR, tests before code. |

---

## 2. Reverse engineering — REA (4)

Skill set from `morluto/rea` (commit `3135db3`), for artifact investigation.

| Skill | Description |
|-------|-------------|
| `rea/reverse-engineer-anything` | Reverse engineer native, managed, Electron/JS, packaged, firmware, and browser targets with REA; explain features, compare versions, decompile, or guide reconstruction. |
| `rea/rea-tool-design` | Design/change REA investigation tools, CLI/MCP contracts, provider capabilities, and Evidence semantics. |
| `rea/rea-ci-tests-audit` | Audit REA's CI and test suites: correctness, fixture drift, false passes, measurable performance. |
| `rea/rea-changelog-update` | Prepare/rewrite REA release changelogs and GitHub release notes from git history, with verified contributor thanks. |

---

## 3. DevOps, deploy & operations (7)

| Skill | Description |
|-------|-------------|
| `devops/sdlc-review` | Review Kanban handoffs and route verified outcomes. |
| `deployer` | Automated pre-deploy checklist: tests pass, no secrets, version bumped, CHANGELOG, env vars, backup plan. |
| `env-setup` | Check dev environment: Python version, installed packages, required env vars, free ports. Standard output [{check, status, fix_command}]. |
| `git-workflow` | Git workflow helper: `smart_commit()` auto-generates commit messages from diffs, `branch_status()` shows branch state, `pr_checklist()` checks before opening a PR. |
| `log-analyzer` | Analyze log files, count errors, find slow patterns. |
| `backup-manager` | Create, list, restore, and clean tar.gz backups. |
| `perf-optimizer` | Analyze performance bottlenecks with AST: nested O(n²) loops, I/O in loops, += string concat, N+1 queries, missing cache. |

---

## 4. Scheduling & notifications (2)

| Skill | Description |
|-------|-------------|
| `cron-manager` | Manage cron jobs: add/list/remove (JSON storage) + validate cron expressions, supports @daily/@hourly and 5-field format. |
| `notify-hub` | Send multi-channel notifications via webhook URLs from env. |

---

## 5. Security & data (3)

| Skill | Description |
|-------|-------------|
| `security-audit` | Audit Python code security: hardcoded secrets, dangerous functions (eval/exec/os.system), SQL injection, missing input validation. |
| `data-validator` | Validate JSON/CSV against schema, types, and formats. |
| `db-helper` | Safe SQLite wrapper: query/insert/migrate with table whitelist, parameterized queries, blocks DROP and WHERE-less DELETE/UPDATE. |

---

## 6. Research & information (6)

| Skill | Description |
|-------|-------------|
| `research/arxiv` | Search arXiv papers by keyword, author, category, or ID. |
| `research/competitor-news-monitor` | Track important news from specified companies; digest with citations. |
| `research/grounded-citations` | Ground answers and documents in verifiable cited sources. |
| `research/llm-wiki` | Build/query linked markdown knowledge bases (Karpathy's LLM Wiki style). |
| `doc-researcher` | Plan research for a topic: questions to answer, sources to check, markdown outline template for the agent to fill in. |
| `feature-watch` | Watch the AI agent ecosystem, report new features/releases with citations. |

---

## 7. Office documents (9)

| Skill | Description |
|-------|-------------|
| `productivity/docx` | Create, read, edit, template, and review Word .docx files. |
| `productivity/pdf` | PDF files: create, read, merge, fill forms, OCR, edit text. |
| `productivity/xlsx` | Create, read, edit Excel .xlsx workbooks and CSV. |
| `productivity/powerpoint` | Create, read, edit .pptx decks with python-pptx. |
| `productivity/notion` | Notion API + `ntn` CLI: pages, databases, markdown, Workers. |
| `productivity/meeting-action-items` | Turn meeting notes into decisions, owners, and cited tickets. |
| `productivity/document-to-action-items` | Extract obligations, deadlines, and cited tasks from documents. |
| `productivity/weekly-review-planning` | Weekly reset: commitments, blockers, next week's plan. |
| `note-taking/obsidian` | Read, search, create, and edit notes in an Obsidian vault. |

---

## 8. Service integrations (13)

| Skill | Description |
|-------|-------------|
| `productivity/airtable` | Airtable REST API via curl: records CRUD, filter, upsert. |
| `productivity/box` | Box: cloud file management, sharing, search, metadata. |
| `productivity/google-workspace` | Gmail, Calendar, Drive, Docs, Sheets via `gws` CLI or Python. |
| `productivity/maps` | Geocode, POI, routes, timezones via OpenStreetMap/OSRM. |
| `productivity/teams-meeting-pipeline` | Summarize Teams meetings, job replay, Graph subscriptions. |
| `email/email-inbox-triage` | Triage inbox: prioritize threads, draft safe replies. |
| `email/himalaya` | Email IMAP/SMTP from terminal via Himalaya CLI. |
| `social-media/xurl` | X/Twitter via `xurl` CLI: search posts, publish, DMs, media. |
| `media/youtube-content` | Turn YouTube transcripts into summaries, threads, blogs. |
| `apple/apple-notes` | Manage Apple Notes via `memo` CLI: create, search, edit. |
| `apple/apple-reminders` | Apple Reminders via `remindctl`: add, list, complete. |
| `apple/findmy` | Track Apple devices/AirTags via FindMy.app on macOS. |
| `apple/imessage` | Send/receive iMessage/SMS via `imsg` CLI on macOS. |

---

## 9. Price tracking (1)

| Skill | Description |
|-------|-------------|
| `productivity/product-price-monitor` | Track product, flight, and listing prices; alert on target hit. |

---

## 10. Creative & media (12)

| Skill | Description |
|-------|-------------|
| `creative/architecture-diagram` | Draw architecture/cloud/infra diagrams as dark-theme SVG exported to HTML. |
| `creative/ascii-video` | ASCII video: convert video/audio to colored ASCII MP4/GIF. |
| `creative/baoyu-infographic` | Infographics: 21 layouts × 21 styles. |
| `creative/claude-design` | One-shot HTML artifact design (landing, deck, prototype). |
| `creative/design-md` | Author/validate/export Google DESIGN.md token spec files. |
| `creative/humanizer` | Humanize text: remove AI-isms, add a real voice. |
| `creative/manim-video` | Manim CE animations: 3Blue1Brown-style math/algorithm videos. |
| `creative/p5js` | p5.js sketches: generative art, shaders, interactive, 3D. |
| `creative/popular-web-designs` | 54 real design systems (Stripe, Linear, Vercel) as HTML/CSS. |
| `creative/songwriting-and-ai-music` | Songwriting techniques and Suno AI music prompts. |
| `media/gif-search` | Find/download GIFs from Tenor via curl + jq. |
| `media/songsee` | Audio spectrogram/features (mel, chroma, MFCC) via CLI. |

---

## 11. Autonomous agents (5)

Delegate coding to other coding-agent CLIs.

| Skill | Description |
|-------|-------------|
| `autonomous-ai-agents/claude-code` | Delegate coding to Claude Code CLI (features, PRs). |
| `autonomous-ai-agents/codex` | Delegate coding to OpenAI Codex CLI (features, PRs). |
| `autonomous-ai-agents/opencode` | Delegate coding to OpenCode CLI (features, PR reviews). |
| `autonomous-ai-agents/boom-agent` | Use, configure, theme, extend, and orchestrate Boom Agent. |
| `autonomous-ai-agents/computer-use` | Background-first desktop control; escalate on signal. |

---

## 12. Web recovery (1)

| Skill | Description |
|-------|-------------|
| `web/blocked-page-recovery` | Use when fetching fails: 403/429, paywalls, WAFs, bot walls. |

---

*Updated 2026-10-16 from `main`. Figures scanned from `skills/**/SKILL.md` — not estimated. Descriptions taken from each SKILL.md's frontmatter `description:` field.*
