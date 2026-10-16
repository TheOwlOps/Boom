"""Marketplace backend for the Boom web UI.

Builds an installable-items catalog from REAL data sources instead of a
hardcoded list:

- Plugin manifests on disk, managed by the existing
  ``extensibility.lifecycle.PluginManager``. Install / enable / uninstall /
  list are all delegated to it — never reimplemented here.
- Builtin tool descriptions from ``AgentEnvironment.get_tools()`` (the same
  source ``GET /api/tools`` uses).

Category mapping is keyword-rule based (see ``_CATEGORY_RULES``); the icon
per category and the ``featured`` flag are rule-based too, so adding a new
tool or plugin never requires touching this file.

Item ids are namespaced: ``"plugin:<name>"`` and ``"tool:<name>"``. The
``install``/``uninstall`` entry points also accept a bare name and resolve
it (installed plugin first, then builtin tool).

Builtin tools are always present; "installing" one marks it enabled and
"uninstalling" one marks it disabled. The disabled set persists in a small
JSON state file (default ``~/.boom/marketplace.json``).

Stdlib only. The optional local-registry install path imports
``extensibility.marketplace`` lazily (it needs pydantic).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from extensibility.lifecycle import PluginManager
except ImportError:  # repo root on sys.path but agent/src is not
    from agent.src.extensibility.lifecycle import PluginManager


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MarketplaceError(Exception):
    """Marketplace failure carrying the HTTP status the route should return."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Category mapping (keyword rules — no hardcoded item lists)
# ---------------------------------------------------------------------------

# (category, keywords). First match wins; matching is on whole tokens from
# the item name + description (lowercased, split on non-alphanumerics).
_CATEGORY_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("Communication", (
        "telegram", "whatsapp", "discord", "slack", "messenger", "zalo",
        "line", "feishu", "sms", "broadcast", "sticker", "kick", "react",
    )),
    ("Productivity", (
        "gmail", "calendar", "drive", "email", "docs", "sheets", "notion",
        "todo", "task", "reminder", "note", "digest", "inbox",
    )),
    ("Developer Tools", (
        "github", "git", "gitops", "code", "review", "lint", "debug",
        "coverage", "deploy", "docker", "npm", "refactor", "ast",
        "depgraph", "unittest", "pytest",
    )),
    ("Web & Search", (
        "web", "search", "fetch", "scrape", "scraper", "crawl", "browser",
        "rss", "news", "feed", "extract", "http", "url",
    )),
    ("Media", (
        "image", "video", "audio", "tts", "stt", "voice", "ocr", "photo",
        "podcast", "banner", "icon", "imagegen",
    )),
    ("Files & Storage", (
        "file", "files", "fileops", "dir", "folder", "storage", "backup",
        "pdf", "csv", "excel", "spreadsheet", "document", "artifact",
    )),
    ("Data & AI", (
        "memory", "vector", "embedding", "llm", "model", "prompt", "rag",
        "dataset", "sql", "sqlite", "database", "knowledge",
    )),
    ("Automation", (
        "cron", "schedule", "scheduler", "trigger", "webhook", "workflow",
        "dag", "autonomous", "plan", "planner",
    )),
    ("Security", (
        "security", "auth", "authz", "secret", "secretscan", "scan",
        "sanitize", "audit", "permission", "redact", "guard", "ratelimit",
    )),
    ("System", (
        "shell", "terminal", "process", "system", "env", "config", "log",
        "logs", "monitor", "health", "ipc", "daemon", "lifecycle",
        "checkpoint",
    )),
]

_DEFAULT_CATEGORY = "Utilities"

_CATEGORY_ICONS: Dict[str, str] = {
    "Communication": "💬",
    "Productivity": "📅",
    "Developer Tools": "🛠️",
    "Web & Search": "🌐",
    "Media": "🎨",
    "Files & Storage": "📁",
    "Data & AI": "🧠",
    "Automation": "⏰",
    "Security": "🔒",
    "System": "⚙️",
    "Utilities": "🧩",
}

# Rule-based "featured" set: curated plugins plus the integration categories
# end users install first. No per-item list.
_FEATURED_CATEGORIES = {"Communication", "Productivity"}


def _tokenize(text: str) -> set:
    """Split text into lowercase alphanumeric tokens."""
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def categorize_item(name: str, description: str = "") -> str:
    """Map an item to a category using the keyword rules. Never raises."""
    tokens = _tokenize(name) | _tokenize(description)
    for category, keywords in _CATEGORY_RULES:
        if tokens.intersection(keywords):
            return category
    return _DEFAULT_CATEGORY


def _semver_key(version: str) -> tuple:
    """Rough semver sort key (numeric parts sort numerically)."""
    key = []
    for part in re.split(r"[.\-+]", str(version)):
        key.append((0, int(part)) if part.isdigit() else (1, part))
    return tuple(key)


