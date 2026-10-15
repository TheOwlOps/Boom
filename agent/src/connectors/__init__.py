"""External-service connectors for the Boom agent.

A connector is one module under this package exposing a module-level
``CONNECTOR`` instance of :class:`connectors.base.Connector`::

    # agent/src/connectors/gmail.py
    from connectors.base import Connector

    class GmailConnector(Connector):
        name = "gmail"

        def status(self):
            ...

        def build_tools(self):
            ...  # tool names prefixed: gmail_search, gmail_send, ...

    CONNECTOR = GmailConnector()

Rules every connector follows:
- Credentials come ONLY from the environment (``BOOM_<NAME>_*``); see
  :mod:`connectors.credentials`. Never prompt, never hardcode.
- ``status()`` is honest: ``connected=False`` when the credential is absent.
- Tool names are prefixed with the connector name so merged registries
  never collide; a clash raises :class:`DuplicateToolError`.
- Tool funcs return ``{"error": ...}`` on failure; they never raise to the
  caller and never report a success they did not observe.

Boot wiring (see :mod:`connectors.registry`)::

    from connectors.registry import load_default_registry
    registry = load_default_registry()          # skips missing/broken connectors
    tools.update(registry.build_tools())        # merge into the agent's tool dicts
"""

from connectors.base import Connector
from connectors.credentials import (
    connector_env,
    get_token,
    get_token_for,
    require_token,
    require_token_for,
)
from connectors.registry import (
    DEFAULT_CONNECTOR_MODULES,
    ConnectorRegistry,
    DuplicateToolError,
    load_default_registry,
)

__all__ = [
    "Connector",
    "ConnectorRegistry",
    "DuplicateToolError",
    "DEFAULT_CONNECTOR_MODULES",
    "connector_env",
    "get_token",
    "get_token_for",
    "load_default_registry",
    "require_token",
    "require_token_for",
]
