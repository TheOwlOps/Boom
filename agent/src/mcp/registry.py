"""Connect the MCP servers the user declared and register their tools.

Config is read from `mcp.json` in the project root, in the shape every other MCP host
uses, and may be overridden inline by BOOM_MCP_SERVERS for a one-off run:

    {"mcpServers": {"files": {"command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."]}}}

A server may be a URL instead (streamable HTTP, JSON or SSE replies):

    {"mcpServers": {"remote": {"url": "https://host/mcp",
                               "headers": {"Authorization": "Bearer ${MY_MCP_TOKEN}"}}}}

A server that issues its own tokens takes an `oauth` block instead of a header; the client
asks its token endpoint for a client-credentials token and asks again when the server
answers 401:

    {"mcpServers": {"remote": {"url": "https://host/mcp",
                               "oauth": {"token_url": "https://host/token",
                                         "client_id": "${HOST_CLIENT_ID}",
                                         "client_secret": "${HOST_CLIENT_SECRET}"}}}}

Discovered tools are named `mcp__<server>__<tool>`, which keeps a third-party server from
shadowing a built-in. They are deliberately absent from the approval gate's SAFE_TOOLS:
a remote tool is not a read-only local default and must be approved like any other.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict

from mcp.client import MCPClient, MCPError

CONFIG_FILENAME = "mcp.json"
TOOL_PREFIX = "mcp__"


def load_server_configs(root_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Server definitions from BOOM_MCP_SERVERS, else mcp.json, else none."""
    raw = os.getenv("BOOM_MCP_SERVERS")
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError as e:
            print(f"[mcp] BOOM_MCP_SERVERS is not valid JSON, ignoring it: {e}")
            parsed = {}
    else:
        path = Path(root_dir) / CONFIG_FILENAME
        if not path.exists():
            return {}
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"[mcp] cannot read {path}: {e}")
            return {}

    servers = parsed.get("mcpServers", parsed) if isinstance(parsed, dict) else {}
    if not isinstance(servers, dict):
        print(f"[mcp] {CONFIG_FILENAME} must map server names to definitions")
        return {}
    # A server is either a command to spawn or a URL to talk to; anything else registers nothing.
    return {
        name: cfg for name, cfg in servers.items()
        if isinstance(cfg, dict) and (cfg.get("command") or cfg.get("url"))
    }


def _tool_function(client: MCPClient, tool_name: str) -> Callable:
    async def _call(**arguments) -> str:
        # to_thread: the client blocks on a pipe read, which would stall the ReAct event loop.
        return await asyncio.to_thread(client.call_tool, tool_name, arguments)

    return _call


def register_mcp_tools(env: Any, root_dir: Path = None) -> int:
    """Connect every configured server and add its tools to *env*. Returns how many.

    A server that fails to start is reported and skipped: one broken entry must not stop
    the agent from running with the rest.
    """
    configs = load_server_configs(root_dir or env.root_dir)
    if not configs:
        return 0

    registered = 0
    for name, cfg in configs.items():
        client = MCPClient(
            name=name,
            command=cfg.get("command") or [],
            env=cfg.get("env"),
            cwd=cfg.get("cwd"),
            timeout=float(cfg.get("timeout", 30.0)),
            url=cfg.get("url") or "",
            # A token in mcp.json would be a secret in the repo; `${VAR}` reads it from the env.
            headers=cfg.get("headers") or {},
            # Client-credentials OAuth, for a server that issues tokens instead of taking one.
            oauth=cfg.get("oauth") or {},
        )
        try:
            client.start()
        except MCPError as e:
            print(f"[mcp] server '{name}' unavailable: {e}")
            client.close()
            continue

        env.mcp_clients[name] = client
        for tool in client.tools:
            tool_name = tool.get("name")
            if not tool_name:
                continue
            key = f"{TOOL_PREFIX}{name}__{tool_name}"
            env.get_tools()[key] = {
                "name": key,
                "func": _tool_function(client, tool_name),
                "description": (tool.get("description") or f"MCP tool '{tool_name}' on server '{name}'."),
                # MCP inputSchema is already the JSON Schema get_tools_schema() forwards.
                "schema": tool.get("inputSchema") or {"type": "object", "properties": {}},
            }
            registered += 1
        print(f"[mcp] server '{name}': {len(client.tools)} tool(s) registered")

    return registered