def _build_environment():
    """Build a real AgentEnvironment (lazy so import stays cheap)."""
    try:
        from agent.src.environment import AgentEnvironment
    except ImportError:  # agent/src itself on sys.path
        from environment import AgentEnvironment
    return AgentEnvironment()


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class MarketplaceBackend:
    """Marketplace logic: catalog, installed state, install/uninstall.

    All plugin operations delegate to the existing
    ``extensibility.lifecycle.PluginManager``.
    """

    TOOL_PREFIX = "tool:"
    PLUGIN_PREFIX = "plugin:"

    def __init__(
        self,
        plugins_dir: Optional[Path | str] = None,
        state_path: Optional[Path | str] = None,
        registry_path: Optional[Path | str] = None,
        env: Any = None,
    ):
        # Same default as AgentEnvironment: <cwd>/plugins.
        self.plugins_dir = Path(plugins_dir) if plugins_dir else Path.cwd() / "plugins"
        self.state_path = (
            Path(state_path) if state_path else Path.home() / ".boom" / "marketplace.json"
        )
        self.registry_path = Path(registry_path) if registry_path else None
        self._env = env  # injected by tests, otherwise built lazily
        self.manager = PluginManager(self.plugins_dir)
        self._state = self._load_state()
        self._tools_cache: Optional[Dict[str, dict]] = None

    # -- persistent state -------------------------------------------------

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                disabled = data.get("disabled_tools") or []
                return {"disabled_tools": [str(x) for x in disabled]}
        except (OSError, ValueError):
            pass
        return {"disabled_tools": []}

    def _save_state(self) -> None:
        # Best-effort: a state write failure must never break install/uninstall.
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass

    # -- data sources -----------------------------------------------------

    @property
    def env(self) -> Any:
        if self._env is None:
            self._env = _build_environment()
        return self._env

    def _builtin_tools(self) -> Dict[str, dict]:
        if self._tools_cache is None:
            try:
                tools = self.env.get_tools() or {}
            except Exception:
                tools = {}
            self._tools_cache = {
                name: spec for name, spec in tools.items()
                if isinstance(name, str) and isinstance(spec, dict)
            }
        return self._tools_cache

    def _plugin_manifest(self, name: str) -> dict:
        """Read the raw plugin.json for description/author/icon/tags."""
        info = self.manager.plugins.get(name) or {}
        plugin_dir = info.get("dir") or (self.plugins_dir / name)
        try:
            data = json.loads((Path(plugin_dir) / "plugin.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    # -- id handling ------------------------------------------------------

    def _split_id(self, item_id: str) -> Tuple[str, str]:
        """Split an id into (kind, name). Raises MarketplaceError on problems."""
        if not isinstance(item_id, str) or not item_id.strip():
            raise MarketplaceError("item id is required", 400)
        item_id = item_id.strip()
        if ":" in item_id:
            prefix, name = item_id.split(":", 1)
            prefix, name = prefix.strip().lower(), name.strip()
            if prefix == "plugin" and name:
                return ("plugin", name)
            if prefix == "tool" and name:
                return ("tool", name)
            raise MarketplaceError(f"invalid item id '{item_id}'", 400)
        # Bare name: installed plugin wins, then builtin tool.
        if item_id in self.manager.plugins:
            return ("plugin", item_id)
        if item_id in self._builtin_tools():
            return ("tool", item_id)
        raise MarketplaceError(f"item '{item_id}' not found", 404)

    # -- catalog ----------------------------------------------------------

    def _plugin_item(self, name: str) -> dict:
        manifest = self._plugin_manifest(name)
        info = self.manager.plugins[name]
        description = str(manifest.get("description") or "")
        category = categorize_item(name, description)
        return {
            "id": f"{self.PLUGIN_PREFIX}{name}",
            "name": name,
            "icon": str(manifest.get("icon") or _CATEGORY_ICONS.get(category, "🧩")),
            "description": description,
            "category": category,
            # Plugins are curated additions — always featured.
            "featured": True,
            "kind": "plugin",
            "version": str((info.get("manifest") or {}).get("version", "")),
            "author": str(manifest.get("author") or ""),
            "enabled": bool(info.get("enabled")),
        }

    def _tool_item(self, name: str, spec: dict, disabled: set) -> dict:
        description = str(spec.get("description") or "")
        category = categorize_item(name, description)
        return {
            "id": f"{self.TOOL_PREFIX}{name}",
            "name": name,
            "icon": _CATEGORY_ICONS.get(category, "🧩"),
            "description": description,
            "category": category,
            "featured": category in _FEATURED_CATEGORIES,
            "kind": "tool",
            "enabled": name not in disabled,
        }

    def get_catalog(self) -> List[dict]:
        """Build the catalog from real plugin manifests + real tool specs."""
        items: List[dict] = []
        for name in sorted(self.manager.plugins):
            items.append(self._plugin_item(name))
        disabled = set(self._state.get("disabled_tools") or [])
        tools = self._builtin_tools()
        for name in sorted(tools):
            items.append(self._tool_item(name, tools[name], disabled))
        return items

    def get_installed(self) -> List[str]:
        """Ids of installed plugins + enabled builtin tools."""
        installed = [f"{self.PLUGIN_PREFIX}{n}" for n in sorted(self.manager.plugins)]
        disabled = set(self._state.get("disabled_tools") or [])
        installed += [
            f"{self.TOOL_PREFIX}{n}"
            for n in sorted(self._builtin_tools())
            if n not in disabled
        ]
        return installed

    # -- install / uninstall ----------------------------------------------

    def _install_plugin_from_registry(self, name: str) -> bool:
        """Install the newest registry version of a plugin. True on success."""
        if self.registry_path is None:
            return False
        try:
            try:
                from extensibility.marketplace import PluginMarketplace
            except ImportError:
                from agent.src.extensibility.marketplace import PluginMarketplace
            registry = PluginMarketplace(self.registry_path)
            versions = [m.version for m in registry.list_available() if m.name == name]
            if not versions:
                return False
            newest = sorted(versions, key=_semver_key)[-1]
            registry.install_from_registry(
                name, newest, plugins_dir=self.plugins_dir
            )
        except Exception:
            return False
        self.manager.refresh()
        return name in self.manager.plugins

    def install_item(self, item_id: str) -> dict:
        """Install/enable an item. Delegates plugin work to PluginManager."""
        kind, name = self._split_id(item_id)
        if kind == "plugin":
            if name not in self.manager.plugins:
                if not self._install_plugin_from_registry(name):
                    raise MarketplaceError(f"plugin '{name}' not found", 404)
            result = self.manager.enable(name)
            if not result.get("enabled"):
                raise MarketplaceError(
                    f"could not enable plugin '{name}': "
                    f"{result.get('error', 'unknown error')}",
                    500,
                )
            return {"ok": True, "id": f"{self.PLUGIN_PREFIX}{name}"}
        # Builtin tool: mark enabled.
        if name not in self._builtin_tools():
            raise MarketplaceError(f"tool '{name}' does not exist", 404)
        disabled = set(self._state.get("disabled_tools") or [])
        if name in disabled:
            disabled.discard(name)
            self._state["disabled_tools"] = sorted(disabled)
            self._save_state()
        return {"ok": True, "id": f"{self.TOOL_PREFIX}{name}"}

    def uninstall_item(self, item_id: str) -> dict:
        """Uninstall/disable an item. Delegates plugin work to PluginManager."""
        kind, name = self._split_id(item_id)
        if kind == "plugin":
            if name not in self.manager.plugins:
                raise MarketplaceError(f"plugin '{name}' is not installed", 404)
            result = self.manager.uninstall(name)
            if not result.get("uninstalled"):
                raise MarketplaceError(
                    f"could not uninstall plugin '{name}': "
                    f"{result.get('error', 'unknown error')}",
                    500,
                )
            return {"ok": True, "id": f"{self.PLUGIN_PREFIX}{name}"}
        # Builtin tool: mark disabled (builtins cannot be deleted).
        if name not in self._builtin_tools():
            raise MarketplaceError(f"tool '{name}' does not exist", 404)
        disabled = set(self._state.get("disabled_tools") or [])
        if name not in disabled:
            disabled.add(name)
            self._state["disabled_tools"] = sorted(disabled)
            self._save_state()
        return {"ok": True, "id": f"{self.TOOL_PREFIX}{name}"}


# ---------------------------------------------------------------------------
# Module-level convenience API (used by gateway/api.py handlers)
# ---------------------------------------------------------------------------

_default_backend: Optional[MarketplaceBackend] = None


def _default() -> MarketplaceBackend:
    global _default_backend
    if _default_backend is None:
        _default_backend = MarketplaceBackend()
    return _default_backend


def get_marketplace_catalog() -> List[dict]:
    """Catalog built from real plugin manifests + real tool descriptions."""
    return _default().get_catalog()


def get_installed() -> List[str]:
    """Ids of installed plugins + enabled builtin tools."""
    return _default().get_installed()


def install_item(item_id: str) -> dict:
    """Install/enable ``item_id``. Returns ``{"ok": True, "id": ...}``."""
    return _default().install_item(item_id)


def uninstall_item(item_id: str) -> dict:
    """Uninstall/disable ``item_id``. Returns ``{"ok": True, "id": ...}``."""
    return _default().uninstall_item(item_id)
