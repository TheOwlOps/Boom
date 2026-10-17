# SELF-REVIEW: Boom evaluates its own codebase with its own modules

> **Run date:** 2026-10-16 ~02:30 +07
> **Repo:** `TheOwlOps/Boom`, branch `main` — `git pull` run, status "Already up to date" (read-only, no repo changes, no push)
> **Worker:** subagent "conversation with Boom"

## 0. Method transparency — what's real module output vs. worker commentary

| # | Module run | Command actually run | Result |
|---|---|---|---|
| 1 | `agent/src/autonomous/planner.py` — `TaskPlanner.plan()` + `to_markdown()` | ✅ ran for real, heuristic needs no LLM | Real output, verbatim in section 1 |
| 2 | `agent/src/reasoning/cot.py` — `ChainOfThought.think()` | ⚠️ **pipeline ran for real, "LLM" is a stub** | See explanation below |
| 3 | `agent/src/autonomous/codereview.py` — `CodeReviewer.review_file()` on 5 core files | ✅ ran for real, real AST | Real output, verbatim in section 3 |

**On ChainOfThought — stated plainly to avoid fabricated reporting:** `ChainOfThought` requires an injected `llm_fn`. Actual checks:
- The muse-bridge endpoint `127.0.0.1:20138` was **not reachable** (curl → HTTP 000, connection failed).
- No provider key was available in the worker environment.
- `evals/test_reasoning.py` only contains toy mocks (an A>B>C puzzle), unusable for real analysis.

So the worker ran the **real CoT pipeline** (per-step `_step_prompt` construction, history chaining over 5 steps, `_summary_prompt` construction, answer parsing via the module's literal `Đáp án:` regex — Vietnamese for "Answer:") but the `llm_fn` was a **deterministic stub written by the worker** — each reasoning step is text the worker wrote, and **every claim rests on evidence already verified with `CodeReviewer`, `grep`, and `wc`** in earlier steps. **The three weaknesses in section 2 are the worker's evidence-backed analysis, NOT real LLM output.** The CoT mechanics (step splitting, summarization) ran 100% for real.

**The 5 core files reviewed** (the task named `brain/` and `memory/` as directories — they don't exist, so the worker picked representative files and says so):
- `environment.py` → `agent/src/environment.py` (893 lines)
- `gateway/api.py` → `gateway/api.py` (500 lines)
- `loop/react.py` → `agent/src/loop/react.py` (359 lines)
- `brain/` → `agent/src/orchestration/brain.py` (823 lines) — the ReAct+DAG+MASS brain, no `brain/` directory
- `memory/` → `agent/src/memory/longterm.py` (391 lines) — the largest, most central file in `agent/src/memory/`

---

## 1. Plan from TaskPlanner — 100% real output

Input task: **"What is Boom missing to become the strongest harness?"**

`TaskPlanner.plan()` classified task_type = `feature` (keyword heuristic) and returned:

```markdown
# Execution plan

**Task:** What is Boom missing to become the strongest harness?
**Type:** New feature
**Estimated steps:** 5

## Steps

### 1. Survey codebase `low`
Read the existing code to understand module structure, conventions, and where a new feature fits.
- **Actions:** read, explore

### 2. Design solution `low`
Define the API/parameters, code placement, and edge cases before writing code.
- **Actions:** design
- **Depends on:** step_1

### 3. Implement feature `high`
Write code per the design, split by file, following repo conventions.
- **Actions:** implement, multi-file
- **Depends on:** step_2

### 4. Write tests `medium`
Add tests covering correct/incorrect behavior and edge cases of the new feature.
- **Actions:** test, implement
- **Depends on:** step_3

### 5. Review and finalize `medium`
Run all related tests, re-scan the code, clean up leftovers, then conclude.
- **Actions:** test, review
- **Depends on:** step_4
```

### The worker's commentary on this output (not module output)

This plan contains **no information specific to Boom or to the question "what's missing"** — it's a fixed template for the `feature` type. The worker verified further:

- `plan("Add dark mode to the web UI")` → step titles **100% identical** to the Boom task's plan.
- `plan("Fix crash when sending a Telegram sticker")` → the `bugfix` template, equally fixed.
- `_suggest_files()` returned `[]` for this task because no keyword in the description matched the suggestion list (`tool/channel/cron/memory/skill/gateway/web/autonomous/test`).

**Conclusion:** TaskPlanner is currently a **keyword-based template emitter that never reads the codebase**. For an open question like "what's missing", it can't produce a single concrete survey step (e.g.: read `GAP.md`, run evals, compare against RikkaHub/OpenClaw). This is weakness #1 in section 2.

---

## 2. The three biggest weaknesses — from the ChainOfThought pipeline (transparent stub per section 0)

`ChainOfThought.think("What are Boom's biggest current weaknesses? List the 3 biggest.", max_steps=5)` ran for real; the 5 intermediate reasoning steps and the synthesized answer (extracted via the module's literal `Đáp án:` regex — Vietnamese for "Answer:") follow:

**Answer (extracted from the CoT summary):**
> 1) Heuristic TaskPlanner that never analyzes the real codebase; 2) Complexity concentrated in god-functions/branch-heavy spots (environment._init_tools 787 lines/CC18, gateway do_GET CC30, react._run_live_turn CC29); 3) CodeReviewer dead_code ~40% false positive at module scope (doesn't track variables used in child functions, doesn't count arg defaults) + scattered print() logging.

