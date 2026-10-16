# Boom — Tools Reference

The complete tool catalog registered in `agent/src/environment.py` (`AgentEnvironment._init_tools()`), covering directly registered tools plus tools loaded from `build_tools()` modules (web, cron, browser, artifacts, goals, people, digest, research, tasks, verify, triggers, OCR, authz, audit, broadcast, the refactored `tools.web` / `tools.code` / `tools.security` / `tools.db` packages, the connector registry, and the plugin registry).

> **Verification source:** counted directly in Python — `AgentEnvironment(root_dir=..., yolo=True).get_tools()` on `main` (commit `f4ed006`).
> **Total: 158 tools.**

| Group | Tool count |
|-------|-----------|
| filesystem | 13 |
| terminal | 13 |
| git | 11 |
| web | 28 |
| code | 11 |
| security | 12 |
| db | 3 |
| legacy & extended | 67 |

---

## 1. filesystem (13)

Safe file operations (safe path resolution, path traversal blocked). The `fs_*` set is the new refactored version; the older set (`read_file`, `write_file`, `patch_file`, `edit_file`, `append_file`, `file_info`) is kept for backward compatibility.

| Tool name | Description |
|-----------|-------------|
| `fs_read` | Read a file; returns numbered lines. Binary files report their type instead of being dumped. Blocks path traversal. |
| `fs_write` | Write new content to a file in full (overwrite). |
| `fs_edit` | Edit a file via exact-match replacement. |
| `fs_append` | Append text to the end of a file. |
| `fs_delete` | Permanently delete a file; not undoable. |
| `fs_list` | List directory contents; supports recursion. |
| `fs_info` | File metadata: size, mtime, line count. |
| `read_file` | (legacy) Read a file; returns numbered lines; supports offset/limit pagination. |
| `write_file` | (legacy) Write new content to a file in full (overwrite). |
| `patch_file` | (legacy) Exact-string replacement in a file — for small edits. |
| `edit_file` | (legacy) Edit a file via exact-match; creates a `.bak` backup; fails on 0 or >1 matches. |
| `append_file` | (legacy) Append text to the end of a file. |
| `file_info` | (legacy) File metadata: size, mtime, line count. |

---

## 2. terminal (13)

Run shell commands, manage background processes, and execute dynamic code.

| Tool name | Description |
|-----------|-------------|
| `shell_run` | Run a shell command with a timeout (default 30s); no shell=True; blocks dangerous commands. |
| `shell_bg` | Run a command in the background; returns a `job_id`. |
| `shell_status` | Check the status of a background job. |
| `shell_kill` | Kill a background job. |
| `shell_forget` | Drop a finished background job from the registry to free memory. |
| `process_start` | Start a long-running background process tracked via session_id; stdout/stderr buffered until polled. |
| `process_poll` | Poll a background process for new stdout/stderr and status. |
| `process_list` | List tracked background processes. |
| `process_kill` | Stop a background process; returns unpolled output. |
| `sandbox_exec` | Run a command via the Rust sandbox core over IPC; returns stdout + exit code. |
| `execute_code` | Run Python code in-process with the tool registry exposed; returns stdout. |
| `fuzzy_patch` | Fault-tolerant find-and-replace text patching: exact → CRLF → Unicode → whitespace-trim → fuzzy ≥ 0.85. |
| `rewind` | Undo this session's file edits: restore modified files, delete created files (dry_run to preview). |

---

## 3. git (11)

6 local git tools + 5 GitHub tools (via token).

| Tool name | Description |
|-----------|-------------|
| `git_status` | Git status --porcelain: staged/unstaged per file. |
| `git_diff` | View git diff: per-file stats + raw diff text (capped); staged=true diffs the index. |
| `git_log` | Recent commit history: hash, author, date, subject. |
| `git_diff_summary` | Diff summary: files changed, +/- lines, functions touched. |
| `git_safe_commit` | Commit staged files only when the message is non-empty and no secret is detected; refuses on protected branches. |
| `git_branch_guard` | Check whether the current branch is protected (main/master) — the agent must create a feature branch. |
| `github_list_issues` | List repo issues (excluding PRs). |
| `github_get_issue` | Read a single issue with its body and first comments. |
| `github_list_prs` | List repo pull requests. |
| `github_read_file` | Read a text file from a repo at a given ref (branch/tag/commit, default main). |
| `github_search_code` | Search code across repos the token can access. |

