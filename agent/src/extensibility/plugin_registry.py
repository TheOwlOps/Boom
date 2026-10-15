import importlib.util
import json
from pathlib import Path
from typing import Any, Callable, Dict
from pydantic import BaseModel

class PluginContext(BaseModel):
    tools: Dict[str, Dict[str, Any]] = {}

    def register_tool(self, name: str, func: Callable, description: str = "", schema: Dict[str, Any] = None):
        self.tools[name] = {
            "name": name,
            "func": func,
            "description": description,
            "schema": schema or {}
        }

class PluginRegistry:
    def __init__(self, plugins_dir: Path):
        self.plugins_dir = Path(plugins_dir)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.loaded_plugins: Dict[str, Any] = {}

    def scan_and_load(self) -> PluginContext:
        ctx = PluginContext()
        for p in self.plugins_dir.iterdir():
            if p.is_dir() and (p / "plugin.json").exists():
                manifest = json.loads((p / "plugin.json").read_text(encoding="utf-8"))
                entry_file = p / manifest.get("entry", "entry.py")
                if entry_file.exists():
                    plugin_mod = self._load_module(manifest["name"], entry_file)
                    if hasattr(plugin_mod, "register"):
                        plugin_mod.register(ctx)
                        self.loaded_plugins[manifest["name"]] = plugin_mod
        return ctx

    def _load_module(self, name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
