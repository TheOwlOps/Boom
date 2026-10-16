"""
Plugin Lifecycle Manager for Boom.

Extends the ``plugin_registry.py`` idea (scan_and_load loads everything
unordered) with a full lifecycle: install -> enable -> disable ->
uninstall, where enable follows dependency order (topological sort).

A plugin is a directory containing:
    plugin.json   - manifest: {"name", "version", "entry", "dependencies"}
    entry.py      - entry module, with a register(ctx) function (legacy style)
                    and/or lifecycle hooks: on_enable(ctx), on_disable(),
                    on_uninstall() (optional)

Stdlib only.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Manifest validation (minimal, self-contained — no other-module dependency)
# ---------------------------------------------------------------------------

def validate_manifest(data: Any) -> Dict[str, str]:
    """Validates a manifest dict just parsed from plugin.json.

    Returns a normalized {"name": ..., "version": ..., "entry": ...,
    "dependencies": [...]} or raises ValueError with an English message.
    """
    if not isinstance(data, dict):
        raise ValueError("invalid plugin.json: content must be a JSON object")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("invalid plugin.json: missing 'name' field")
    name = name.strip()
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError(f"invalid plugin.json: invalid plugin name: {name!r}")

    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"plugin '{name}': missing a valid 'version' field")

    entry = data.get("entry", "entry.py")
    if not isinstance(entry, str) or not entry.strip():
        raise ValueError(f"plugin '{name}': invalid 'entry' field")
    entry = entry.strip()
    if entry.startswith("/") or ".." in Path(entry).parts:
        raise ValueError(f"plugin '{name}': 'entry' must not point outside the plugin directory")

    dependencies = data.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(d, str) and d.strip() for d in dependencies
    ):
        raise ValueError(f"plugin '{name}': 'dependencies' must be a list of strings")
    dependencies = [d.strip() for d in dependencies]
    if name in dependencies:
        raise ValueError(f"plugin '{name}': cannot depend on itself")

    return {"name": name, "version": version.strip(), "entry": entry,
            "dependencies": dependencies}


def load_manifest(path: Path) -> Dict[str, Any]:
    """Reads + parses plugin.json, raises ValueError on any problem."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read plugin.json: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"plugin.json is not valid JSON: {exc}") from exc
    return validate_manifest(data)


# ---------------------------------------------------------------------------
# Dependency topological sort
# ---------------------------------------------------------------------------

def topo_sort(nodes: Dict[str, List[str]]) -> List[str]:
    """Topological sort: every node comes AFTER its dependencies.

    ``nodes``: dict {name: [dependency names]}. Dependencies not present in
    ``nodes`` are skipped (assumed checked for existence beforehand).

    Kahn's algorithm: repeatedly take nodes with no unprocessed dependencies.
    If a round takes nothing while nodes remain -> the graph has a cycle ->
    raises ValueError.
    """
    # Copy so the caller's data isn't mutated
    remaining: Dict[str, set] = {
        name: {d for d in deps if d in nodes} for name, deps in nodes.items()
    }
    order: List[str] = []

    while remaining:
        # "Ready" nodes: no unprocessed dependencies left
        ready = sorted(name for name, deps in remaining.items() if not deps)
        if not ready:
            cycle = ", ".join(sorted(remaining))
            raise ValueError(
                f"circular dependency detected between plugins: {cycle}"
            )
        for name in ready:
            order.append(name)
            del remaining[name]
        # Remove newly ordered nodes from the remaining nodes' dependency sets
        done = set(ready)
        for deps in remaining.values():
            deps -= done

    return order


# ---------------------------------------------------------------------------
# PluginManager
# ---------------------------------------------------------------------------