---

## 4. web (28)

Search, fetch, extract, real headless browser, scraping, news, and weather.

| Tool name | Description |
|-----------|-------------|
| `web_search` | Search the web via DuckDuckGo HTML; returns a list of {title, url, snippet}. |
| `web_fetch` | Fetch a URL, return text; has timeout, retry, circuit breaker. |
| `web_extract` | Extract the main content of a URL, stripping nav/footer/scripts. |
| `web_extract_many` | Fetch multiple URLs in parallel; a failing URL fails only its own slot. |
| `crawl_site` | BFS-crawl a single site (same domain only); polite: delay between fetches, backoff on 429. |
| `read_sitemap` | Parse sitemap.xml into [{url, lastmod}]; supports sitemapindex (recursive, max 5). |
| `browser_navigate` | Open a URL in headless Chrome/Edge; the browser stays open for other browser tools. |
| `browser_click` | Click an element via CSS selector using a real mouse event. |
| `browser_fill` | Type text into an input/textarea, firing input/change events. |
| `browser_eval` | Run JavaScript on the current page; returns the value. |
| `browser_screenshot` | Capture a PNG of the full page or one element via CSS selector. |
| `browser_tabs` | List open tabs and the active tab. |
| `browser_open_tab` | Open a URL in a new tab and switch to it. |
| `browser_use_tab` | Switch to the tab at the given index. |
| `browser_close_tab` | Close the tab at the given index. |
| `browser_close` | Close the browser, its process, and temp profile. |
| `browser_wait_for` | Wait until a CSS selector matches (no guessing durations). |
| `browser_network` | List requests the page has made (method, url, type, status). |
| `scrape_basic` | Scrape a static HTML page structurally: each element matching item_selector becomes one item. |
| `scrape_list` | Crawl a paginated listing page, extracting fields via CSS selectors; JS pages auto-fallback to the headless browser. |
| `scrape_product` | Extract a detail page (name, price, images, description) via a {field: selector} schema. |
| `scrape_table` | Extract an HTML table into [{column: value}] rows. |
| `news_headlines` | Latest news from VnExpress RSS (latest, business, sports) or a custom RSS feed. |
| `news_search` | Search VnExpress news by keyword (case-insensitive title match). |
| `research_brief` | Build a cited research brief [1], [2] from already-gathered sources; fetches nothing new. |
| `watch_url` | Snapshot a page (SHA256) and report changes vs. the previous run — designed for daily cron (e.g. price tracking). |
| `weather_now` | Current weather at coordinates: temperature, humidity, wind (e.g. Hanoi 21.0285, 105.8542). |
| `weather_daily` | Daily forecast at coordinates: max/min temperature and a short description. |

---

## 5. code (11)

Code analysis, review, verification, and agent coordination.

| Tool name | Description |
|-----------|-------------|
| `ast_inspect` | Parse a Python file; list imports/classes/functions without running code. |
| `ast_parse` | Parse a single Python file with AST: returns classes/functions/imports with line numbers. |
| `code_review` | Static review of a Python file: complexity, dead code, bare except, TODO; returns a Markdown report (in Vietnamese). |
| `dep_cycles` | Find circular imports under a directory. |
| `dep_impact` | Find modules affected when a given file changes (reverse-dependency BFS). |
| `verify_code` | Verify Python code for real: run in a subprocess, auto-debug errors with the LLM over a few rounds, then adversarial critique. |
| `verify_text` | Verify text/fact/plan with an adversarial critic + Self-Refine; the 'fact' kind verifies claims via search_fn. |
| `optimize_workflow` | Find a better multi-agent workflow for a repeated goal type; the winning workflow is saved to memories/workflows.json. |
| `delegate_task` | Run a subtask as separate subagents (own transcripts); subtasks with depends_on run after and receive the previous subtask's result. Roles: planner, coder, reviewer, critic, learner. |
| `search_tools` | Find a tool by keyword at runtime (name, description, toolset); returns results with schemas. Use instead of guessing tool names. |
| `list_toolsets` | List all registered toolsets and the tool names in each. |

