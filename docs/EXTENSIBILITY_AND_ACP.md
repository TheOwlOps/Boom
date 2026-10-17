# Boom Extensibility: Plugin Architecture & Agent Client Protocol (ACP)

## 1. Plugin Core Philosophy: "Plugins Never Touch Core"

Boom strictly enforces structural decoupling:
- A plugin must never modify core runtime files (`run_agent.py`, `cli.py`, `gateway/run.py`).
- Plugins reside in isolated directories (`plugins/<name>/`, `~/.boom/plugins/`, or `./.boom/plugins/`).
- All integrations occur through a well-defined `PluginContext` (`ctx`) interface exposed during discovery.

---

## 2. Plugin Contract: `register(ctx)`

Every plugin exposes a root entrypoint (`__init__.py` or `entry.py`) containing the canonical `register(ctx)` lifecycle function:

```python
def register(ctx) -> None:
    # 1. Register new agent tools
    ctx.register_tool(
        name="custom_scanner",
        func=run_scanner,
        description="Scans project files for compliance rules",
        schema={...}
    )

    # 2. Wire lifecycle hooks
    ctx.register_hook("pre_tool_call", on_pre_tool_audit)
    ctx.register_hook("post_tool_call", on_post_tool_metric)
    ctx.register_hook("pre_llm_call", on_pre_inference)
    ctx.register_hook("on_session_start", on_session_init)
    ctx.register_hook("on_session_end", on_session_teardown)

    # 3. Register CLI subcommands
    ctx.register_cli_command(
        name="scan",
        handler=cli_scan_handler,
        help_text="Run compliance scan from command line"
    )
```

---

## 3. Host Process Isolation (`plugins.isolation: host`)

For untrusted or resource-heavy plugins, Boom provides process-level isolation:
- **`in_process` (Default)**: Plugins are imported directly into the main Python runtime.
- **`host` Mode**: Non-bundled plugins run inside an isolated subprocess (`plugin_host_child.py`).
- **Framed JSON Wire Protocol**: Communication between Boom Core and the Plugin Host occurs over a dedicated pipe/socket.
- **Crash Immunity**: A crash or segfault in a plugin process does not destabilize the core agent loop.

---

## 4. Agent Client Protocol (ACP) Specification

The `acp_adapter/` module exposes the agent via the open Agent Client Protocol, enabling direct integration into code editors and IDEs (e.g. Zed, VS Code, JetBrains):

```
+-----------------------------------------------------------------------------------+
|                        Editor / IDE Client (Zed, VS Code)                         |
+-----------------------------------------------------------------------------------+
                                          |
                              (ACP JSON-RPC Protocol)
                                          v
+-----------------------------------------------------------------------------------+
|                       `acp_adapter` Gateway Server                                |
|  - JSON-RPC over stdio / TCP loopback                                             |
|  - Methods: `initialize`, `session/new`, `session/prompt`, `session/cancel`       |
+-----------------------------------------------------------------------------------+
                                          |
                              (Turn Execution Request)
                                          v
+-----------------------------------------------------------------------------------+
|                            Harness Reasoning Core                                 |
|  - Triggers ReAct loop and MASS DAG task graph                                    |
|  - Streams thinking traces and execution chunks back to editor status bar         |
+-----------------------------------------------------------------------------------+
```

### ACP Core Protocol Methods:
1. **`initialize`**: Exchanges capabilities (prompt streaming, tool execution, session forking).
2. **`session/new`**: Allocates an isolated session ID tied to the active editor workspace.
3. **`session/prompt`**: Sends developer instructions and file contexts directly to the agent.
4. **`session/cancel`**: Triggers emergency stop to immediately halt active tool execution and model streaming.
