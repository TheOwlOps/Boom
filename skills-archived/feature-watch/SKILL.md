---
name: feature-watch
description: "Watch the AI agent ecosystem, reporting new features/releases with citations."
version: 0.1.0
---

# Skill: feature-watch

> This is a **playbook for the agent** (the LLM reads it and follows each step).
> It is not executable code — every action goes through Boom's existing tools.

## When to use

- The user asks things like: "what's new", "what new features do AI agents have lately", "which framework just released".
- Daily/weekly cron tick (job `feature-watch-daily`).

## Sources to scan

The AI agent ecosystem: frameworks (openclaw, eliza/elizaOS, langchain,
autogen, crewai, composio, browser-use, skyvern...), new LLM releases,
new papers/patterns (ReAct, DAG, memory, evals).

## Per-tick workflow

### 1. Collect

- `news_search` with keywords: `AI`, `agent`, `LLM` on the `tin-moi-nhat`
  feed (VnExpress — the news connector array).
- `web_search` for new releases of agent frameworks, e.g.
  `"openclaw release notes"`, `"elizaOS changelog"`, `"langchain release"`,
  `"autogen new version"`, `"crewai release"`. Add GitHub releases/trending
  when needed (`site:github.com ... releases`).
- `web_extract` into release/changelog URLs to read details when verification is needed.

### 2. Filter

- **Keep only what is VERIFIED**: every item must have a real URL (release page,
  changelog, official blog, or news article). Never invent features,
  never guess release dates — drop anything that can't be verified.
- Read the state file `memories/feature-watch.json` (list of already-reported URLs).
  Any item whose URL is already in the state → **skip**.
- State file doesn't exist yet → treat as empty, create it after reporting.

### 3. Publish the briefing (report)

- Short briefing: each item **1–2 lines + citation link** `[n]`.
- Don't use `digest_preview` when there are few items (~5 or fewer) — return plain
  text directly to keep it short; with many items, use `digest_preview` for consistency.
- **If there's nothing new, reply with exactly:** `No notable new features today.`
  Don't make things up to fill space.

### 4. Update state

- After reporting, append the reported URLs to `memories/feature-watch.json`
  (an array of URLs, keeping at most the ~500 most recent so the file doesn't bloat).
- If nothing new → write nothing.

## Briefing example

```
🛰️ AI agent — latest updates

1. CrewAI 0.177.0 adds a native memory server, detached from the main
   loop [1]
2. ElizaOS opens a new plugin for background scheduled tasks [2]

[1] https://github.com/crewAIInc/crewAI/releases/tag/0.177.0
[2] https://github.com/elizaOS/eliza/releases/tag/...
```

## Rules

- This skill differs from `competitor-news-monitor`: that one watches *companies*,
  this one watches the *AI agent ecosystem* (frameworks, releases, patterns).
- Keep user-facing parts in English. Citation links are mandatory for every item.
- Dedupe via the state file, never rely on memory.
