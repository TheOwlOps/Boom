"""Provider configuration manager for Boom.

Stdlib only (``json`` / ``os`` / ``pathlib`` / ``urllib`` / ``socket``).

Stores per-provider credentials and the active provider in
``$BOOM_HOME/providers.json`` (default ``~/.boom/providers.json``) with
``0600`` permissions. Secrets are **never** printed, logged, or included in
error messages — only written to the config file itself.

Design notes:
  - ``PROVIDER_DEFS`` is the single source of provider metadata (id, name,
    models, key requirement, default base URL, and how to probe the API).
    ``list_providers()`` exposes it to UIs; the gateway's
    ``GET /api/providers`` must add ``has_key`` itself (never the key).
  - ``test_connection()`` performs one lightweight, real HTTP request per
    provider kind (models list or a 1-token ping), with a 10s timeout.
    It returns ``(ok, message)`` — never raises for expected failures.
  - ``base_dir`` is injectable so tests can point at a temp directory
    instead of the real ``~/.boom``.
"""

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CONFIG_FILENAME = "providers.json"
CONFIG_MODE = 0o600
TEST_TIMEOUT_SEC = 10.0


class ProviderError(Exception):
    """Transport-level failure while talking to a provider API."""


class UnknownProviderError(ProviderError):
    """Raised when a provider id is not in PROVIDER_DEFS."""


# Single source of provider metadata. ``probe`` selects the lightweight
# request ``test_connection()`` sends:
#   - "openai_models": GET {base}/models with a Bearer token
#     (OpenAI, xAI, DeepSeek, Ollama, and custom OpenAI-compatible servers).
#   - "anthropic": POST {base}/v1/messages with max_tokens=1.
#   - "gemini_models": GET {base}/v1beta/models?key=...
PROVIDER_DEFS: List[Dict[str, Any]] = [
    {
        "id": "openai",
        "name": "OpenAI",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o4-mini"],
        "needs_key": True,
        "default_base_url": "https://api.openai.com/v1",
        "show_base_url": False,
        "allow_custom_model": True,
        "probe": "openai_models",
    },
    {
        "id": "anthropic",
        "name": "Anthropic",
        "models": ["claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5"],
        "needs_key": True,
        "default_base_url": "https://api.anthropic.com",
        "show_base_url": False,
        "allow_custom_model": True,
        "probe": "anthropic",
    },
    {
        "id": "google",
        "name": "Google (Gemini)",
        "models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
        "needs_key": True,
        "default_base_url": "https://generativelanguage.googleapis.com",
        "show_base_url": False,
        "allow_custom_model": True,
        "probe": "gemini_models",
    },
    {
        "id": "xai",
        "name": "xAI (Grok)",
        "models": ["grok-4", "grok-3", "grok-3-mini"],
        "needs_key": True,
        "default_base_url": "https://api.x.ai/v1",
        "show_base_url": False,
        "allow_custom_model": True,
        "probe": "openai_models",
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "needs_key": True,
        "default_base_url": "https://api.deepseek.com/v1",
        "show_base_url": False,
        "allow_custom_model": True,
        "probe": "openai_models",
    },
    {
        "id": "ollama",
        "name": "Ollama (local)",
        "models": ["llama3.3", "qwen3", "mistral-small3.1", "gemma3"],
        "needs_key": False,
        "default_base_url": "http://localhost:11434/v1",
        "show_base_url": True,
        "allow_custom_model": True,
        "probe": "openai_models",
    },
    {
        "id": "custom",
        "name": "Custom (OpenAI-compatible)",
        "models": [],
        "needs_key": True,
        "default_base_url": "",
        "show_base_url": True,
        "allow_custom_model": True,
        "probe": "openai_models",
    },
]


