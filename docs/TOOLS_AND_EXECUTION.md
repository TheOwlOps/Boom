# Boom Tools: Execution Engine & Safety Guardrails Specification

## 1. Tool Subsystem Overview

Boom uses a hybrid tool dispatch system that balances broad capability with context efficiency:
1. **Core Tools** (Directly loaded in system prompt): High-frequency operations like terminal execution, file manipulation, and skill inspection.
2. **Deferred Tools** (Dynamic Catalog): 100+ specialized tools (GitHub, Canva, Cloud integrations) discovered on-demand via `tool_search`, described with `tool_describe`, and invoked with `tool_call`.
3. **Programmatic Meta-Execution (`execute_code`)**: Executes Python scripts that invoke tools locally via RPC, collapsing multi-step workflows into zero-context-cost iterations.

---

## 2. Core Tool Specifications

### A. Terminal Execution (`terminal_tool.py`)
- **Capabilities**: Foreground and background execution with pseudo-terminal (PTY) support.
- **Process Registry**: Tracks active background jobs in `processes.json`. Provides health polling, heartbeat notifications, and exit code capture.
- **Output Spillover**: Outputs exceeding 50KB are truncated with head/tail previews in chat while full output is automatically persisted to scratch disk.
- **Safety Floors**: Prohibits interactive blocking commands unless running in PTY mode; enforces timeouts (default 180s, max 600s).

### B. Precision File Manipulation
Boom avoids brute-force file rewriting:
- **`read_file`**: 1-indexed pagination (`offset`, `limit`) with document text extractors for PDF, Office, and SQLite files.
- **`patch` (`fuzzy_match.py`)**: Unified find-and-replace using 9 fuzzy matching heuristics (whitespace tolerance, indentation alignment) to guarantee safe edits without clobbering concurrent changes.
- **`write_file`**: Full file overwriter with mandatory read-before-write safety validation; automatically triggers syntax linters (`ast.parse`, `json.loads`, etc.) upon saving.

### C. Programmatic Kernel (`execute_code`)
Instead of making 10 individual tool calls (wasting 10 LLM round-trips and filling the context window), the agent can execute a Python script:
```python
from boom_tools import search_files, read_file, patch

# Multi-file refactor inside a single turn:
files = search_files(pattern="old_api_v1", target="content")["matches"]
for f in files:
    patch(path=f["path"], old_string="old_api_v1()", new_string="new_api_v2()")
print(f"Refactored {len(files)} files.")
```

### D. Subagent Isolation (`delegate_task`)
Spawns background subagents in isolated workspaces:
- Isolated context window and terminal sessions.
- Parent agent receives only the structured final result or summary upon subagent completion.
- Prevents context flooding during deep research or large-scale code builds.

---

## 3. Tool Safety & Guardrail Architecture

```
Tool Invocation Request
           |
           v
+-------------------------------------------------------+
|              Tirith Security & Approval Gate          |
|  - Validates command against dangerous patterns       |
|    (e.g. `rm -rf /`, credential harvesting)           |
|  - Enforces User Approval Prompts for sensitive ops   |
+-------------------------------------------------------+
           |
           +---> [Rejected / User Denied] -> Returns abort signal to Agent
           |
           v
+-------------------------------------------------------+
|                 Execution Environment                 |
|  - Path Security: Blocks escapes outside workspace    |
|  - Secret Scrubber: Redacts tokens & private keys     |
|  - Timeout Watchdog: Kills hung child processes       |
+-------------------------------------------------------+
           |
           v
+-------------------------------------------------------+
|               Output Intake & Sanitization            |
|  - ANSI Escape code stripper                          |
|  - Budget-aware truncation and scratch file spillover |
+-------------------------------------------------------+
```