class PluginManager:
    """Manages the plugin lifecycle: install / enable / disable / uninstall."""

    def __init__(self, plugins_dir: Path, ctx: Optional[Any] = None):
        self.plugins_dir = Path(plugins_dir)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        # {name: {"manifest": dict, "enabled": bool, "module": module|None}}
        self.plugins: Dict[str, Dict[str, Any]] = {}
        # Persisted enabled-plugin list so enable() survives refresh().
        self._state_file = self.plugins_dir / ".enabled.json"
        # Use PluginContext (has register_tool) instead of a plain dict,
        # so plugin register(ctx)/on_enable(ctx) hooks work correctly.
        if ctx is not None:
            self.ctx = ctx
        else:
            from .plugin_registry import PluginContext
            self.ctx = PluginContext()
        self.refresh()

    def _load_enabled(self) -> set:
        """Read the persisted set of enabled plugin names."""
        try:
            import json
            if self._state_file.is_file():
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return set(data)
        except Exception:
            pass
        return set()

    def _save_enabled(self) -> None:
        """Persist the enabled-plugin set atomically."""
        import json
        import os
        enabled = sorted(n for n, i in self.plugins.items() if i["enabled"])
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(enabled), encoding="utf-8")
        os.replace(tmp, self._state_file)

    # -- internal ---------------------------------------------------------

    def refresh(self) -> None:
        """Re-scans plugins_dir, loading every installed plugin's manifest."""
        previously_enabled = self._load_enabled()
        # Also keep in-memory enabled flags so refresh() doesn't wipe
        # plugins enabled earlier in this process lifetime.
        previously_enabled |= {
            n for n, i in self.plugins.items() if i["enabled"]
        }
        self.plugins.clear()
        for p in sorted(self.plugins_dir.iterdir()):
            manifest_file = p / "plugin.json"
            if p.is_dir() and manifest_file.exists():
                try:
                    manifest = load_manifest(manifest_file)
                except ValueError:
                    continue  # broken manifest: skip, don't crash the manager
                name = manifest["name"]
                self.plugins[name] = {
                    "manifest": manifest,
                    "enabled": name in previously_enabled,
                    "module": None,
                    "dir": p,
                }

    def _load_module(self, name: str):
        manifest = self.plugins[name]["manifest"]
        entry_file = self.plugins[name]["dir"] / manifest["entry"]
        if not entry_file.exists():
            raise ValueError(
                f"plugin '{name}': entry file '{manifest['entry']}' not found"
            )
        spec = importlib.util.spec_from_file_location(
            f"boom_plugin_{name}", entry_file)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def _dependents_enabled(self, name: str) -> List[str]:
        """Enabled plugins that (directly) depend on name."""
        result = []
        for other, info in self.plugins.items():
            if info["enabled"] and name in info["manifest"]["dependencies"]:
                result.append(other)
        return sorted(result)

    def _enable_order(self, name: str) -> List[str]:
        """Enable order for name + its full (transitive) dependency set."""
        if name not in self.plugins:
            raise ValueError(f"plugin '{name}' is not installed")
        # Collect the closure: name + every recursive dependency
        closure = set()

        def collect(n: str):
            if n in closure:
                return
            closure.add(n)
            for dep in self.plugins[n]["manifest"]["dependencies"]:
                if dep not in self.plugins:
                    raise ValueError(
                        f"plugin '{name}': missing dependency '{dep}' "
                        f"(install it first)"
                    )
                collect(dep)

        collect(name)
        graph = {n: self.plugins[n]["manifest"]["dependencies"] for n in closure}
        return topo_sort(graph)

    # -- public API -------------------------------------------------------

    def install(self, source_dir: Path) -> dict:
        """Installs a plugin from a source directory into plugins_dir."""
        source_dir = Path(source_dir)
        manifest_file = source_dir / "plugin.json"
        if not source_dir.is_dir() or not manifest_file.exists():
            return {"installed": False,
                    "error": "source directory has no plugin.json"}

        try:
            manifest = load_manifest(manifest_file)
        except ValueError as exc:
            return {"installed": False, "error": str(exc)}
        name = manifest["name"]

        if name in self.plugins:
            return {"installed": False,
                    "error": f"plugin '{name}' is already installed"}

        missing = [d for d in manifest["dependencies"] if d not in self.plugins]
        if missing:
            return {"installed": False,
                    "error": f"plugin '{name}' has uninstalled dependencies: "
                             f"{', '.join(missing)}"}

        entry_src = source_dir / manifest["entry"]
        if not entry_src.exists():
            return {"installed": False,
                    "error": f"plugin '{name}': entry file "
                             f"'{manifest['entry']}' not found in source directory"}

        dest = self.plugins_dir / name
        try:
            shutil.copytree(source_dir, dest)
        except OSError as exc:
            return {"installed": False, "error": f"could not copy plugin: {exc}"}

        self.plugins[name] = {"manifest": manifest, "enabled": False,
                              "module": None, "dir": dest}
        return {"installed": True, "name": name}

    def enable(self, name: str) -> dict:
        """Enables a plugin: dependencies load first, in topological order."""
        try:
            order = self._enable_order(name)
        except ValueError as exc:
            return {"enabled": False, "error": str(exc)}

        enabled_now: List[str] = []
        try:
            for pname in order:
                info = self.plugins[pname]
                if info["enabled"]:
                    continue
                module = self._load_module(pname)
                # New-style on_enable(ctx) hook; falls back to the legacy
                # register(ctx) for backward compatibility.
                if hasattr(module, "on_enable"):
                    module.on_enable(self.ctx)
                elif hasattr(module, "register"):
                    module.register(self.ctx)
                info["module"] = module
                info["enabled"] = True
                enabled_now.append(pname)
        except Exception as exc:  # noqa: BLE001 - surface plugin errors
            return {"enabled": False,
                    "error": f"plugin '{pname}' failed to enable: {exc}",
                    "enabled_order": enabled_now}

        self._save_enabled()
        return {"enabled": True, "name": name, "enabled_order": enabled_now}

    def disable(self, name: str) -> dict:
        """Disables a plugin (keeps the module in memory for fast re-enable)."""
        if name not in self.plugins:
            return {"disabled": False,
                    "error": f"plugin '{name}' is not installed"}
        info = self.plugins[name]
        if not info["enabled"]:
            return {"disabled": True, "name": name, "note": "already disabled"}

        dependents = self._dependents_enabled(name)
        if dependents:
            return {"disabled": False,
                    "error": f"refuse to disable '{name}' while other plugins "
                             f"depend on it: {', '.join(dependents)}"}

        module = info["module"]
        if module is not None and hasattr(module, "on_disable"):
            try:
                module.on_disable()
            except Exception as exc:  # noqa: BLE001
                return {"disabled": False,
                        "error": f"plugin '{name}': on_disable failed: {exc}"}
        info["enabled"] = False
        self._save_enabled()
        return {"disabled": True, "name": name}

    def uninstall(self, name: str) -> dict:
        """Uninstalls: disables first, calls on_uninstall if present, then deletes the directory.

        Refuses to delete bundled plugins (shipped with the repo / tracked by
        git) — those can only be disabled, not removed.
        """
        if name not in self.plugins:
            return {"uninstalled": False,
                    "error": f"plugin '{name}' is not installed"}

        result = self.disable(name)
        if not result.get("disabled"):
            return {"uninstalled": False, "error": result.get("error", "")}

        plugin_dir = self.plugins[name]["dir"]
        # Don't delete bundled plugins (tracked in git).
        try:
            import subprocess
            tracked = subprocess.run(
                ["git", "ls-files", str(plugin_dir)],
                capture_output=True, text=True, timeout=5,
            )
            if tracked.returncode == 0 and tracked.stdout.strip():
                del self.plugins[name]
                self._save_enabled()
                return {"uninstalled": True, "name": name,
                        "note": "bundled plugin disabled (not deleted)"}
        except Exception:
            pass  # git check failed: fall through to normal uninstall

        module = self.plugins[name]["module"]
        if module is not None and hasattr(module, "on_uninstall"):
            try:
                module.on_uninstall()
            except Exception as exc:  # noqa: BLE001
                return {"uninstalled": False,
                        "error": f"plugin '{name}': on_uninstall failed: {exc}"}

        try:
            shutil.rmtree(plugin_dir)
        except OSError as exc:
            return {"uninstalled": False,
                    "error": f"could not delete plugin directory '{name}': {exc}"}
        del self.plugins[name]
        self._save_enabled()
        return {"uninstalled": True, "name": name}

    def list(self) -> list:
        """Lists every installed plugin."""
        return [
            {"name": name,
             "version": info["manifest"]["version"],
             "enabled": info["enabled"],
             "dependencies": list(info["manifest"]["dependencies"])}
            for name, info in sorted(self.plugins.items())
        ]
