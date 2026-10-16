"""Boom's unified plugin system.

Merges 2 previously separate systems:

- **Skills** (``skill_loader.py`` / ``skills_hub.py``): dirs containing ``SKILL.md``
  — markdown instructions for the LLM to read, lazy-loaded, no code executed.
- **Plugins** (``plugin_registry.py``): dirs containing ``plugin.json`` + ``entry.py``
  — real Python code, loaded via importlib, full permissions, no lifecycle.

New architecture: each plugin has ``plugin.json`` (manifest) + ``entry.py`` (code)
+ ``SKILL.md`` (optional, LLM instructions), managed by ``PluginManager``
with a full lifecycle (install → enable → disable → uninstall → update),
manifest read via ``PluginManifest``, and permission-based sandboxing via
``PluginSandbox``.

The imports below use ``try/except ImportError`` so the package stays
importable even when a component module (written by another worker in
parallel) doesn't exist yet. When a module appears, its export becomes real.
"""

__all__ = ["PluginManager", "PluginManifest", "PluginSandbox"]

try:
    from .lifecycle import PluginManager
except ImportError:  # module written by another worker, may not exist yet
    PluginManager = None  # type: ignore[assignment]

try:
    from .manifest import PluginManifest
except ImportError:  # module written by another worker, may not exist yet
    PluginManifest = None  # type: ignore[assignment]

try:
    from .sandbox import PluginSandbox
except ImportError:  # module written by another worker, may not exist yet
    PluginSandbox = None  # type: ignore[assignment]


def available():
    """Returns a dict of export name -> True/False telling which modules are ready."""
    return {
        name: globals().get(name) is not None
        for name in __all__
    }
