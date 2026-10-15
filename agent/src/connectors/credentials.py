"""Environment-only credential helpers for connectors.

Rules, no exceptions:
- Credentials come from environment variables, following the BOOM_<NAME>_<SUFFIX>
  convention (e.g. BOOM_GMAIL_API_KEY). See `connector_env`.
- Nothing here ever prompts for a secret, writes one to disk, or hardcodes one.
- `get_token` is the honest check: "" means "not configured".
- `require_token` raises RuntimeError with setup guidance instead of failing
  later with a cryptic 401 from the service.
"""

import os
from typing import Optional


def connector_env(connector_name: str, suffix: str) -> str:
    """Canonical env var name for a connector credential: BOOM_<NAME>_<SUFFIX>."""
    name = (connector_name or "").strip().upper().replace("-", "_")
    sfx = (suffix or "").strip().upper().replace("-", "_")
    if not name or not sfx:
        raise ValueError("connector_env needs a non-empty connector name and suffix")
    return f"BOOM_{name}_{sfx}"


def get_token(env_name: str) -> str:
    """Credential value for `env_name`, stripped; "" when unset. Never raises."""
    if not env_name:
        return ""
    return (os.environ.get(env_name) or "").strip()


def require_token(env_name: str, hint: str = "") -> str:
    """Credential value, or RuntimeError telling the operator exactly what to export.

    `hint` names how to obtain the value (one short sentence, no secrets).
    """
    token = get_token(env_name)
    if token:
        return token
    guidance = (
        f"Missing credential: environment variable {env_name} is not set. "
        f"Export it before starting the agent, e.g. `export {env_name}=<value>`."
    )
    if hint:
        guidance += f" {hint}"
    raise RuntimeError(guidance)


def get_token_for(connector_name: str, suffix: str) -> str:
    """Shorthand: get_token(connector_env(connector_name, suffix))."""
    return get_token(connector_env(connector_name, suffix))


def require_token_for(connector_name: str, suffix: str, hint: str = "") -> str:
    """Shorthand: require_token(connector_env(connector_name, suffix), hint)."""
    return require_token(connector_env(connector_name, suffix), hint)
