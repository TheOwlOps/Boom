"""MCP (Model Context Protocol) client: stdio JSON-RPC, no third-party SDK."""

from mcp.client import MCPClient, MCPError, content_text
from mcp.registry import load_server_configs, register_mcp_tools

__all__ = ["MCPClient", "MCPError", "content_text", "load_server_configs", "register_mcp_tools"]