---

## 6. security (12)

Secret scanning, input sanitization, RBAC authorization, and audit.

| Tool name | Description |
|-----------|-------------|
| `secret_scan` | Scan files/directories for leaked secrets (API keys, tokens, private keys); previews are masked. |
| `sanitize_input` | Sanitize args before a tool call: strip control characters, truncate, detect prompt injection, validate paths against traversal. |
| `check_rate_limit` | Check a token-bucket rate limit for a key; strict=true for dangerous tools. |
| `authz_check` | Check whether a user is authorized; returns allowed=true/false. |
| `authz_add_user` | Add a new user (optional roles, which must exist). |
| `authz_assign_role` | Assign a role to a user. |
| `authz_revoke_role` | Remove a role from a user. |
| `authz_create_role` | Create a custom role (must not clash with builtin names). |
| `authz_list_roles` | List all roles (builtin + custom). |
| `authz_list_users` | List all users with their roles. |
| `audit_get` | Get one audit event by id. |
| `audit_query` | Search the append-only audit log (who did what, when). |

---

## 7. db (3)

Safe SQLite: whitelisted tables only, parameterized queries, no raw SQL.

| Tool name | Description |
|-----------|-------------|
| `db_query` | Read-only SELECT on whitelisted tables; where only allows 'col OP ?' joined by AND/OR. |
| `db_insert` | INSERT one row {column: value} into a whitelisted table; returns rowid. |
| `db_update` | UPDATE a whitelisted table; WHERE is mandatory (guards against full-table updates). |

---

## 8. legacy & extended (67)

Legacy tools (kept for compatibility) and domain-extended tools.

### Skills (3)

| Tool name | Description |
|-----------|-------------|
| `list_skills` | List available Agent Skills with names and descriptions. |
| `view_skill` | Read a skill's full documentation (optionally a specific file inside). |
| `install_skill` | Install a skill by name from the skills hub. |

### Email — Gmail (5)

| Tool name | Description |
|-----------|-------------|
| `gmail_search` | Search Gmail messages; returns a list of id/snippet, no body. |
| `gmail_read` | Read one Gmail message: headers, snippet, plain-text body. |
| `gmail_draft` | Create a LOCAL draft (does not send, no Gmail API call); returns draft_id + preview. |
| `gmail_send` | Send a draft created with gmail_draft. DANGEROUS: sends for real, always requires approval. |
| `gmail_discard` | Discard a local draft without sending. |

### Connectors: calendar, drive, spotify, status (9)

| Tool name | Description |
|-----------|-------------|
| `calendar_list` | List events on the primary calendar between two ISO 8601 timestamps. |
| `calendar_create` | Create an event on the primary calendar (default timezone Asia/Ho_Chi_Minh). |
| `calendar_delete` | Delete an event by id. |
| `drive_list` | List Google Drive files (newest first). |
| `drive_search` | Find Drive files by name. |
| `drive_download` | Download a Drive file's content (text inlined, up to 200k characters). |
| `drive_upload` | Upload text as a new Drive file. |
| `spotify_search` | Search tracks/artists/albums on Spotify. |
| `spotify_now_playing` | What's playing on Spotify; honestly returns {"playing": false} when nothing is. |
| `spotify_play` | Play a track URI on the active Spotify device. |
| `spotify_pause` | Pause Spotify playback. |
| `connector_status` | Which connectors are connected/disconnected + setup hints; config check only, no network calls. |

### Memory & learning (5)

| Tool name | Description |
|-----------|-------------|
| `memory` | Read/write durable memory notes shown in the system prompt. |
| `session_search` | Full-text search old chat sessions in SQLite. |
| `learn_from_conversation` | Extract durable learnings (preference, fact, correction) from a conversation and store them. |
| `recall_learned` | Find stored learnings, ordered by confidence. |
| `forget_learning` | Permanently delete one learning by id. |

### Goals (6)

| Tool name | Description |
|-----------|-------------|
| `goal_add` | Record a new long-term goal (saves to memories/goals.json + timestamped progress log). |
| `goal_get` | Get one goal with its full progress log. |
| `goal_list` | List goals, filtered by status (active/paused/completed). |
| `goal_log` | Append a timestamped progress note to a goal's log. |
| `goal_pause` | Pause a goal without deleting it. |
| `goal_complete` | Mark a goal complete. |

