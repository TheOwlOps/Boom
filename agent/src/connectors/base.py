"""Connector contract for Boom agent/src/connectors/.

A connector bridges the agent to an external service (Gmail, Calendar, ...).
Credentials come ONLY from the environment (BOOM_<NAME>_*); nothing here ever
prompts for secrets or hardcodes one. status() must be honest: connected=False
when the credential is absent — never a fake success.

Tool names are prefixed with the connector name (gmail_search, calendar_list, ...)
so merged registries never collide.
"""

from typing import Any, Dict


class Connector:
    """One external service. Subclass, set `name`, implement both methods."""

    name: str = "base"

    def status(self) -> Dict[str, Any]:
        """{"connected": bool, "detail": str}. Config check only, no network needed."""
        raise NotImplementedError

    def build_tools(self) -> Dict[str, Dict[str, Any]]:
        """Tool dicts like every other Boom tool module.

        {tool_name: {"name":..., "func":..., "description":..., "schema":...}}
        func may be a plain function or `async def`. Errors are returned as
        {"error": ...}, never raised to the caller, never faked.
        """
        raise NotImplementedError


def _schema(properties=None, required=None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or []}
