# Hermes Agent: System Architecture & Execution Lifecycle Specification

## 1. Executive Summary

Hermes Agent is a production-grade, self-improving autonomous agent framework designed to execute complex reasoning and coding workflows across diverse deployment environments (local CLI, cloud VM, serverless sandboxes, and multi-channel messaging platforms).

Unlike static prompt-wrapper architectures, Hermes features:
- **Closed-Loop Self-Improvement**: Dynamic skill synthesis, in-session memory curation, and FTS5-backed episodic memory retrieval.
- **Micro-Compaction & Trajectory Compression**: Token budget-aware trajectory management preventing context degradation.
- **Asynchronous Multi-Platform Gateway**: Unified ingestion bus multiplexing Telegram, Discord, Slack, WhatsApp, and Webhooks into a single event loop.
- **Extensible Host Isolation**: Zero-coupling plugin interface (`register(ctx)`) with optional out-of-process process sandboxing.

---

## 2. Core Architecture Topography

```
+-----------------------------------------------------------------------------------+
|                            Multi-Channel Gateway Bus                              |
|   [Telegram]    [Discord]    [Slack]    [WhatsApp]    [Signal]    [Webhooks/API]  |
+-----------------------------------------------------------------------------------+
                                          |
                               (IncomingEvent Queue)
                                          v
+-----------------------------------------------------------------------------------+
|                             Session & State Manager                               |
|        - SQLite WAL Database (Sessions, Timelines, Messages, FTS5 Virtual Index)  |
|        - Profile & Auth Isolation (Multi-tenant Credential Resolution)            |
+-----------------------------------------------------------------------------------+
                                          |
                                (Turn Context Assembly)
                                          v
+-----------------------------------------------------------------------------------+
|                             Agent Reasoning Engine                                |
|        - ReAct Iteration Loop (Thought -> Tool Call -> Observation)               |
|        - Prompt Builder with Progressive Skill Disclosure (<available_skills>)    |
|        - Context Compressor & Window Truncator                                    |
+-----------------------------------------------------------------------------------+
                   |                                             |
         (Tool Call Dispatch)                          (Reflection & Learning)
                   v                                             v
+--------------------------------------+      +-------------------------------------+
|        Tool Execution Engine         |      |       Autonomous Learning Loop      |
| - Sandbox Shell / Terminal (PTY)     |      | - Episodic Storage (FTS5 search)    |
| - AST Code Inspection & Fuzzy Patch  |      | - Procedural Skills (`SKILL.md`)    |
| - Headless Browser (CDP / Stealth)   |      | - Semantic Curation (Profile notes) |
| - Subagent Delegation (`delegate`)   |      | - Autonomous Skill Auto-Synthesis   |
+--------------------------------------+      +-------------------------------------+
```

---

## 3. The Turn Execution Lifecycle

Every user turn follows a deterministic state machine orchestrated by `run_turn.py` and `conversation_loop.py`:

```
User Input Received
       |
       v
1. Preflight Validation
   - Access & Authorization Check (owner guardrails, tenant tokens)
   - Secret Masking & Redaction Scrubber
   - Rate Limit & Token Budget Verification
       |
       v
2. Context Construction (`prompt_builder.py`)
   - Static System Instructions + Dynamic Environment Facts
   - Semantic Profile Injection (High-signal personal/standing rules)
   - Progressive Skill Index (`<available_skills>` catalog - name + 57-char summary)
   - Paged Message History with Micro-Compacted Tool Results
       |
       v
3. Model Inference Round
   - Streaming LLM Generation with Thinking/CoT Scrubber
   - Parsing Structured Tool Calls (JSON Schema validation)
       |
       v
4. Tool Call Execution Round (`tool_executor.py`)
   - Pre-Tool Hooks (Auditing, permission validation)
   - Concurrency Check: Batch independent calls; serialize dependent calls
   - Execution Sandbox (Local shell, Docker, or isolated subagent)
   - Observation Intake & Truncation (Spillover large output to disk)
   - Post-Tool Hooks (Metrics, log streaming)
       |
       v
5. Stop Gate Decision
   - If Model outputs text response -> Exit loop to step 6.
   - If Model requests more tools & iterations < `max_turns` -> Loop to step 3.
   - If `max_turns` or timeout exceeded -> Trigger Graceful Truncation Handler.
       |
       v
6. Post-Turn Finalization (`turn_finalizer.py`)
   - Deliver Response to Channel Egress Queue (with rate-limiting/typing indicators)
   - Append Turn to SQLite WAL State DB (`messages` and `timeline`)
   - Trigger Asynchronous Reflection & Memory Nudge if significant task concluded.
```

---

## 4. Context Engineering & Memory Compaction

To prevent token blowout in long-running tasks, Hermes utilizes a three-tier context strategy:

### A. Progressive Disclosure
Full tool documentation and skill instructions are not dumped into the system prompt. Instead:
1. Index level: Only short triggers are shown.
2. Demand level: When the agent invokes `skill_view(name="...")`, the detailed guide enters the context strictly for that turn.

### B. Micro-Compaction (`micro_compaction.py`)
- Tool responses exceeding output limits (e.g. ripgrep outputs, giant terminal logs) are summarized or stored to temporary disk files, returning a head/tail snippet with a disk pointer.
- Stale tool results older than $N$ turns are collapsed into one-line digests.

### C. Trajectory Compression (`trajectory_compressor.py`)
- When total conversation tokens reach the model context window limit, conversation history undergoes rolling compression.
- An auxiliary model generates a structured summary preserving:
  - Key user intents and constraints.
  - Files modified, commands executed, and verified results.
  - Pending action items.
- Summarized turns are archived to the SQLite timeline and pruned from the active prompt array.
