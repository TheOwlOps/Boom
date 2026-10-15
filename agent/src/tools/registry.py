"""Central Tool Registry for Boom Agent, registering built-in tools into modular toolsets."""

import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Toolset membership for the built-in tools. The callables themselves come from
# AgentEnvironment so this registry cannot drift from what the model is offered.
TOOLSETS: Dict[str, List[str]] = {
    "file": ["read_file", "write_file", "patch_file"],
    "skills": ["list_skills", "view_skill", "install_skill"],
    "code_execution": ["execute_code", "ast_inspect"],
    "sandbox": ["sandbox_exec"],
    "terminal": ["process_list", "process_poll", "process_start", "process_kill"],
    "memory": ["memory"],
    "session_search": ["session_search"],
}

def tool_error(message: str) -> str:
    """The failure envelope a tool handler returns: a JSON string, not an exception."""
    return json.dumps({"error": message})

class ToolEntry:
    def __init__(self, name: str, func: Callable, toolset: str, description: str = "", schema: Optional[Dict[str, Any]] = None):
        self.name = name
        self.func = func
        self.toolset = toolset
        self.description = description
        self.schema = schema or {}

class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, ToolEntry] = {}
        self._toolsets: Dict[str, List[str]] = {}
        self._loaded = False

    def register(self, name: str, func: Callable, toolset: str = "default", description: str = "", schema: Optional[Dict[str, Any]] = None):
        entry = ToolEntry(name=name, func=func, toolset=toolset, description=description, schema=schema)
        self._tools[name] = entry
        if toolset not in self._toolsets:
            self._toolsets[toolset] = []
        if name not in self._toolsets[toolset]:
            self._toolsets[toolset].append(name)

    def _ensure_loaded(self):
        """Populate on first read. Importing this module must not build an environment."""
        if self._loaded:
            return
        self._loaded = True  # set first: registration reads back through this class
        register_builtin_tools()

    def get_tool(self, name: str) -> Optional[ToolEntry]:
        self._ensure_loaded()
        return self._tools.get(name)

    def get_all_tool_names(self) -> List[str]:
        self._ensure_loaded()
        return sorted(list(self._tools.keys()))

    def get_registered_toolset_names(self) -> List[str]:
        self._ensure_loaded()
        return sorted(list(self._toolsets.keys()))

    def get_toolset_for_tool(self, name: str) -> Optional[str]:
        self._ensure_loaded()
        entry = self._tools.get(name)
        return entry.toolset if entry else None

    def get_tools_in_toolset(self, toolset: str) -> List[str]:
        self._ensure_loaded()
        return self._toolsets.get(toolset, [])

    def get_tool_names_for_toolset(self, toolset: str) -> List[str]:
        return self.get_tools_in_toolset(toolset)

    def dispatch(self, name: str, **kwargs) -> Any:
        self._ensure_loaded()
        entry = self._tools.get(name)
        if not entry:
            return {"error": f"Tool '{name}' not found in registry."}
        return entry.func(**kwargs)

# Global registry instance
registry = ToolRegistry()

def register_builtin_tools(env: Any = None):
    """Register every tool AgentEnvironment exposes, grouped by TOOLSETS."""
    try:
        from environment import AgentEnvironment
    except ModuleNotFoundError:
        # This file lives in agent/src/tools, so agent/src is the import root the rest of
        # the package assumes. Callers reach the registry through several different paths.
        src_dir = str(Path(__file__).resolve().parent.parent)
        if src_dir not in sys.path:
            sys.path.insert(0, src_dir)
        from environment import AgentEnvironment

    toolset_of = {name: ts for ts, names in TOOLSETS.items() for name in names}
    for name, meta in (env or AgentEnvironment()).get_tools().items():
        registry.register(
            name,
            meta["func"],
            toolset=toolset_of.get(name, "plugin"),
            description=meta.get("description", ""),
            schema=meta.get("schema"),
        )
