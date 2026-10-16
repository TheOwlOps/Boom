# Boom — Skills Catalog

Boom's agent skills live in `skills/` — each skill is a directory containing a `SKILL.md` (frontmatter `description` + detailed instructions). The agent discovers skills via the `list_skills` tool, reads details with `view_skill`, and installs more with `install_skill`.

> **Source of truth:** scanned `skills/**/SKILL.md` on `main`.
> **Total: 40 skills** (one `SKILL.md` file = one skill) across 10 top-level skill packs.
> Archived skills live in `skills-archived/` and can be restored with `git mv`.

| Group | Skills |
|-------|--------|
| Software development | 14 |
| Code quality | 2 |
| Git workflow | 1 |
| Web | 1 |
| Research | 4 |
| Email | 2 |
| Media | 3 |
| Productivity | 14 |
| Security | 1 |

---

## 1. Software development (14)

| Skill | Description |
|-------|-------------|
| `software-development/boom-agent-skill-authoring` | Author in-repo SKILL.md files: frontmatter and structure. |
| `software-development/codebase-inspection` | Inspect codebases w/ pygount: LOC, languages, ratios. |
| `software-development/dogfood` | Exploratory QA of web apps: find bugs, evidence, reports. |
| `software-development/github` | GitHub via gh CLI: PRs, issues, reviews, repos, auth. |
| `software-development/inspecting-boom-desktop-dom` | Read the live Boom desktop DOM/CSS over CDP. |
| `software-development/node-inspect-debugger` | Debug Node.js via --inspect + Chrome DevTools Protocol CLI. |
| `software-development/python-debugpy` | Debug Python: pdb REPL + debugpy remote (DAP). |
| `software-development/requesting-code-review` | Pre-commit review: security scan, quality gates, auto-fix. |
| `software-development/simplify-code` | Parallel 4-agent cleanup of recent code changes. |
| `software-development/spike` | Throwaway experiments to validate an idea before build. |
| `software-development/systematic-debugging` | 4-phase root cause debugging: understand bugs before fixing. |
| `software-development/test-driven-development` | TDD: enforce RED-GREEN-REFACTOR, tests before code. |
| `code-review` | Statically review Python code: complexity, dead code, bare except, print, TODO. Wraps Boom's codereview.py. |
| `test-writer` | Generate pytest tests from AST: analyze functions, infer sample values, generate happy path + edge cases (None, empty, boundary). |

---

## 2. Git workflow (1)

| Skill | Description |
|-------|-------------|
| `git-workflow` | Git workflow helper: smart_commit() auto-generates commit messages from diffs, branch_status() shows branch state, pr_checklist() checks before creating a PR. |

---

## 3. Web (1)

| Skill | Description |
|-------|-------------|
| `web/blocked-page-recovery` | Use when a fetch fails: 403/429, paywall, WAF, bot wall. |

---

## 4. Research (4)

| Skill | Description |
|-------|-------------|
| `research/arxiv` | Search arXiv papers by keyword, author, category, or ID. |
| `research/competitor-news-monitor` | Watch named companies for material news; cited digests. |
| `research/grounded-citations` | Ground answers and documents in cited, verifiable sources. |
| `research/llm-wiki` | Karpathy's LLM Wiki: build/query interlinked markdown KB. |

---

## 5. Email (2)

| Skill | Description |
|-------|-------------|
| `email/email-inbox-triage` | Triage an inbox: prioritize threads, draft replies safely. |
| `email/himalaya` | Himalaya CLI: IMAP/SMTP email from terminal. |

---

## 6. Media (3)

| Skill | Description |
|-------|-------------|
| `media/gif-search` | Search/download GIFs from Tenor via curl + jq. |
| `media/songsee` | Audio spectrograms/features (mel, chroma, MFCC) via CLI. |
| `media/youtube-content` | YouTube transcripts to summaries, threads, blogs. |

---

## 7. Productivity (14)

| Skill | Description |
|-------|-------------|
| `productivity/airtable` | Airtable REST API via curl. Records CRUD, filters, upserts. |
| `productivity/box` | Box manages cloud files, sharing, search, and metadata. |
| `productivity/document-to-action-items` | Extract cited obligations, deadlines, tasks from documents. |
| `productivity/docx` | Create, read, edit, template, and review Word .docx files. |
| `productivity/google-workspace` | Gmail, Calendar, Drive, Docs, Sheets via gws CLI or Python. |
| `productivity/maps` | Geocode, POIs, routes, timezones via OpenStreetMap/OSRM. |
| `productivity/meeting-action-items` | Turn meeting notes into cited decisions, owners, tickets. |
| `productivity/notion` | Notion API + ntn CLI: pages, databases, markdown, Workers. |
| `productivity/pdf` | PDF files: create, read, merge, fill, OCR, edit text. |
| `productivity/powerpoint` | Create, read, edit .pptx decks with python-pptx. |
| `productivity/product-price-monitor` | Watch product, flight, or listing prices; alert on target. |
| `productivity/teams-meeting-pipeline` | Teams meeting summaries, job replay, Graph subscriptions. |
| `productivity/weekly-review-planning` | Weekly reset: commitments, stalled work, next-week plan. |
| `productivity/xlsx` | Create, read, edit Excel .xlsx workbooks and CSVs. |

---

## 8. Security (1)

| Skill | Description |
|-------|-------------|
| `security-audit` | Security audit of Python code: hardcoded secrets, dangerous functions (eval/exec/os.system), SQL injection, missing input validation. |

---

## Archived skills

22 skill packs moved to `skills-archived/` (restorable with `git mv skills-archived/<name> skills/<name>`):

`api-designer`, `apple`, `ast-refactor`, `autonomous-ai-agents`, `backup-manager`, `creative`, `cron-manager`, `data-validator`, `db-helper`, `deployer`, `devops`, `doc-researcher`, `env-setup`, `feature-watch`, `index-cache`, `log-analyzer`, `note-taking`, `notify-hub`, `perf-optimizer`, `rea`, `refactor-assistant`, `social-media`.

---

*Updated 2026-10-17 from `main`. Figures scanned from `skills/**/SKILL.md` — not estimated. Descriptions taken from each SKILL.md's frontmatter `description:` field.*
