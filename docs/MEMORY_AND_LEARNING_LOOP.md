# Boom Memory & The Closed Learning Loop Specification

## 1. The Tri-Tier Memory Architecture

Boom implements a structured memory architecture designed for persistence, precision, and low token overhead across sessions:

```
+-----------------------------------------------------------------------------------+
| 1. Semantic Memory (Durable Declarative Facts)                                    |
|    - Target 'user': Who the user is, role, preferences, language style.           |
|    - Target 'memory': Standing environment facts, conventions, hardware rules.    |
|    - Injected directly into the system prompt of every turn (strictly budgeted). |
+-----------------------------------------------------------------------------------+
| 2. Episodic Memory (Cross-Session Experience & History)                          |
|    - Stored in SQLite WAL database (`state.db`).                                  |
|    - Indexed via FTS5 (Full-Text Search) and semantic embeddings.                 |
|    - Queried via `session_search` tool: Retrieves prior solutions & discussions.  |
+-----------------------------------------------------------------------------------+
| 3. Procedural Memory (Reusable Operational Skills)                                |
|    - Structured directories containing `SKILL.md` files.                          |
|    - Compatible with open standard `agentskills.io`.                              |
|    - Contains YAML frontmatter (trigger, summary) + execution guide + scripts.   |
|    - Loaded via Progressive Disclosure (zero prompt bloat when unused).           |
+-----------------------------------------------------------------------------------+
```

---

## 2. Progressive Disclosure Specification

To prevent hundreds of skills from polluting the context window:
1. **Catalog Stage**: During startup, `skill_utils.py` reads only the YAML frontmatter of all available skills. A concise `<available_skills>` XML block is generated:
   ```xml
   <available_skills>
     ast-grep: AST-aware structural code search and rewrite via ast-grep.
     systematic-debugging: 4-phase root cause debugging: understand bugs before fixing.
   </available_skills>
   ```
2. **Activation Stage**: When the agent recognizes a matching task, it invokes:
   ```json
   {"name": "skill_view", "arguments": {"name": "systematic-debugging"}}
   ```
3. **Execution Stage**: The full content of `SKILL.md` and any linked scripts (`scripts/run.py`) or reference templates are returned as tool output, entering context only for the duration of that task.

---

## 3. The Autonomous Self-Learning Loop (Reflexion)

Boom is not a static agent; it improves its own knowledge base through a closed learning loop:

```
Task Execution (User Request -> Reasoning -> Tool Calling)
                     |
                     v
             Evaluation Gate
      [Task was non-trivial / error fixed]
                     |
                     v
+-------------------------------------------------------+
|             Reflexion & Extraction Phase              |
|  - What was the root cause of the initial failure?    |
|  - What specific command or pattern resolved it?      |
|  - Is this rule generalizable to future sessions?     |
+-------------------------------------------------------+
                     |
                     +----------------------------+
                     |                            |
       (Universal Environment Fact)    (Procedural Workflow)
                     v                            v
+------------------------------------+   +------------------------------------+
|        `memory` Tool Update        |   |       `skill_manage` Action        |
|  - Updates Semantic Profile Notes  |   |  - Synthesizes new `SKILL.md` or   |
|  - Consolidates stale entries to   |   |  - Patches existing skill to add   |
|    respect char budget limits      |   |    discovered pitfalls/workarounds |
+------------------------------------+   +------------------------------------+
```

### Self-Improvement Invariant Rules:
1. **Write lessons, not logs**: Store imperative rules and underlying reasons, not timestamps, PR numbers, or incident narratives.
2. **Atomic batch updates**: Memory and skill edits apply atomically to prevent corrupt or partial states.
3. **Continuous Skill Evolution**: If an existing skill contains an outdated command or misses an edge case discovered during debugging, the agent patches the skill before completing the turn.
