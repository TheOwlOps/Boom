# CHANGELOG — Phiên làm việc xuyên đêm 2026-10-16

**Thời gian:** 01:36 → ~07:00 (+07), 2026-10-16
**Repo:** TheOwlOps/Boom, branch `main`
**Kết quả:** 26 commits, **~1.400+ assertions PASS**, 0 fail.

---

## Đợt 0 — Hardening coding + security (01:36)

**Commit `543a7bf`** — 8 modules mới, 286 tests PASS.

**CODING** → `agent/src/autonomous/`:
| Module | Tests | Chức năng |
|---|---|---|
| `codereview.py` | 34 | Review tĩnh: complexity, dead code, bare except, TODO |
| `depgraph.py` | 29 | Import graph, phát hiện circular, impact analysis |
| `gitops.py` | 40 | Diff summary, branch guard, safe commit (quét secret) |
| `coverage.py` | 19 | Coverage qua sys.monitoring (stdlib, Python 3.12+) |

**BẢO MẬT** → `agent/src/security/`:
| Module | Tests | Chức năng |
|---|---|---|
| `secretscan.py` | 43 | Quét 8 loại secret, preview che 80% |
| `sanitize.py` | 49 | Chống prompt injection (Việt+Anh), path traversal |
| `ratelimit.py` | 43 | Token bucket per-user, thread-safe |
| `redact.py` | 29 | Che secret trong audit log (fail-open) |

**Wiring:** 8 tools mới vào `environment.py` → **131 → 139 tools**. `redact` móc vào `AuditStore.log()`.

---

## Đợt 1 — Tools/Skills/Security/UI/Mobile (01:45 → 02:35)

19 commits (`02d4189` → `bf171b9`), ~800+ tests PASS.

### Tools — tổ chức lại thành 7 packages
| Commit | Nội dung | Tests |
|---|---|---|
| `72ca054` | `tools/filesystem/`, `tools/terminal/`, `tools/git/` | 91 |
| `6b63299` | `tools/web/`, `tools/code/`, `tools/security/`, `tools/db/` | 75 |
| `f4ed006` | Wire 7 packages vào environment | 93 → **139 → 158 tools** |
| `b7ccfad` | Wire `deep_search`/`deep_think` | **→ 160 tools** |

Mỗi package có SSRF / SQLi / path-traversal protection.

### Skills — 16 mới (tổng 80+)
| Commit | Skills |
|---|---|
| `02d4189` | deployer, doc-researcher, refactor-assistant, api-designer (38 tests) |
| `3feac82` | code-review, test-writer, security-audit, perf-optimizer (30 tests) |
| `54a47bc` | db-helper, cron-manager, git-workflow, env-setup (42 tests) |
| `afb484b` | log-analyzer, backup-manager, notify-hub, data-validator (25 tests) |

### Security — 10 modules
`secretscan`, `sanitize`, `ratelimit`, `redact` (đợt 0) + `a85ee9d` command validator + env protector (45 tests) + `c8faae6` tool execution sandbox + audit (46 tests).

### Harness core
| Commit | Nội dung | Tests |
|---|---|---|
| `d6f33f4` | Context window management | 34 |
| `90e659a` | Self-healing agent loop | 26 |
| `bf171b9` | RepoAwarePlanner v2 | 16 |
| `ccfd154` | Performance utils (TTLCache, LazyLoader, profiler) | 31 |

### UI / Mobile / Docs
| Commit | Nội dung |
|---|---|
| `5ba47cd` | Dashboard UI: StatusBar, ToolManager, SkillList, TerminalLog (build PASS) |
| `5a0820c` | Mobile PWA + API enhancements (`/api/logs`, health stats) |
| `c1e4f8c` | OpenAPI spec + API docs (9 endpoints) |
| `b7ccfad` | TOOLS.md (158), SKILLS.md (80), ARCHITECTURE.md |

### Bug thật đã fix
- `a2d1a20`: `build_environment()` không tồn tại trong `api.py` → dùng `AgentEnvironment()`.

### "Trao đổi với Boom"
Dùng chính module Boom (planner, reasoning) để tự đánh giá — chi tiết trong `docs/SELF_REVIEW.md`.

---

## Đợt 2 — Redo sau VM restart (fix tồn đọng + hoàn thiện)

6 commits local (chờ push), ~580 assertions PASS.

| Commit | Nội dung | Tests |
|---|---|---|
| `3192f99` | `gateway/bind_config.py` — bind 0.0.0.0 + CORS đầy đủ cho mobile PWA trong LAN (phát hiện: `api.py:487` bind cứng 127.0.0.1) | 43 |
| `353a307` | Tách gateway router → `gateway/routes/` (health, chat, tools, channels, admin, websocket) — 9 endpoints thật, backward-compat | 214 |
| `0b348c9` | Integration test full pipeline: env init, 159 tools, gọi tool thật — **không phát hiện bug** | 39 |
| `902a107` | `benchmarks/bench_tools.py` — 17 benchmarks (import ~300ms/pkg, `_init_tools` 0.5ms, tool calls pure ~µs) | 32 |
| `c5f722b` | Tách `_init_tools` (893 → 106 dòng) → `agent/src/tool_groups.py` (7 hàm group) — **registry tương đương 100%** bản gốc (verify names+descriptions+schemas) | 34 |
| `fdd2301` | `agent/src/autonomous/deadcode.py` — AST analyzer, 12 nhóm loại trừ false positive | 166 |

---

## Tổng kết kỹ thuật

- **Tools:** 131 → **160** (đợt 0+1) → 159 (đợt 2, đếm thực tế trên env)
- **Skills:** 80+ (16 mới trong đợt 1)
- **Security modules:** 10 + tool sandbox + audit
- **Test files:** `evals/test_*.py` — mọi module mới đều có test chạy thật
- **Docs:** TOOLS.md, SKILLS.md, ARCHITECTURE.md, API.md, openapi.json, SELF_REVIEW.md, PLUGINS.md, INTEGRATION_NOTES.md

## Lưu ý khi deploy

1. `gateway/api.py:487` vẫn bind `127.0.0.1` — dùng `gateway/bind_config.py` (`BOOM_HOST`/`BOOM_PORT`/`BOOM_CORS_ORIGINS`) khi cần LAN/mobile truy cập.
2. `benchmarks/bench_tools.py` chạy độc lập: `python3 benchmarks/bench_tools.py`.
3. `deadcode.py` là analyzer mới, chưa thay thế rule `dead_code` cũ trong `codereview.py` — có thể wire sau.
4. `gateway/routes/websocket.py` export `ROUTES = []` có chủ ý: gateway hiện không có endpoint WebSocket công khai (chỉ nội bộ trong channel adapters).