def _request_json(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Dict[str, Any]] = None,
    timeout: float = TEST_TIMEOUT_SEC,
) -> Tuple[int, Optional[Any]]:
    """Send one JSON HTTP request.

    Returns ``(status_code, parsed_body)``. HTTP error statuses (401, 500,
    ...) are returned, not raised. Raises :class:`ProviderError` on
    transport failures (DNS, refused connection, timeout). The URL may
    contain a key as a query parameter — callers must never log it.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # HTTPError still carries the response body; treat it as a status.
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        try:
            parsed = json.loads(raw) if raw.strip() else None
        except ValueError:
            parsed = None
        return e.code, parsed
    except urllib.error.URLError as e:
        raise ProviderError(f"connection failed: {e.reason}") from e
    except (TimeoutError, socket.timeout) as e:
        raise ProviderError("connection timed out") from e
    except OSError as e:
        raise ProviderError(f"connection failed: {e}") from e
    try:
        parsed = json.loads(raw) if raw.strip() else None
    except ValueError:
        parsed = None
    return resp.status, parsed


class ProviderManager:
    """Reads/writes provider credentials and tests connectivity."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        if base_dir is None:
            # Same convention as agent/src/config.py: $BOOM_HOME wins.
            base_dir = Path(os.environ.get("BOOM_HOME", str(Path.home() / ".boom")))
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / CONFIG_FILENAME

    # ------------------------------------------------------------ metadata

    def list_providers(self) -> List[Dict[str, Any]]:
        """Return provider metadata (id, name, models, needs_key, ...).

        Contains no secrets. Callers add ``has_key``/``active`` themselves.
        """
        out = []
        for d in PROVIDER_DEFS:
            out.append(
                {
                    "id": d["id"],
                    "name": d["name"],
                    "models": list(d["models"]),
                    "needs_key": d["needs_key"],
                    "default_base_url": d["default_base_url"],
                    "show_base_url": d["show_base_url"],
                    "allow_custom_model": d["allow_custom_model"],
                }
            )
        return out

    def _get_def(self, provider_id: str) -> Dict[str, Any]:
        for d in PROVIDER_DEFS:
            if d["id"] == provider_id:
                return d
        raise UnknownProviderError(f"unknown provider: {provider_id}")

    # ------------------------------------------------------------ storage

    def _load(self) -> Dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"active": None, "providers": {}}
        try:
            data = json.loads(raw)
        except ValueError:
            return {"active": None, "providers": {}}
        if not isinstance(data, dict):
            return {"active": None, "providers": {}}
        data.setdefault("active", None)
        data.setdefault("providers", {})
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        """Write the config file, keeping it owner-readable-only (0600)."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, CONFIG_MODE)
        tmp.replace(self.path)
        # replace() can reset the mode on some filesystems — enforce again.
        os.chmod(self.path, CONFIG_MODE)

    def save_config(
        self,
        provider_id: str,
        api_key: str,
        model: str,
        base_url: Optional[str] = None,
    ) -> None:
        """Persist credentials for a provider.

        Raises UnknownProviderError for a bad id, ValueError when a required
        API key is missing. The key is stored in the config file only — it is
        never printed or logged.
        """
        defn = self._get_def(provider_id)
        api_key = (api_key or "").strip()
        model = (model or "").strip()
        base_url = (base_url or "").strip()
        if defn["needs_key"] and not api_key:
            raise ValueError(f"{defn['name']} requires an API key")
        if not model:
            model = defn["models"][0] if defn["models"] else ""
        data = self._load()
        data["providers"][provider_id] = {
            "api_key": api_key,
            "model": model,
            "base_url": base_url or defn["default_base_url"],
        }
        self._save(data)

    def get_config(self, provider_id: str) -> Optional[Dict[str, str]]:
        """Return the saved config for a provider (includes the API key).

        Internal use only — never expose the key through an HTTP response.
        Returns None when nothing is saved yet.
        """
        defn = self._get_def(provider_id)
        cfg = self._load()["providers"].get(provider_id)
        if cfg is None:
            return None
        return {
            "api_key": cfg.get("api_key", "") or "",
            "model": cfg.get("model", "") or "",
            "base_url": cfg.get("base_url") or defn["default_base_url"],
        }

    def has_key(self, provider_id: str) -> bool:
        """True when a non-empty API key is saved for the provider."""
        cfg = self.get_config(provider_id)
        return bool(cfg and cfg["api_key"].strip())

    def get_active_provider(self) -> Optional[str]:
        data = self._load()
        active = data.get("active")
        ids = {d["id"] for d in PROVIDER_DEFS}
        return active if active in ids else None

    def set_active_provider(self, provider_id: str) -> None:
        self._get_def(provider_id)  # validates the id
        data = self._load()
        data["active"] = provider_id
        self._save(data)

    # ------------------------------------------------------------ probing

    def _build_probe(
        self, defn: Dict[str, Any], cfg: Dict[str, str]
    ) -> Tuple[str, str, Dict[str, str], Optional[Dict[str, Any]]]:
        """Build (method, url, headers, body) for a lightweight live check."""
        base = (cfg.get("base_url") or defn["default_base_url"]).rstrip("/")
        key = cfg.get("api_key") or ""
        probe = defn["probe"]
        if probe == "openai_models":
            headers: Dict[str, str] = {}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            return ("GET", f"{base}/models", headers, None)
        if probe == "anthropic":
            headers = {
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body = {
                "model": cfg.get("model") or defn["models"][0],
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
            return ("POST", f"{base}/v1/messages", headers, body)
        if probe == "gemini_models":
            url = f"{base}/v1beta/models?key={urllib.parse.quote(key, safe='')}"
            return ("GET", url, {}, None)
        raise ProviderError(f"no probe defined for provider {defn['id']}")

    def test_connection(self, provider_id: str) -> Tuple[bool, str]:
        """Send one lightweight real request to the provider's API.

        Returns ``(ok, message)``. Never raises for expected failures and
        never includes the API key in the message.
        """
        try:
            defn = self._get_def(provider_id)
        except UnknownProviderError:
            return False, f"unknown provider: {provider_id}"
        cfg = self.get_config(provider_id)
        if cfg is None:
            return False, "no configuration saved for this provider yet"
        if defn["needs_key"] and not cfg["api_key"].strip():
            return False, "API key is required but not set"
        try:
            method, url, headers, body = self._build_probe(defn, cfg)
            status, _ = _request_json(
                method, url, headers=headers, body=body, timeout=TEST_TIMEOUT_SEC
            )
        except ProviderError as e:
            return False, str(e)
        except Exception as e:  # pragma: no cover - defensive
            return False, f"unexpected error: {type(e).__name__}"
        if 200 <= status < 300:
            return True, "connected"
        if status in (401, 403):
            return False, f"authentication failed (HTTP {status}): check the API key"
        return False, f"request failed (HTTP {status})"