**The 5 intermediate reasoning steps (verbatim from the stub, each grounded in measured evidence):**
1. *Evidence from CodeReviewer (actually run on 5 core files): 58 issues. Highlights: environment._init_tools is 787 lines, CC=18; gateway do_GET CC=30, 88 lines; react._run_live_turn CC=29, 104 lines; brain._heuristic_plan CC=28; 5 dead config constants in gateway/api.py (MAX_BODY_BYTES, ALLOWED_ORIGINS, CHAT_STRATEGIES, KNOWN_CHANNELS, CHANNEL_CREDENTIAL_HINT — 0 references repo-wide).*
2. *TaskPlanner verification: plan('What is Boom missing...') and plan('Add dark mode to the web UI') give byte-identical step titles. The module doesn't read the repo; file-hints are empty because no keyword matches. → Weakness 1: task analysis is not codebase-aware.*
3. *dead_code rule verification: brain.py 15/15 warnings are false positives (every variable is used inside function bodies — the module-scope rule doesn't track cross-scope); longterm.SUMMARIZE_AFTER_DAYS used at line 274; react.HYDRATE_MESSAGES used as an arg default at line 91; api.VERSION/DEFAULT_PORT imported elsewhere. → Weakness 3: the self-review tool lacks reliability in this rule.*
4. *Weakness 2 is clear from the numbers: all tool-registration logic is packed into ONE 787-line function (_init_tools) — every new tool (131→139 tools) must touch it, a merge/conflict risk and hard to unit-test. Likewise do_GET (CC30) packs the entire gateway API's HTTP routing. This is an architecture bottleneck, not just style.*
5. *Ranked by impact: (1) Codebase-blind planner — every automated plan is generic and misdirected from step one; (2) Concentrated god-functions — one breakage takes out the whole tools/gateway stack; (3) Noisy reviewer — automating review on a high-false-positive base will miss real bugs or cause alert fatigue.*

### The worker's correction after deeper verification (honest, nothing hidden)

The "~40% false positive" in the CoT answer was a **conservative estimate made when writing the stub** — after verifying each variable with repo-wide `grep`, the real rate is even worse:

- Total `dead_code` warnings: **28**, of which **22 are false positives (~79%)**, only **6 are real**.
- Single root cause: the rule analyzes **each scope independently** — a variable assigned at module scope but only `Load`ed **inside function bodies** (e.g. 15/15 variables in `brain.py`: `_DEP_WORDS` used at line 164, `_PLAN_SYSTEM` at 413, `_MASS_CONCURRENCY` at 798…) is declared "unused". Similarly: `HYDRATE_MESSAGES` used as an arg default (line 91 of `react.py`), `SUMMARIZE_AFTER_DAYS` used at line 274 of `longterm.py`, `_API_START_TIME`/`_LOG_BUFFER`/`_LOG_MAX` used inside functions in `api.py`, `VERSION`/`DEFAULT_PORT` imported from other modules.
- The 6 **real** warnings: 5 dead config constants in `gateway/api.py` (`MAX_BODY_BYTES`, `ALLOWED_ORIGINS`, `CHAT_STRATEGIES`, `KNOWN_CHANNELS`, `CHANNEL_CREDENTIAL_HINT` — 0 references repo-wide) and a `_errors` variable assigned but unused in `_chat_sse` (line 442).
---

## 3. Top 10 issues from CodeReviewer — real output, each verified

Total: **58 issues** across 5 files (49 warnings + 9 info, **0 errors** — no bare `except:`, no syntax errors: a plus). Below are the top 10 by impact; each issue is the real output of `review_file()`; the "Verify" column is the worker's own check.

| # | File | Issue (real module output) | Worker verification |
|---|---|---|---|
| 1 | `agent/src/environment.py:90` | `long_function`: `_init_tools` is **787 lines** (threshold 50); `complexity`: **CC=18** | ✅ Real. The function registers all ~139 tools inline, from line 90 to ~876. The biggest god-function in the agent layer. |
| 2 | `gateway/api.py:233` | `complexity`: `do_GET` **CC=30** (threshold 10); `long_function` 88 lines | ✅ Real. All gateway API GET routing in one function — every new endpoint adds another branch. |
| 3 | `agent/src/loop/react.py:237` | `complexity`: `_run_live_turn` **CC=29**; `long_function` 104 lines | ✅ Real. The main ReAct turn loop — the likeliest place for bugs to grow, and the hardest to test because of all the branching. |
| 4 | `agent/src/orchestration/brain.py:505` | `complexity`: `_heuristic_plan` **CC=28**; `long_function` 82 lines | ✅ Real. The no-LLM fallback planner — the more complex the heuristic, the less trustworthy it is exactly when it's needed most. |
| 5 | `agent/src/orchestration/brain.py:362` | `complexity`: `_parse_plan_json` **CC=22** | ✅ Real. Parses the LLM's plan JSON — high complexity here means malformed LLM output slips through more easily. |
| 6 | `gateway/api.py:126,374,341` | `complexity`: `dag_status` CC=17, `_handle_chat` CC=14, `_handle_tool_run` CC=11 | ✅ Real. All 3 HTTP handlers exceed the threshold — a shared pattern: handlers mixing routing and logic. |
| 7 | `gateway/api.py:59,60,64,68` (+57) | `dead_code`: 5 constants `MAX_BODY_BYTES`, `ALLOWED_ORIGINS`, `CHAT_STRATEGIES`, `KNOWN_CHANNELS`, `CHANNEL_CREDENTIAL_HINT` assigned but unused | ✅ **Real** (verified with repo-wide `grep -rn`: 0 references). Dead config — either vestigial, or half-built features nobody remembers. |
| 8 | `agent/src/environment.py:1` / `brain.py:1` | `long_file`: 893 lines / 823 lines (threshold 500) | ✅ Real. Both central files far exceed the threshold — overlaps issues #1, #4, #5. |
| 9 | `environment.py:399,411,511,876` + `api.py:470,489` + `react.py:177` | `print_stmt`: 7 uses of `print()` instead of logging | ✅ Real. Raw stdout logging — hard to manage, filter by level, or trace per request once the daemon/gateway runs for real. |
| 10 | `gateway/api.py:442` | `dead_code`: `_errors` assigned but unused in `_chat_sse` | ✅ Real (unpack `reply, used_strategy, _errors = ...` then never use `_errors`). Small but a real bug — the turn's errors should have been logged/emitted. |

**Warnings confirmed as false positives (excluded from the top 10):** 15/15 `dead_code` in `brain.py`, `SUMMARIZE_AFTER_DAYS` (`longterm.py:26`), `HYDRATE_MESSAGES` (`react.py:8`), `VERSION`/`DEFAULT_PORT`/`_API_START_TIME`/`_LOG_BUFFER`/`_LOG_MAX` (`api.py`) — all genuinely used per `grep` verification (see section 2). The `dead_code` rule's noise rate: **22/28 ≈ 79%**.

---

## 4. Top 5 proposed improvements (worker commentary, evidence-backed)

**P0-1. Split the `_init_tools` god-function (787 lines) into per-group tool registries.**
Evidence: issue #1. Every new tool (131→139 just now) edits the same giant function — conflict risk when multiple workers add tools, and impossible to unit-test per group. Proposal: each tool group gets its own module (`tools/files.py`, `tools/web.py`, …) self-registering via decorator or `register()`, with `_init_tools` just calling the registry. Structure change only, behavior unchanged.

**P0-2. Make TaskPlanner read the real repo instead of emitting templates.**
Evidence: section 1 + weakness #1. Right now "What's missing" and "add dark mode" plans are identical — useless for the autonomous controller. Proposal: use the existing `agent/src/autonomous/depgraph.py` to build a dependency graph, make `_suggest_files` scan real file/symbol names instead of a fixed keyword list, and estimate complexity from the files touched. This is the biggest lever for autonomous mode, since every goal starts with a plan.

**P0-3. Fix CodeReviewer's `dead_code` rule before trusting it.**
Evidence: 22/28 false positives (~79%). Proposal: (a) when scanning module scope, also collect `Load`s inside the module's function bodies (cross-scope); (b) count arg defaults as `Load`s; (c) ignore `_`-prefixed variables in unpacks (the "intentionally unused" convention). Add a regression test running `review_file` on these 5 core files and asserting FP count = 0. A self-review tool with 79% noise either gets turned off, or worse — gets trusted wrongly.

**P1-4. Split the gateway HTTP router + replace `print()` with logging.**
Evidence: issues #2, #6, #9. `do_GET` at CC=30 is a bottleneck: every new endpoint adds an `if` branch. Proposal: a dispatch table `path → handler` (one function/module per handler in `gateway/routes/`), `do_GET` down to ~10 lines. Replace the 7 `print()` calls with `logging` at proper levels and module-named loggers — once the gateway runs as a daemon under PM2/systemd, this is the difference between "debuggable" and "flying blind".

**P1-5. Clean up the 5 dead constants + the swallowed `_errors` + split the long files.**
Evidence: issues #7, #8, #10. `MAX_BODY_BYTES`, `ALLOWED_ORIGINS`, `CHAT_STRATEGIES`, `KNOWN_CHANNELS`, `CHANNEL_CREDENTIAL_HINT` — delete them, or if they're half-built features, give them intentional TODOs (right now they're "dead code nobody remembers"). `_errors` in `_chat_sse` should be emitted/logged, not swallowed silently — the one place that most needs observability (failed chat turns) is currently mute. `environment.py` (893) and `brain.py` (823) should be split by responsibility after P0-1.

---

## Appendix: raw numbers for cross-checking

- `CodeReviewer` on 5 files: **58 issues** = 49 warnings + 9 info + 0 errors. No `bare_except`, no syntax errors.
- `dead_code` rule: 28 warnings → 6 real, 22 false positives (~79%), one root cause (no cross-scope tracking).
- `TaskPlanner`: 2 different `feature` tasks → byte-identical step titles; `files: []` because no keyword matched the suggestion list.
- `ChainOfThought`: real pipeline (5 step prompts + 1 summary, the module's literal `Đáp án:` ("Answer:") regex parsing) with a deterministic `EvidenceLLM` stub — the reasoning content was written by the worker from measured evidence, **not an LLM**.
- Intermediate JSON files (not committed to the repo, worker cross-checking only): `/tmp/selfreview_issues.json`, `/tmp/selfreview_cot.json`.
- Repo unchanged: only `git pull` (already up to date); all analysis ran read-only.
