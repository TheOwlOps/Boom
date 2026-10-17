# CHANGELOG — Overnight session 2026-10-16

**Time:** 01:36 → ~07:00 (+07), 2026-10-16
**Repo:** TheOwlOps/Boom, branch `main`
**Result:** 26 commits, **~1,400+ assertions PASS**, 0 fail.

---

## Batch 0 — Coding hardening + security (01:36)

**Commit `543a7bf`** — 8 new modules, 286 tests PASS.

**CODING** → `agent/src/autonomous/`:
| Module | Tests | Functionality |
|---|---|---|
| `codereview.py` | 34 | Static review: complexity, dead code, bare except, TODO |
| `depgraph.py` | 29 | Import graph, circular detection, impact analysis |
| `gitops.py` | 40 | Diff summary, branch guard, safe commit (secret scan) |
| `coverage.py` | 19 | Coverage via sys.monitoring (stdlib, Python 3.12+) |

**SECURITY** → `agent/src/security/`:
| Module | Tests | Functionality |
|---|---|---|
| `secretscan.py` | 43 | Scan for 8 secret types, 80%-masked preview |
| `sanitize.py` | 49 | Anti prompt injection (Vietnamese+English), path traversal |
| `ratelimit.py` | 43 | Per-user token bucket, thread-safe |
| `redact.py` | 29 | Redact secrets in audit log (fail-open) |

**Wiring:** 8 new tools into `environment.py` → **131 → 139 tools**. `redact` hooked into `AuditStore.log()`.

---

## Batch 1 — Tools/Skills/Security/UI/Mobile (01:45 → 02:35)

19 commits (`02d4189` → `bf171b9`), ~800+ tests PASS.

### Tools — reorganized into 7 packages
| Commit | Contents | Tests |
|---|---|---|
| `72ca054` | `tools/filesystem/`, `tools/terminal/`, `tools/git/` | 91 |
| `6b63299` | `tools/web/`, `tools/code/`, `tools/security/`, `tools/db/` | 75 |
| `f4ed006` | Wire 7 packages into environment | 93 → **139 → 158 tools** |
| `b7ccfad` | Wire `deep_search`/`deep_think` | **→ 160 tools** |

Each package has SSRF / SQLi / path-traversal protection.

### Skills — 16 new (80+ total)
| Commit | Skills |
|---|---|
| `02d4189` | deployer, doc-researcher, refactor-assistant, api-designer (38 tests) |
| `3feac82` | code-review, test-writer, security-audit, perf-optimizer (30 tests) |
| `54a47bc` | db-helper, cron-manager, git-workflow, env-setup (42 tests) |
| `afb484b` | log-analyzer, backup-manager, notify-hub, data-validator (25 tests) |

### Security — 10 modules
`secretscan`, `sanitize`, `ratelimit`, `redact` (batch 0) + `a85ee9d` command validator + env protector (45 tests) + `c8faae6` tool execution sandbox + audit (46 tests).

### Harness core
| Commit | Contents | Tests |
|---|---|---|
| `d6f33f4` | Context window management | 34 |
| `90e659a` | Self-healing agent loop | 26 |
| `bf171b9` | RepoAwarePlanner v2 | 16 |
| `ccfd154` | Performance utils (TTLCache, LazyLoader, profiler) | 31 |

### UI / Mobile / Docs
| Commit | Contents |
|---|---|
| `5ba47cd` | Dashboard UI: StatusBar, ToolManager, SkillList, TerminalLog (build PASS) |
| `5a0820c` | Mobile PWA + API enhancements (`/api/logs`, health stats) |
| `c1e4f8c` | OpenAPI spec + API docs (9 endpoints) |
| `b7ccfad` | TOOLS.md (158), SKILLS.md (80), ARCHITECTURE.md |

### Real bugs fixed
- `a2d1a20`: `build_environment()` doesn't exist in `api.py` → use `AgentEnvironment()`.

### "Conversation with Boom"
Used Boom's own modules (planner, reasoning) for self-evaluation — details in `docs/SELF_REVIEW.md`.

---

## Batch 2 — Redo after VM restart (fix leftovers + finalize)

6 local commits (pending push), ~580 assertions PASS.

| Commit | Contents | Tests |
|---|---|---|
| `3192f99` | `gateway/bind_config.py` — bind 0.0.0.0 + full CORS for mobile PWA on LAN (found: `api.py:487` hard-binds 127.0.0.1) | 43 |
| `353a307` | Split gateway router → `gateway/routes/` (health, chat, tools, channels, admin, websocket) — 9 real endpoints, backward-compat | 214 |
| `0b348c9` | Full-pipeline integration test: env init, 159 tools, real tool calls — **no bugs found** | 39 |
| `902a107` | `benchmarks/bench_tools.py` — 17 benchmarks (import ~300ms/pkg, `_init_tools` 0.5ms, pure tool calls ~µs) | 32 |
| `c5f722b` | Split `_init_tools` (893 → 106 lines) → `agent/src/tool_groups.py` (7 group functions) — **registry 100% equivalent** to the original (verified names+descriptions+schemas) | 34 |
| `fdd2301` | `agent/src/autonomous/deadcode.py` — AST analyzer, 12 false-positive exclusion groups | 166 |

---

## Technical summary

- **Tools:** 131 → **160** (batches 0+1) → 159 (batch 2, actual env count)
- **Skills:** 80+ (16 new in batch 1)
- **Security modules:** 10 + tool sandbox + audit
- **Test files:** `evals/test_*.py` — every new module has a test that runs for real
- **Docs:** TOOLS.md, SKILLS.md, ARCHITECTURE.md, API.md, openapi.json, SELF_REVIEW.md, PLUGINS.md, INTEGRATION_NOTES.md

## Deploy notes

1. `gateway/api.py:487` still binds `127.0.0.1` — use `gateway/bind_config.py` (`BOOM_HOST`/`BOOM_PORT`/`BOOM_CORS_ORIGINS`) when LAN/mobile access is needed.
2. `benchmarks/bench_tools.py` runs standalone: `python3 benchmarks/bench_tools.py`.
3. `deadcode.py` is the new analyzer; it hasn't replaced the old `dead_code` rule in `codereview.py` yet — can be wired later.
4. `gateway/routes/websocket.py` exports `ROUTES = []` intentionally: the gateway currently has no public WebSocket endpoints (only internal channel adapters).