### Tasks (5)

| Tool name | Description |
|-----------|-------------|
| `task_create` | Create a durable task from a goal; the goal is broken into steps via the LLM planner (fallback: 1 step). |
| `task_status` | Get one task with its steps and each step's status/result. |
| `task_list` | List tasks, filtered by status. |
| `task_resume` | Run/continue a task step by step via the brain engine; finished steps are skipped; each step checkpoints to SQLite. |
| `task_cancel` | Mark a task cancelled without deleting it. |

### People (5)

| Tool name | Description |
|-----------|-------------|
| `person_add` | Remember a person: name, relationship, initial notes; if the name is already known (case-insensitive), updates instead. |
| `person_get` | Get a person's record by name. |
| `person_list` | List everyone in the people ledger in the order added. |
| `person_note` | Add a note to an existing person in the ledger. |
| `person_search` | Find people by keyword over names and notes (case-insensitive). |

### Cron & triggers (8)

| Tool name | Description |
|-----------|-------------|
| `cron_add` | Schedule a prompt on a 5-field cron expression (supports @hourly/@daily/...). Jobs are stored in memories/cron_jobs.json; with deliver_to ({channel, target}) the result is pushed to the channel when the job finishes. |
| `cron_list` | List cron jobs: expression, last run, run count, status. |
| `cron_remove` | Delete a cron job by id. |
| `cron_run` | Run a job now through the agent loop, regardless of schedule. |
| `trigger_create` | Create a trigger: a condition checked periodically by the runner, running an action when fired. Kinds: gmail_new, price_below, keyword_news, webhook. |
| `trigger_list` | List triggers, optionally only enabled ones. |
| `trigger_toggle` | Enable/disable a trigger. |
| `trigger_delete` | Delete a trigger by id. |

### Artifacts (4)

| Tool name | Description |
|-----------|-------------|
| `artifact_markdown` | Build a Markdown report file under artifacts/ (filename inferred from the title if not given). |
| `artifact_json` | Build a pretty-printed JSON file under artifacts/. |
| `artifact_csv` | Build a CSV table under artifacts/ from a list of dicts (header = ordered union of keys). |
| `artifact_html` | Build a complete standalone HTML page under artifacts/. |

### OCR (3)

| Tool name | Description |
|-----------|-------------|
| `ocr_image` | Extract text from a local image file with local Tesseract OCR; returns {text, lang, char_count}. |
| `ocr_image_url` | Download an image (max 10MB) and extract text with Tesseract OCR. |
| `ocr_image_queued` | OCR via a bounded worker queue: safe under load (limited concurrent tesseract, repeated-image cache). |

### Vietnam e-commerce (5)

| Tool name | Description |
|-----------|-------------|
| `tiki_search` | Search Tiki products by keyword: id, name, price, url, seller, brand, rating. |
| `tiki_product` | Tiki product details by id/URL: price, rating, stock. |
| `shopee_search` | Search Shopee products by keyword (best-effort: Shopee's anti-bot usually blocks non-browser clients). |
| `shopee_product` | Shopee product details from a URL (best-effort, same anti-bot caveat). |
| `lazada_search` | Search Lazada products by keyword (best-effort: JS-rendered pages so plain fetch often finds no data). |

### Notifications & utilities (7)

| Tool name | Description |
|-----------|-------------|
| `broadcast_send` | Send one text message to many channel targets (rate-limit friendly); dry_run to preview. |
| `broadcast_history` | List sent broadcasts (newest first). |
| `captcha_balance` | Check a 2captcha account balance (needs BOOM_2CAPTCHA_KEY). |
| `captcha_solve_image` | Solve an image CAPTCHA via the paid 2captcha service (polls up to 120s). |
| `digest_preview` | Render a proactive Markdown briefing from sections to preview exactly what the cron job will send. |
| `sample_echo` | Plugin sample tool: echoes input back to the caller. |

---

*Updated 2026-10-16 from `main` (commit `f4ed006` — 139 → 158 tools). Numbers obtained by actually running `AgentEnvironment.get_tools()` — not estimated.*
