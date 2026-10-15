"""Registry merging every connector's tools into the agent's tool dicts.

Each connector module exposes a module-level ``CONNECTOR`` instance; the
registry never imports service SDKs itself. A connector that fails to import
(or misbehaves in status()) is skipped with a warning — one broken connector
must never take down the agent.
"""

import importlib
import logging
from typing import Any, Dict, List, Optional, Tuple

from connectors.base import Connector

logger = logging.getLogger("boom.connectors")

# Connector modules tried by load_default_registry(), in order.
DEFAULT_CONNECTOR_MODULES: Tuple[str, ...] = (
    "connectors.gmail",
    "connectors.google_calendar",
    "connectors.google_drive",
    "connectors.http_api",
    "connectors.github",
    "connectors.spotify",
    "connectors.weather",
    "connectors.news",
)


class DuplicateToolError(ValueError):
    """Two connectors registered the same tool name."""


class ConnectorRegistry:
    """Holds Connector instances; merges their tool dicts for the agent."""

    def __init__(self) -> None:
        self._connectors: Dict[str, Connector] = {}

    def register(self, connector: Connector) -> Connector:
        """Add a connector. ValueError on a duplicate or empty name."""
        if not isinstance(connector, Connector):
            raise TypeError(
                f"register() needs a connectors.base.Connector, got {type(connector).__name__}"
            )
        name = connector.name
        if not isinstance(name, str) or not name.strip():
            raise ValueError("connector.name must be a non-empty string")
        name = name.strip()
        if name in self._connectors:
            raise ValueError(f"connector {name!r} is already registered")
        self._connectors[name] = connector
        return connector

    def names(self) -> List[str]:
        """Registered connector names, in registration order."""
        return list(self._connectors.keys())

    def get(self, name: str) -> Optional[Connector]:
        """The connector, or None when unknown."""
        return self._connectors.get(name)

    def status_all(self) -> Dict[str, Dict[str, Any]]:
        """{"name": {"connected": bool, "detail": str}} for every connector.

        A status() that raises is reported as disconnected, never propagated:
        this is a config check, not a health probe.
        """
        out: Dict[str, Dict[str, Any]] = {}
        for name, connector in self._connectors.items():
            try:
                raw = connector.status() or {}
            except Exception as exc:  # noqa: BLE001 - status must never break the caller
                out[name] = {
                    "connected": False,
                    "detail": f"status() raised {type(exc).__name__}: {exc}",
                }
                continue
            out[name] = {
                "connected": bool(raw.get("connected", False)),
                "detail": str(raw.get("detail", "")),
            }
        return out

    def build_tools(self) -> Dict[str, Dict[str, Any]]:
        """Merge every connector's tool dicts. DuplicateToolError on a name clash."""
        merged: Dict[str, Dict[str, Any]] = {}
        origin: Dict[str, str] = {}
        for name, connector in self._connectors.items():
            tools = connector.build_tools() or {}
            for tool_name, meta in tools.items():
                if tool_name in merged:
                    raise DuplicateToolError(
                        f"tool {tool_name!r} registered by both "
                        f"{origin[tool_name]!r} and {name!r}"
                    )
                merged[tool_name] = meta
                origin[tool_name] = name
        return merged


def load_default_registry(
    module_names: Tuple[str, ...] = DEFAULT_CONNECTOR_MODULES,
) -> ConnectorRegistry:
    """Import each connector module and register its ``CONNECTOR`` instance.

    A module that fails to import, exposes no CONNECTOR, or fails to register
    is skipped with a warning — never a crash.
    """
    registry = ConnectorRegistry()
    for mod_name in module_names:
        try:
            module = importlib.import_module(mod_name)
        except Exception as exc:  # noqa: BLE001 - optional connectors stay optional
            logger.warning(
                "connector module %s not loaded: %s: %s",
                mod_name, type(exc).__name__, exc,
            )
            continue
        connector = getattr(module, "CONNECTOR", None)
        if connector is None:
            logger.warning("connector module %s has no CONNECTOR; skipped", mod_name)
            continue
        try:
            registry.register(connector)
        except Exception as exc:  # noqa: BLE001 - a bad connector must not break boot
            logger.warning(
                "connector %s not registered: %s: %s",
                mod_name, type(exc).__name__, exc,
            )
    return registry
