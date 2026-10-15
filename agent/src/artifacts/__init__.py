"""User-facing artifacts: finished documents the agent builds for the owner.

The builder writes real files under ``<root_dir>/artifacts/`` — Markdown reports,
complete HTML pages, CSV tables and pretty JSON. ``tools.build_tools(env)`` exposes
them to the ReAct loop as four tools, wired into AgentEnvironment exactly like the
cron and browser tool modules.
"""

from artifacts.documents import DocumentBuilder

__all__ = ["DocumentBuilder"]
